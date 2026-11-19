"""Target dispatcher, two-phase remote lock sessions, and SSH execution bridge."""

from __future__ import annotations

import base64
import functools
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from src.runtime import transport
from src.runtime.transport import JsonRequestError, _json_request

from .common import (
    ERR_INVALID_ARGUMENT,
    ERR_IO_ERROR,
    ERR_NOT_A_FILE,
    ERR_PATH_NOT_FOUND,
    ERR_POST_TRANSFER_HASH_MISMATCH,
    ERR_SSH_TRANSPORT_ERROR,
    CrossVolumeError,
    FileLayerError,
    TargetFileLock,
    atomic_replace_with_retry,
    compute_sha256,
    get_canonical_part_path,
    get_staging_dir,
    get_unique_temp_path,
    has_sftp_glob_chars,
    make_envelope,
    path_lock_key,
    to_sftp_remote_path,
)
from .fs_ops import (
    check_base_sha256,
    ensure_not_reparse_or_root,
    op_copy,
    op_edit,
    op_ls,
    op_mkdir,
    op_move,
    op_read,
    op_remove,
    op_stat,
    op_write_or_create,
)

SSH_BASE_ARGS = [
    "ssh",
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "UpdateHostKeys=no",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ConnectionAttempts=1",
    "-o",
    "ServerAliveInterval=3",
    "-o",
    "ServerAliveCountMax=2",
]


def _read_stdin_line_with_timeout(timeout_sec: float = 630.0) -> str:
    """Read a line from sys.stdin with a bounded timeout so remote locks never hang forever."""
    q_in: queue.Queue[tuple[Optional[str], Optional[BaseException]]] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            q_in.put((sys.stdin.readline(), None))
        except BaseException as exc:
            q_in.put((None, exc))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        line, err = q_in.get(timeout=max(1.0, float(timeout_sec)))
    except queue.Empty:
        raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"Remote session timed out waiting {timeout_sec:.1f}s for stdin")
    if err is not None:
        raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"Remote stdin read error: {err}")
    return line or ""


