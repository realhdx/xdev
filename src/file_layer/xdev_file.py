"""CLI and programmatic entrypoint for the CrossDeviceCollab Windows safe file layer."""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time
from typing import Any, Optional

from src.runtime import transport

from .common import ERR_INVALID_ARGUMENT, ERR_IO_ERROR, FileLayerError, make_envelope
from .remote_fs_helper import invoke_target_op
from .transfer import op_pull, op_push

def run_file_op(payload: dict[str, Any], *, root=None) -> dict[str, Any]:
    """Programmatic entrypoint accepting a structured dict and returning a single-line JSON envelope dict."""
    t0 = time.monotonic()
    op = payload.get("op")
    host = payload.get("host") or "local"
    target_p = payload.get("dst") or payload.get("remote") or payload.get("local") or payload.get("path") or payload.get("src") or ""
    try:
        route = transport.route(root, host)
    except ValueError as error:
        return make_envelope(
            ok=False,
            op=str(op or ""),
            host=str(host),
            target_path=str(target_p),
            started_monotonic=t0,
            error_code=ERR_INVALID_ARGUMENT,
            error_message=str(error),
        )
    if op == "push":
        return op_push(payload, route=route)
    if op == "pull":
        return op_pull(payload, route=route)
    timeout_sec = float(payload.get("timeout_sec") or 60.0)
    return invoke_target_op(host, payload, timeout_sec=timeout_sec, route=route)


def _add_common_flags(p: argparse.ArgumentParser, default_timeout: float = 60.0) -> None:
    p.add_argument("--host", default="local", help="Device name, configured alias, or local")
    p.add_argument("--lock-timeout-sec", type=float, default=10.0, help="Seconds to wait for target file lock")
    p.add_argument("--timeout-sec", type=float, default=default_timeout, help="Overall operation timeout in seconds")
    p.add_argument("--remote-xdev-home", help="Optional XDEV_HOME override on the remote host")


def _add_path_arg(p: argparse.ArgumentParser, help_text: str = "Target path") -> None:
    p.add_argument("--path", dest="opt_path", help=help_text)
    p.add_argument("pos_path", nargs="?", help=help_text)


def _resolve_path(args: argparse.Namespace) -> str:
    val = getattr(args, "opt_path", None) or getattr(args, "pos_path", None)
    if not val:
        raise FileLayerError(ERR_INVALID_ARGUMENT, "Missing required --path argument")
    return val


def _load_content_bytes(args: argparse.Namespace) -> bytes:
    src_file = getattr(args, "content_file", None) or getattr(args, "input_file", None)
    if src_file:
        p = pathlib.Path(src_file)
        if not p.exists():
            raise FileLayerError(ERR_INVALID_ARGUMENT, f"Input content file does not exist: {src_file}")
        return p.read_bytes()
    if getattr(args, "stdin", False):
        return sys.stdin.buffer.read()
    if getattr(args, "content", None) is not None:
        raw_str = args.content
        if getattr(args, "encoding", "utf-8") == "base64":
            return base64.b64decode(raw_str)
        return raw_str.encode("utf-8")
    raise FileLayerError(ERR_INVALID_ARGUMENT, "Provide one of --content, --content-file, --input-file, or --stdin")


