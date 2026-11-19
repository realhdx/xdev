"""Independent SFTP push/pull with end-to-end target lock, prefix-verified resume, and atomic commit."""

from __future__ import annotations

import json
import os
import pathlib
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Optional

from src.runtime import transport

from .common import (
    ERR_INVALID_ARGUMENT,
    ERR_IO_ERROR,
    ERR_NOT_A_FILE,
    ERR_PATH_NOT_FOUND,
    ERR_POST_TRANSFER_HASH_MISMATCH,
    ERR_RESUME_PREFIX_MISMATCH,
    ERR_SSH_TRANSPORT_ERROR,
    FileLayerError,
    TargetFileLock,
    compute_sha256,
    get_canonical_part_path,
    get_staging_dir,
    has_sftp_glob_chars,
    make_envelope,
    path_lock_key,
)
from .fs_ops import check_base_sha256, ensure_not_reparse_or_root
from .remote_fs_helper import (
    SSH_BASE_ARGS,
    _publish_part_to_target,
    get_remote_bootstrap_b64,
)

SFTP_BASE_ARGS = [
    "sftp",
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


def _run_sftp_batch(
    host: str,
    batch_line: str,
    bwlimit_kbps: Optional[int] = None,
    timeout_sec: float = 600.0,
) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sftp", delete=False) as bf:
        bf.write(batch_line + "\n")
        batch_path = pathlib.Path(bf.name)
    try:
        args = list(SFTP_BASE_ARGS)
        if bwlimit_kbps is not None and int(bwlimit_kbps) > 0:
            args.extend(["-l", str(int(bwlimit_kbps))])
        args.extend(["-b", str(batch_path), host])
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_sec)
        if proc.returncode != 0:
            err_txt = proc.stderr.decode("utf-8", errors="replace").strip()
            out_txt = proc.stdout.decode("utf-8", errors="replace").strip()
            raise FileLayerError(
                ERR_SSH_TRANSPORT_ERROR,
                f"sftp failed (rc={proc.returncode}): {err_txt or out_txt}",
                {"returncode": proc.returncode},
            )
    except subprocess.TimeoutExpired as e:
        raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"sftp timed out after {timeout_sec}s: {e}")
    finally:
        batch_path.unlink(missing_ok=True)


def _open_remote_helper_session(host: str, req: dict[str, Any]) -> subprocess.Popen[str]:
    cmd = [
        *SSH_BASE_ARGS,
        host,
        'python -X utf8 -c "import sys,base64;exec(base64.b64decode(sys.stdin.readline()).decode())"',
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(get_remote_bootstrap_b64() + "\n")
        proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        proc.stdin.flush()
    except Exception:
        _close_helper_proc(proc)
        raise
    return proc


def _readline_with_timeout(proc: subprocess.Popen[str], timeout_sec: float, phase_label: str) -> str:
    """Read a single line from proc.stdout with a strict timeout; kills proc if stalled."""
    q_out: queue.Queue[tuple[Optional[str], Optional[BaseException]]] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            q_out.put((proc.stdout.readline(), None))
        except BaseException as exc:
            q_out.put((None, exc))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        line, err = q_out.get(timeout=max(1.0, float(timeout_sec)))
    except queue.Empty:
        try:
            proc.kill()
        except OSError:
            pass
        raise FileLayerError(
            ERR_SSH_TRANSPORT_ERROR,
            f"Remote helper session timed out after {timeout_sec:.1f}s waiting for {phase_label}",
        )
    if err is not None:
        raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"Remote helper stdout read failed ({phase_label}): {err}")
    return line or ""


def _close_helper_proc(proc: subprocess.Popen[str], send_line: Optional[str] = None) -> None:
    """Cleanly close stdin/stdout/stderr and reap the helper process without leaking descriptors."""
    if send_line is not None:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(send_line)
                proc.stdin.flush()
        except OSError:
            pass
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=3.0)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3.0)
        except Exception:
            pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream and not stream.closed:
                stream.close()
        except OSError:
            pass