def dispatch_target_op(req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "stat":
        return op_stat(req)
    if op == "ls":
        return op_ls(req)
    if op == "read":
        return op_read(req)
    if op == "mkdir":
        return op_mkdir(req)
    if op == "create":
        return op_write_or_create(req, create_only=True)
    if op == "write":
        return op_write_or_create(req, create_only=False)
    if op == "edit":
        return op_edit(req)
    if op == "copy":
        return op_copy(req)
    if op in ("move", "rename"):
        return op_move(req)
    if op in ("remove", "delete"):
        return op_remove(req)
    raise FileLayerError(ERR_INVALID_ARGUMENT, f"Unsupported target operation: {op}")


def _publish_part_to_target(part_path: pathlib.Path, target: pathlib.Path, abs_path: str, expected_sha256: str) -> None:
    try:
        atomic_replace_with_retry(part_path, target)
    except CrossVolumeError:
        tmp_path = get_unique_temp_path(abs_path)
        try:
            shutil.copy2(part_path, tmp_path)
            if compute_sha256(tmp_path) != expected_sha256:
                raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, "Cross-volume staging hash mismatch")
            atomic_replace_with_retry(tmp_path, target)
            part_path.unlink(missing_ok=True)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def run_push_lock_session(req: dict[str, Any]) -> None:
    """
    Two-phase remote lock & atomic commit session over stdin/stdout.
    Holds the exclusive OS lock (msvcrt.locking) on the normalized remote path
    across Phase 1 (pre-check & part inspection), SFTP transfer, and Phase 2 (hash verify & atomic replace).
    """
    abs_path, norm_key, key_hash = path_lock_key(req["path"])
    target = pathlib.Path(abs_path)
    lock_timeout = float(req.get("lock_timeout_sec") or 10.0)
    session_timeout = float(req.get("session_timeout_sec") or 630.0)
    resume = bool(req.get("resume", False))
    no_overwrite = bool(req.get("no_overwrite", False))
    base_sha256 = req.get("base_sha256")

    try:
        with TargetFileLock(abs_path, timeout_sec=lock_timeout):
            target.parent.mkdir(parents=True, exist_ok=True)
            before_sha256 = check_base_sha256(target, abs_path, base_sha256, no_overwrite=no_overwrite)
            part_path = get_canonical_part_path(abs_path)
            part_path.parent.mkdir(parents=True, exist_ok=True)
            if not resume and part_path.exists():
                part_path.unlink(missing_ok=True)

            part_exists = part_path.exists()
            part_size = part_path.stat().st_size if part_exists else 0
            part_sha256 = compute_sha256(part_path) if (resume and part_exists and part_size > 0) else None

            phase1 = {
                "ok": True,
                "phase": "locked",
                "canonical_path": abs_path,
                "norm_key": norm_key,
                "lock_key": key_hash,
                "before_sha256": before_sha256,
                "part_path": str(part_path),
                "sftp_part_path": to_sftp_remote_path(str(part_path)),
                "part_exists": part_exists,
                "part_size": part_size,
                "part_sha256": part_sha256,
            }
            sys.stdout.write(json.dumps(phase1, ensure_ascii=False) + "\n")
            sys.stdout.flush()

            line = _read_stdin_line_with_timeout(session_timeout)
            if not line:
                return
            cmd_obj = json.loads(line)
            if cmd_obj.get("cmd") != "commit":
                if not bool(cmd_obj.get("keep_part", True)) and part_path.exists():
                    part_path.unlink(missing_ok=True)
                sys.stdout.write(json.dumps({"ok": False, "phase": "aborted"}) + "\n")
                sys.stdout.flush()
                return

            expected_sha256 = cmd_obj["expected_sha256"].lower()
            expected_size = int(cmd_obj["expected_size"])
            if not part_path.exists():
                raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, f"Staging part file missing: {part_path}")
            actual_size = part_path.stat().st_size
            actual_sha256 = compute_sha256(part_path)
            if actual_size != expected_size or actual_sha256 != expected_sha256:
                raise FileLayerError(
                    ERR_POST_TRANSFER_HASH_MISMATCH,
                    f"Post-transfer hash/size mismatch on {part_path}: expected {expected_sha256} ({expected_size}B), got {actual_sha256} ({actual_size}B)",
                    {
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                        "expected_size": expected_size,
                        "actual_size": actual_size,
                        "part_path": str(part_path),
                    },
                )

            check_base_sha256(target, abs_path, base_sha256, no_overwrite=no_overwrite)
            _publish_part_to_target(part_path, target, abs_path, actual_sha256)
            st = target.stat()
            phase2 = {
                "ok": True,
                "phase": "committed",
                "canonical_path": abs_path,
                "lock_key": key_hash,
                "before_sha256": before_sha256,
                "after_sha256": actual_sha256,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "part_path": str(part_path),
            }
            sys.stdout.write(json.dumps(phase2, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except FileLayerError as e:
        sys.stdout.write(json.dumps({"ok": False, "error_code": e.error_code, "error_message": e.message, "data": e.data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error_code": ERR_IO_ERROR, "error_message": f"{type(e).__name__}: {e}", "data": {}}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def run_pull_source_session(req: dict[str, Any]) -> None:
    """
    Remote source preparation session for pull.
    Inspects remote file and, if the remote path contains SFTP glob metacharacters,
    stages a temporary bracket-free hardlink/copy in <state_root>/staging/ for the download.
    """
    abs_path, _, key_hash = path_lock_key(req["path"])
    target = pathlib.Path(abs_path)
    prefix_bytes = req.get("prefix_bytes")
    session_timeout = float(req.get("session_timeout_sec") or 630.0)
    staged_link: Optional[pathlib.Path] = None
    try:
        ensure_not_reparse_or_root(target, abs_path)
        if not target.exists():
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"Remote source not found: {abs_path}", {"canonical_path": abs_path})
        if not target.is_file():
            raise FileLayerError(ERR_NOT_A_FILE, f"Remote source is not a file: {abs_path}", {"canonical_path": abs_path})
        st = target.stat()
        full_sha256 = compute_sha256(target)
        prefix_sha256 = compute_sha256(target, int(prefix_bytes)) if (prefix_bytes is not None and int(prefix_bytes) > 0) else None
        sftp_src = to_sftp_remote_path(abs_path)
        if has_sftp_glob_chars(abs_path):
            staged_link = get_staging_dir() / f".xdev_pull_src_{key_hash[:20]}_{os.getpid()}_{time.time_ns()}.bin"
            try:
                os.link(target, staged_link)
            except OSError:
                shutil.copy2(target, staged_link)
            sftp_src = to_sftp_remote_path(str(staged_link))

        info = {
            "ok": True,
            "canonical_path": abs_path,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "sha256": full_sha256,
            "prefix_sha256": prefix_sha256,
            "sftp_src_path": sftp_src,
        }
        sys.stdout.write(json.dumps(info, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        _read_stdin_line_with_timeout(session_timeout)
    except FileLayerError as e:
        sys.stdout.write(json.dumps({"ok": False, "error_code": e.error_code, "error_message": e.message, "data": e.data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error_code": ERR_IO_ERROR, "error_message": f"{type(e).__name__}: {e}", "data": {}}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    finally:
        if staged_link is not None and staged_link.exists():
            staged_link.unlink(missing_ok=True)


@functools.lru_cache(maxsize=1)
def get_remote_bootstrap_b64() -> str:
    here = pathlib.Path(__file__).parent
    common_src = (here / "common.py").read_text(encoding="utf-8")
    names_src = (here.parent / "runtime" / "device_names.py").read_text(encoding="utf-8")
    fs_ops_raw = (here / "fs_ops.py").read_text(encoding="utf-8")
    idx_end = fs_ops_raw.find(")\n\n\ndef ensure_not_reparse_or_root")
    fs_ops_body = fs_ops_raw[idx_end + 2 :]

    helper_raw = pathlib.Path(__file__).read_text(encoding="utf-8")
    idx_h = helper_raw.find("def _read_stdin_line_with_timeout")
    idx_h_end = helper_raw.find("@functools.lru_cache")
    helper_body = helper_raw[idx_h:idx_h_end]

    runner = """
import base64, contextlib, fnmatch, queue, shutil, sys, threading
def _remote_main():
    line = _read_stdin_line_with_timeout(30.0)
    if not line:
        return
    req = json.loads(line)
    if req.get("xdev_home"):
        os.environ["XDEV_HOME"] = str(req["xdev_home"])
    op = req.get("op")
    t0 = time.monotonic()
    target_p = req.get("dst") or req.get("path") or req.get("src") or ""
    try:
        default_root = pathlib.Path(os.environ["LOCALAPPDATA"]) / "CrossDeviceCollab"
        cfg = pathlib.Path(os.environ.get("XDEV_HOME") or default_root) / "config.json"
        if not cfg.is_file():
            cfg = default_root / "config.json"
        devices = DeviceDirectory(json.loads(cfg.read_text(encoding="utf-8-sig")) if cfg.is_file() else {}).check_machine()
        expected = req.get("expected_device")
        if not valid_name(expected) or not devices.same(expected, devices.node):
            raise ValueError(f"Expected {expected!r}, reached {devices.node!r}")
    except (ValueError, OSError, KeyError) as error:
        res = make_envelope(ok=False, op=op, host=req.get("expected_device"), target_path=target_p,
                            started_monotonic=t0, error_code="DEVICE_IDENTITY_MISMATCH", error_message=str(error))
        print(json.dumps(res, ensure_ascii=False), flush=True)
        return
    if op == "_push_lock_session":
        run_push_lock_session(req)
        return
    if op == "_pull_source_session":
        run_pull_source_session(req)
        return
    try:
        data = dispatch_target_op(req)
        canon = data.get("canonical_path") or target_p
        res = make_envelope(ok=True, op=op, host=req.get("host", "local"), target_path=canon, started_monotonic=t0, data=data)
    except FileLayerError as e:
        canon = e.data.get("canonical_path") or target_p
        res = make_envelope(ok=False, op=op or "", host=req.get("host", "local"), target_path=canon, started_monotonic=t0, data=e.data, error_code=e.error_code, error_message=e.message)
    except Exception as e:
        res = make_envelope(ok=False, op=op or "", host=req.get("host", "local"), target_path=target_p, started_monotonic=t0, error_code=ERR_IO_ERROR, error_message=f"{type(e).__name__}: {e}")
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
_remote_main()
"""
    combined = "\n".join((common_src, names_src, fs_ops_body, helper_body, runner))
    return base64.b64encode(combined.encode("utf-8")).decode("ascii")


def invoke_target_op(host: str, req: dict[str, Any], timeout_sec: float = 60.0, *, route=None) -> dict[str, Any]:
    t0 = time.monotonic()
    host, ssh = route or transport.route(None, host)
    req = {**req, "host": host, "expected_device": host,
           "xdev_home": req.get("remote_xdev_home") or req.get("xdev_home")}
    target_p = req.get("dst") or req.get("path") or req.get("src") or ""
    if ssh is None:
        try:
            data = dispatch_target_op(req)
            canon = data.get("canonical_path") or target_p
            return make_envelope(ok=True, op=req["op"], host=host, target_path=canon, started_monotonic=t0, data=data)
        except FileLayerError as e:
            canon = e.data.get("canonical_path") or target_p
            return make_envelope(ok=False, op=req["op"], host=host, target_path=canon, started_monotonic=t0, data=e.data, error_code=e.error_code, error_message=e.message)
        except Exception as e:
            return make_envelope(ok=False, op=req["op"], host=host, target_path=target_p, started_monotonic=t0, error_code=ERR_IO_ERROR, error_message=f"{type(e).__name__}: {e}")

    payload_str = get_remote_bootstrap_b64() + "\n" + json.dumps(req, ensure_ascii=False) + "\n"
    cmd = [*SSH_BASE_ARGS, ssh, 'python -X utf8 -c "import sys,base64;exec(base64.b64decode(sys.stdin.readline()).decode())"']
    try:
        # Only the ordinary operation returns one final JSON line. Transfer
        # lock/source sessions above deliberately retain their connections.
        res = _json_request(cmd, payload_str.encode("utf-8"), timeout_sec)
    except (JsonRequestError, OSError) as exc:
        failure = make_envelope(ok=False, op=req["op"], host=host, target_path=target_p,
                                started_monotonic=t0, error_code=ERR_SSH_TRANSPORT_ERROR,
                                error_message=str(exc))
        failure["outcome"] = "unknown"
        return failure
    res["elapsed_ms"] = int(round((time.monotonic() - t0) * 1000.0))
    return res