def build_parser(parser_class=argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = parser_class(prog="xdev file", description="Windows cross-device safe file operations")
    sub = parser.add_subparsers(dest="op", required=True)

    # stat
    p_stat = sub.add_parser("stat", help="Inspect file or directory metadata and optional SHA-256")
    _add_common_flags(p_stat)
    _add_path_arg(p_stat)
    p_stat.add_argument("--with-sha256", action="store_true", help="Always compute full SHA-256 even for large files")
    p_stat.add_argument("--no-sha256", action="store_true", help="Skip full SHA-256 computation")
    p_stat.add_argument("--prefix-bytes", type=int, help="Compute prefix_sha256 over the first N bytes")

    # ls
    p_ls = sub.add_parser("ls", help="List directory entries")
    _add_common_flags(p_ls)
    _add_path_arg(p_ls)
    p_ls.add_argument("--recursive", action="store_true", help="List entries recursively")
    p_ls.add_argument("--glob", default="*", help="Filter entries by glob pattern")
    p_ls.add_argument("--limit", type=int, default=1000, help="Maximum entries to return")
    p_ls.add_argument("--with-sha256", action="store_true", help="Compute SHA-256 for each file entry")

    # read
    p_read = sub.add_parser("read", help="Read file content or range (ranged reads skip full scan by default)")
    _add_common_flags(p_read)
    _add_path_arg(p_read)
    p_read.add_argument("--offset-bytes", type=int, help="Start reading at byte offset (0-based)")
    p_read.add_argument("--max-bytes", type=int, help="Maximum bytes to read")
    p_read.add_argument("--start-line", type=int, help="Start line (1-based)")
    p_read.add_argument("--end-line", type=int, help="End line (1-based inclusive)")
    p_read.add_argument("--encoding", choices=("utf-8", "base64"), default="utf-8")
    p_read.add_argument("--with-sha256", action="store_true", help="Explicitly compute full file_sha256 even on ranged reads")
    p_read.add_argument("--no-sha256", action="store_true", help="Skip full file_sha256 computation")

    # mkdir
    p_mkdir = sub.add_parser("mkdir", help="Create directory")
    _add_common_flags(p_mkdir)
    _add_path_arg(p_mkdir)
    p_mkdir.add_argument("--no-parents", action="store_true")
    p_mkdir.add_argument("--no-exist-ok", action="store_true")

    # create & write
    for name in ("create", "write"):
        p_w = sub.add_parser(name, help="Atomically create or write a file")
        _add_common_flags(p_w)
        _add_path_arg(p_w)
        p_w.add_argument("--content", help="Inline text or base64 content")
        p_w.add_argument("--content-file", help="Local file whose bytes will be written")
        p_w.add_argument("--input-file", help="Alias for --content-file")
        p_w.add_argument("--stdin", action="store_true", help="Read content bytes from stdin")
        p_w.add_argument("--encoding", choices=("utf-8", "base64"), default="utf-8")
        p_w.add_argument("--base-sha256", help="Expected current SHA-256 of target (or NONE if must not exist)")
        p_w.add_argument("--no-create", action="store_true", help="Fail if target file does not already exist")
        p_w.add_argument("--no-parents", action="store_true", help="Do not auto-create parent directories")

    # edit
    p_edit = sub.add_parser("edit", help="Apply deterministic text replacements under --base-sha256 optimistic lock")
    _add_common_flags(p_edit)
    _add_path_arg(p_edit)
    p_edit.add_argument("--ops-file", help="Path to JSON file containing list of {old, new, count}")
    p_edit.add_argument("--ops-json", help="Inline JSON string containing list of {old, new, count}")
    p_edit.add_argument("--old", help="Single replacement old text")
    p_edit.add_argument("--new", help="Single replacement new text")
    p_edit.add_argument("--count", type=int, default=1, help="Replacement count for --old/--new (default 1)")
    p_edit.add_argument("--base-sha256", help="Mandatory expected SHA-256 before editing")
    p_edit.add_argument("--allow-unlocked", action="store_true", help="Allow editing without --base-sha256")

    # copy, move, rename
    for name in ("copy", "move", "rename"):
        p_cm = sub.add_parser(name, help=f"Safely {name} a file with target locking and hash check")
        _add_common_flags(p_cm)
        p_cm.add_argument("--src", required=True, help="Source path on target host")
        p_cm.add_argument("--dst", required=True, help="Destination path on target host")
        p_cm.add_argument("--base-sha256", help="Expected SHA-256 of destination before overwrite")
        p_cm.add_argument("--no-overwrite", action="store_true", help="Fail with ALREADY_EXISTS if destination exists")

    # remove / delete
    for name in ("remove", "delete"):
        p_rm = sub.add_parser(name, help="Safely remove a single file or empty directory")
        _add_common_flags(p_rm)
        _add_path_arg(p_rm)
        p_rm.add_argument("--base-sha256", help="Expected SHA-256 of file before removal")
        p_rm.add_argument("--missing-ok", action="store_true", help="Succeed if target does not exist")

    # push
    p_push = sub.add_parser("push", help="Upload local file to remote host via independent SFTP + target lock")
    _add_common_flags(p_push, default_timeout=600.0)
    p_push.add_argument("--local", help="Local source file path")
    p_push.add_argument("--src", help="Alias for --local")
    p_push.add_argument("--remote", help="Remote destination file path")
    p_push.add_argument("--dst", help="Alias for --remote")
    p_push.add_argument("--resume", action="store_true", help="Resume from verified .xdev.part if prefix SHA-256 matches")
    p_push.add_argument("--bwlimit-kbps", type=int, help="Limit SFTP bandwidth in Kbit/s")
    p_push.add_argument("--base-sha256", help="Expected SHA-256 of remote destination before publish")
    p_push.add_argument("--no-overwrite", action="store_true", help="Fail if remote destination exists")
    p_push.add_argument("--interrupt-after-bytes", type=int, help="Test hook: abort after transferring N bytes to .xdev.part")

    # pull
    p_pull = sub.add_parser("pull", help="Download remote file to local host via independent SFTP + local lock")
    _add_common_flags(p_pull, default_timeout=600.0)
    p_pull.add_argument("--remote", help="Remote source file path")
    p_pull.add_argument("--src", help="Alias for --remote")
    p_pull.add_argument("--local", help="Local destination file path")
    p_pull.add_argument("--dst", help="Alias for --local")
    p_pull.add_argument("--resume", action="store_true", help="Resume from verified local .xdev.part if prefix SHA-256 matches")
    p_pull.add_argument("--bwlimit-kbps", type=int, help="Limit SFTP bandwidth in Kbit/s")
    p_pull.add_argument("--base-sha256", help="Expected SHA-256 of local destination before publish")
    p_pull.add_argument("--no-overwrite", action="store_true", help="Fail if local destination exists")

    return parser


def parse_args_to_payload(argv: Optional[list[str]] = None, *, parser=None, allow_stdin=True) -> dict[str, Any]:
    parser = parser if parser is not None else build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "stdin", False) and not allow_stdin:
        raise FileLayerError(ERR_INVALID_ARGUMENT, "MCP stdin is reserved for its protocol; use --content or --content-file")
    op = args.op
    payload: dict[str, Any] = {
        "op": op,
        "host": args.host,
        "lock_timeout_sec": args.lock_timeout_sec,
        "timeout_sec": args.timeout_sec,
        "xdev_home": args.remote_xdev_home,
        "remote_xdev_home": args.remote_xdev_home,
    }

    if op == "stat":
        payload.update(
            {
                "path": _resolve_path(args),
                "with_sha256": args.with_sha256,
                "no_sha256": args.no_sha256,
                "prefix_bytes": args.prefix_bytes,
            }
        )
    elif op == "ls":
        payload.update(
            {
                "path": _resolve_path(args),
                "recursive": args.recursive,
                "glob": args.glob,
                "limit": args.limit,
                "with_sha256": args.with_sha256,
            }
        )
    elif op == "read":
        payload.update(
            {
                "path": _resolve_path(args),
                "offset_bytes": args.offset_bytes,
                "max_bytes": args.max_bytes,
                "start_line": args.start_line,
                "end_line": args.end_line,
                "encoding": args.encoding,
                "with_sha256": args.with_sha256,
                "no_sha256": args.no_sha256,
            }
        )
    elif op == "mkdir":
        payload.update(
            {
                "path": _resolve_path(args),
                "parents": not args.no_parents,
                "exist_ok": not args.no_exist_ok,
            }
        )
    elif op in ("create", "write"):
        raw_bytes = _load_content_bytes(args)
        payload.update(
            {
                "path": _resolve_path(args),
                "content_b64": base64.b64encode(raw_bytes).decode("ascii"),
                "base_sha256": args.base_sha256,
                "no_create": args.no_create,
                "parents": not args.no_parents,
            }
        )
    elif op == "edit":
        ops_list: list[dict[str, Any]] = []
        if args.ops_file:
            ops_list = json.loads(pathlib.Path(args.ops_file).read_text(encoding="utf-8-sig"))
        elif args.ops_json:
            ops_list = json.loads(args.ops_json)
        elif args.old is not None and args.new is not None:
            ops_list = [{"old": args.old, "new": args.new, "count": args.count}]
        else:
            raise FileLayerError(ERR_INVALID_ARGUMENT, "edit requires --ops-file, --ops-json, or both --old and --new")
        if isinstance(ops_list, dict):
            ops_list = [ops_list]
        payload.update(
            {
                "path": _resolve_path(args),
                "ops": ops_list,
                "base_sha256": args.base_sha256,
                "allow_unlocked": args.allow_unlocked,
            }
        )
    elif op in ("copy", "move", "rename"):
        payload.update(
            {
                "src": args.src,
                "dst": args.dst,
                "base_sha256": args.base_sha256,
                "no_overwrite": args.no_overwrite,
            }
        )
    elif op in ("remove", "delete"):
        payload.update(
            {
                "path": _resolve_path(args),
                "base_sha256": args.base_sha256,
                "missing_ok": args.missing_ok,
            }
        )
    elif op == "push":
        payload.update(
            {
                "local": args.local or args.src,
                "remote": args.remote or args.dst,
                "resume": args.resume,
                "bwlimit_kbps": args.bwlimit_kbps,
                "base_sha256": args.base_sha256,
                "no_overwrite": args.no_overwrite,
                "interrupt_after_bytes": args.interrupt_after_bytes,
            }
        )
    elif op == "pull":
        payload.update(
            {
                "remote": args.remote or args.src,
                "local": args.local or args.dst,
                "resume": args.resume,
                "bwlimit_kbps": args.bwlimit_kbps,
                "base_sha256": args.base_sha256,
                "no_overwrite": args.no_overwrite,
            }
        )
    return payload


def main(argv: Optional[list[str]] = None, *, parser=None, allow_stdin=True, root=None):
    t0 = time.monotonic()
    try:
        payload = parse_args_to_payload(argv, parser=parser, allow_stdin=allow_stdin)
        res = run_file_op(payload, root=root)
    except FileLayerError as e:
        res = make_envelope(
            ok=False,
            op="",
            host="local",
            target_path="",
            started_monotonic=t0,
            data=e.data,
            error_code=e.error_code,
            error_message=e.message,
        )
    except Exception as e:
        res = make_envelope(
            ok=False,
            op="",
            host="local",
            target_path="",
            started_monotonic=t0,
            error_code=ERR_IO_ERROR,
            error_message=f"{type(e).__name__}: {e}",
        )
    from src.runtime import cli_result
    if cli_result.collecting():
        return res
    sys.stdout.write(json.dumps(res, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