def op_push(req: dict[str, Any], *, route=None) -> dict[str, Any]:
    host, ssh = route or transport.route(None, req.get("host") or "local")
    t0 = time.monotonic()
    local_raw = req.get("local") or req.get("src") or ""
    remote_raw = req.get("remote") or req.get("dst") or ""
    if not local_raw or not remote_raw:
        return make_envelope(
            ok=False,
            op="push",
            host=host,
            target_path=remote_raw,
            started_monotonic=t0,
            error_code=ERR_INVALID_ARGUMENT,
            error_message="push requires --local and --remote",
        )

    local_abs, _, local_key = path_lock_key(local_raw)
    local_p = pathlib.Path(local_abs)
    try:
        ensure_not_reparse_or_root(local_p, local_abs)
        if not local_p.exists():
            raise FileLayerError(ERR_PATH_NOT_FOUND, f"Local file not found: {local_abs}", {"local_path": local_abs})
        if not local_p.is_file():
            raise FileLayerError(ERR_NOT_A_FILE, f"Local path is not a file: {local_abs}", {"local_path": local_abs})

        local_size = local_p.stat().st_size
        source_sha256 = compute_sha256(local_p)
        resume = bool(req.get("resume", False))
        no_overwrite = bool(req.get("no_overwrite", False))
        base_sha256 = req.get("base_sha256")
        bwlimit_kbps = req.get("bwlimit_kbps")
        lock_timeout = float(req.get("lock_timeout_sec") or 10.0)
        timeout_sec = float(req.get("timeout_sec") or 600.0)
        interrupt_bytes = req.get("interrupt_after_bytes")

        if ssh is None:
            remote_abs, _, rem_key = path_lock_key(remote_raw)
            remote_p = pathlib.Path(remote_abs)
            with TargetFileLock(remote_abs, timeout_sec=lock_timeout):
                remote_p.parent.mkdir(parents=True, exist_ok=True)
                before_sha256 = check_base_sha256(remote_p, remote_abs, base_sha256, no_overwrite=no_overwrite)
                part_path = get_canonical_part_path(remote_abs)
                part_path.parent.mkdir(parents=True, exist_ok=True)
                if not resume and part_path.exists():
                    part_path.unlink(missing_ok=True)
                resumed_from = 0
                if resume and part_path.exists():
                    part_size = part_path.stat().st_size
                    if part_size > 0:
                        part_sha = compute_sha256(part_path)
                        prefix_sha = compute_sha256(local_p, part_size) if part_size <= local_size else None
                        if part_size > local_size or part_sha != prefix_sha:
                            raise FileLayerError(
                                ERR_RESUME_PREFIX_MISMATCH,
                                f"Resume prefix hash mismatch on {part_path}",
                                {"part_size": part_size, "part_sha256": part_sha, "expected_prefix_sha256": prefix_sha, "part_path": str(part_path)},
                            )
                        resumed_from = part_size
                if interrupt_bytes is not None and 0 < int(interrupt_bytes) < local_size:
                    with open(local_p, "rb") as sf, open(part_path, "wb") as df:
                        df.write(sf.read(int(interrupt_bytes)))
                    raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"Simulated interruption after {interrupt_bytes} bytes", {"part_path": str(part_path)})
                with open(local_p, "rb") as sf, open(part_path, "ab" if resumed_from > 0 else "wb") as df:
                    sf.seek(resumed_from)
                    shutil.copyfileobj(sf, df)
                if compute_sha256(part_path) != source_sha256:
                    raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, "Local push staging hash mismatch")
                _publish_part_to_target(part_path, remote_p, remote_abs, source_sha256)
                st = remote_p.stat()
                return make_envelope(
                    ok=True,
                    op="push",
                    host=host,
                    target_path=remote_abs,
                    started_monotonic=t0,
                    data={
                        "local_canonical_path": local_abs,
                        "canonical_path": remote_abs,
                        "lock_key": rem_key,
                        "before_sha256": before_sha256,
                        "after_sha256": source_sha256,
                        "source_sha256": source_sha256,
                        "size": st.st_size,
                        "resumed_from_bytes": resumed_from,
                        "part_path": str(part_path),
                    },
                )

        handshake_timeout = max(30.0, lock_timeout + 25.0)
        session_req = {
            "op": "_push_lock_session",
            "expected_device": host,
            "path": remote_raw,
            "resume": resume,
            "no_overwrite": no_overwrite,
            "base_sha256": base_sha256,
            "lock_timeout_sec": lock_timeout,
            "session_timeout_sec": timeout_sec + 30.0,
            "xdev_home": req.get("remote_xdev_home") or req.get("xdev_home"),
        }
        proc = _open_remote_helper_session(ssh, session_req)
        staged_local_src: Optional[pathlib.Path] = None
        try:
            assert proc.stdin is not None
            line1 = _readline_with_timeout(proc, handshake_timeout, "push lock phase1")
            if not line1:
                err_out = proc.stderr.read() if proc.stderr else ""
                raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"Remote lock session failed to start on {host}: {err_out.strip()}")
            phase1 = json.loads(line1)
            if not phase1.get("ok"):
                raise FileLayerError(phase1.get("error_code") or ERR_IO_ERROR, phase1.get("error_message") or "Remote lock error", phase1.get("data"))

            remote_abs = phase1["canonical_path"]
            sftp_part_path = phase1["sftp_part_path"]
            part_path_str = phase1["part_path"]
            part_size = int(phase1.get("part_size") or 0)
            part_sha256 = phase1.get("part_sha256")
            resumed_from = 0

            if resume and phase1.get("part_exists") and part_size > 0:
                prefix_sha = compute_sha256(local_p, part_size) if part_size <= local_size else None
                if part_size > local_size or part_sha256 != prefix_sha:
                    proc.stdin.write(json.dumps({"cmd": "abort", "keep_part": True}) + "\n")
                    proc.stdin.flush()
                    raise FileLayerError(
                        ERR_RESUME_PREFIX_MISMATCH,
                        f"Resume prefix mismatch on {remote_abs}: remote part ({part_size}B) sha256={part_sha256}, local prefix sha256={prefix_sha}",
                        {
                            "canonical_path": remote_abs,
                            "part_path": part_path_str,
                            "part_size": part_size,
                            "part_sha256": part_sha256,
                            "expected_prefix_sha256": prefix_sha,
                        },
                    )
                resumed_from = part_size

            sftp_local_path = local_p
            if interrupt_bytes is not None and 0 < int(interrupt_bytes) < local_size:
                staged_local_src = get_staging_dir() / f".xdev_partial_{local_key[:16]}_{os.getpid()}_{time.time_ns()}.bin"
                with open(local_p, "rb") as sf, open(staged_local_src, "wb") as df:
                    df.write(sf.read(int(interrupt_bytes)))
                sftp_local_path = staged_local_src
            elif has_sftp_glob_chars(local_abs):
                staged_local_src = get_staging_dir() / f".xdev_push_src_{local_key[:16]}_{os.getpid()}_{time.time_ns()}.bin"
                try:
                    os.link(local_p, staged_local_src)
                except OSError:
                    shutil.copy2(local_p, staged_local_src)
                sftp_local_path = staged_local_src

            q = '"'
            sftp_verb = "reput" if (resumed_from > 0 and interrupt_bytes is None) else "put"
            if resumed_from < local_size or local_size == 0:
                _run_sftp_batch(ssh, f"{sftp_verb} {q}{sftp_local_path.as_posix()}{q} {q}{sftp_part_path}{q}", bwlimit_kbps, timeout_sec)

            if interrupt_bytes is not None and 0 < int(interrupt_bytes) < local_size:
                proc.stdin.write(json.dumps({"cmd": "abort", "keep_part": True}) + "\n")
                proc.stdin.flush()
                raise FileLayerError(
                    ERR_SSH_TRANSPORT_ERROR,
                    f"Simulated interruption after {interrupt_bytes} bytes",
                    {"canonical_path": remote_abs, "part_path": part_path_str, "bytes_transferred": int(interrupt_bytes)},
                )

            proc.stdin.write(json.dumps({"cmd": "commit", "expected_sha256": source_sha256, "expected_size": local_size}) + "\n")
            proc.stdin.flush()
            line2 = _readline_with_timeout(proc, max(60.0, timeout_sec), "push commit phase2")
            if not line2:
                raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, "Remote lock session closed before commit confirmation")
            phase2 = json.loads(line2)
            if not phase2.get("ok"):
                raise FileLayerError(phase2.get("error_code") or ERR_IO_ERROR, phase2.get("error_message") or "Commit failed", phase2.get("data"))

            return make_envelope(
                ok=True,
                op="push",
                host=host,
                target_path=remote_abs,
                started_monotonic=t0,
                data={
                    "local_canonical_path": local_abs,
                    "canonical_path": remote_abs,
                    "lock_key": phase2["lock_key"],
                    "before_sha256": phase2["before_sha256"],
                    "after_sha256": phase2["after_sha256"],
                    "source_sha256": source_sha256,
                    "size": phase2["size"],
                    "mtime_ns": phase2["mtime_ns"],
                    "resumed_from_bytes": resumed_from,
                    "part_path": part_path_str,
                },
            )
        finally:
            if staged_local_src is not None and staged_local_src.exists():
                staged_local_src.unlink(missing_ok=True)
            _close_helper_proc(proc)
    except FileLayerError as e:
        return make_envelope(
            ok=False,
            op="push",
            host=host,
            target_path=e.data.get("canonical_path") or remote_raw,
            started_monotonic=t0,
            data=e.data,
            error_code=e.error_code,
            error_message=e.message,
        )


def op_pull(req: dict[str, Any], *, route=None) -> dict[str, Any]:
    host, ssh = route or transport.route(None, req.get("host") or "local")
    t0 = time.monotonic()
    remote_raw = req.get("remote") or req.get("src") or ""
    local_raw = req.get("local") or req.get("dst") or ""
    if not remote_raw or not local_raw:
        return make_envelope(
            ok=False,
            op="pull",
            host=host,
            target_path=local_raw,
            started_monotonic=t0,
            error_code=ERR_INVALID_ARGUMENT,
            error_message="pull requires --remote and --local",
        )

    local_abs, _, local_key = path_lock_key(local_raw)
    local_p = pathlib.Path(local_abs)
    resume = bool(req.get("resume", False))
    no_overwrite = bool(req.get("no_overwrite", False))
    base_sha256 = req.get("base_sha256")
    bwlimit_kbps = req.get("bwlimit_kbps")
    lock_timeout = float(req.get("lock_timeout_sec") or 10.0)
    timeout_sec = float(req.get("timeout_sec") or 600.0)

    try:
        with TargetFileLock(local_abs, timeout_sec=lock_timeout):
            local_p.parent.mkdir(parents=True, exist_ok=True)
            before_sha256 = check_base_sha256(local_p, local_abs, base_sha256, no_overwrite=no_overwrite)
            part_path = get_canonical_part_path(local_abs)
            part_path.parent.mkdir(parents=True, exist_ok=True)
            if not resume and part_path.exists():
                part_path.unlink(missing_ok=True)

            part_exists = part_path.exists()
            part_size = part_path.stat().st_size if part_exists else 0
            part_sha256 = compute_sha256(part_path) if (resume and part_exists and part_size > 0) else None

            if ssh is None:
                rem_abs, _, _ = path_lock_key(remote_raw)
                rem_p = pathlib.Path(rem_abs)
                if not rem_p.exists():
                    raise FileLayerError(ERR_PATH_NOT_FOUND, f"Source not found: {rem_abs}", {"remote_path": rem_abs})
                if not rem_p.is_file():
                    raise FileLayerError(ERR_NOT_A_FILE, f"Source is not a file: {rem_abs}", {"remote_path": rem_abs})
                rem_size = rem_p.stat().st_size
                rem_sha256 = compute_sha256(rem_p)
                resumed_from = 0
                if resume and part_size > 0:
                    prefix_sha = compute_sha256(rem_p, part_size) if part_size <= rem_size else None
                    if part_size > rem_size or part_sha256 != prefix_sha:
                        raise FileLayerError(
                            ERR_RESUME_PREFIX_MISMATCH,
                            f"Resume prefix mismatch on {part_path}",
                            {"part_size": part_size, "part_sha256": part_sha256, "expected_prefix_sha256": prefix_sha, "part_path": str(part_path)},
                        )
                    resumed_from = part_size
                with open(rem_p, "rb") as sf, open(part_path, "ab" if resumed_from > 0 else "wb") as df:
                    sf.seek(resumed_from)
                    shutil.copyfileobj(sf, df)
                if compute_sha256(part_path) != rem_sha256:
                    raise FileLayerError(ERR_POST_TRANSFER_HASH_MISMATCH, "Local pull hash mismatch")
                _publish_part_to_target(part_path, local_p, local_abs, rem_sha256)
                st = local_p.stat()
                return make_envelope(
                    ok=True,
                    op="pull",
                    host=host,
                    target_path=local_abs,
                    started_monotonic=t0,
                    data={
                        "remote_canonical_path": rem_abs,
                        "canonical_path": local_abs,
                        "lock_key": local_key,
                        "before_sha256": before_sha256,
                        "after_sha256": rem_sha256,
                        "source_sha256": rem_sha256,
                        "size": st.st_size,
                        "resumed_from_bytes": resumed_from,
                        "part_path": str(part_path),
                    },
                )

            pull_req = {
                "op": "_pull_source_session",
                "expected_device": host,
                "path": remote_raw,
                "prefix_bytes": part_size if (resume and part_size > 0) else None,
                "session_timeout_sec": timeout_sec + 30.0,
                "xdev_home": req.get("remote_xdev_home") or req.get("xdev_home"),
            }
            proc = _open_remote_helper_session(ssh, pull_req)
            try:
                line1 = _readline_with_timeout(proc, max(60.0, timeout_sec), "pull source phase1")
                if not line1:
                    err_out = proc.stderr.read() if proc.stderr else ""
                    raise FileLayerError(ERR_SSH_TRANSPORT_ERROR, f"Remote pull session failed on {host}: {err_out.strip()}")
                info = json.loads(line1)
                if not info.get("ok"):
                    raise FileLayerError(info.get("error_code") or ERR_IO_ERROR, info.get("error_message") or "Remote source error", info.get("data"))

                rem_abs = info["canonical_path"]
                rem_size = int(info["size"])
                rem_sha256 = info["sha256"]
                rem_prefix_sha = info.get("prefix_sha256")
                sftp_src_path = info["sftp_src_path"]
                resumed_from = 0

                if resume and part_size > 0:
                    if part_size > rem_size or part_sha256 != rem_prefix_sha:
                        raise FileLayerError(
                            ERR_RESUME_PREFIX_MISMATCH,
                            f"Resume prefix mismatch on {local_abs}: local part ({part_size}B) sha256={part_sha256}, remote prefix sha256={rem_prefix_sha}",
                            {
                                "canonical_path": local_abs,
                                "remote_canonical_path": rem_abs,
                                "part_path": str(part_path),
                                "part_size": part_size,
                                "part_sha256": part_sha256,
                                "expected_prefix_sha256": rem_prefix_sha,
                            },
                        )
                    resumed_from = part_size

                q = '"'
                sftp_verb = "reget" if resumed_from > 0 else "get"
                if resumed_from < rem_size or rem_size == 0:
                    _run_sftp_batch(ssh, f"{sftp_verb} {q}{sftp_src_path}{q} {q}{part_path.as_posix()}{q}", bwlimit_kbps, timeout_sec)
            finally:
                _close_helper_proc(proc, send_line="done\n")

            actual_size = part_path.stat().st_size if part_path.exists() else -1
            actual_sha256 = compute_sha256(part_path) if part_path.exists() else None
            if actual_size != rem_size or actual_sha256 != rem_sha256:
                raise FileLayerError(
                    ERR_POST_TRANSFER_HASH_MISMATCH,
                    f"Post-download hash mismatch on {part_path}: expected {rem_sha256} ({rem_size}B), got {actual_sha256} ({actual_size}B)",
                    {"expected_sha256": rem_sha256, "actual_sha256": actual_sha256, "part_path": str(part_path)},
                )

            check_base_sha256(local_p, local_abs, base_sha256, no_overwrite=no_overwrite)
            _publish_part_to_target(part_path, local_p, local_abs, actual_sha256)
            st = local_p.stat()
            return make_envelope(
                ok=True,
                op="pull",
                host=host,
                target_path=local_abs,
                started_monotonic=t0,
                data={
                    "remote_canonical_path": rem_abs,
                    "canonical_path": local_abs,
                    "lock_key": local_key,
                    "before_sha256": before_sha256,
                    "after_sha256": actual_sha256,
                    "source_sha256": rem_sha256,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "resumed_from_bytes": resumed_from,
                    "part_path": str(part_path),
                },
            )
    except FileLayerError as e:
        return make_envelope(
            ok=False,
            op="pull",
            host=host,
            target_path=e.data.get("canonical_path") or local_raw,
            started_monotonic=t0,
            data=e.data,
            error_code=e.error_code,
            error_message=e.message,
        )
