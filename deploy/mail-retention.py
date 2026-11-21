"""Local inbox retention and paired GitHub release maintenance."""

import argparse
import atexit
import json
from pathlib import Path
import runpy
import site
import sqlite3
import sys
import tempfile

KIT = Path(__file__).resolve().parents[1]


def runtime():
    if (KIT / "src/runtime/mail_retention.py").is_file():
        return KIT
    # The distribution does not contain an importable namespace package tree.
    # Use its normal digest and path verifier to read the included runtime.
    deploy = runpy.run_path(str(KIT / "deploy/deploy.py"))
    archive = KIT / "runtime.zip"
    release = json.loads((KIT / "release.json").read_bytes())["runtime_sha256"]
    if deploy["digest"](archive) != release:
        raise ValueError("The kit runtime checksum differs from release.json")
    temporary = tempfile.TemporaryDirectory(prefix="xdev-retention-")
    atexit.register(temporary.cleanup)
    location = Path(temporary.name)
    deploy["extract"](archive, location, release)
    return location


def main():
    sys.path.insert(0, str(runtime()))
    from src.runtime import github_capacity, mail_retention, transport

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=transport.home(),
        help="Existing local xdev data root",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("usage", "compact", "github-status", "github-sweep"):
        commands.add_parser(action)
    sub = commands.add_parser("folder-usage")
    sub.add_argument("--directory", type=Path, required=True)
    for action in (
        "protect",
        "unprotect",
        "mark-processed",
        "export",
        "archive",
        "restore",
        "verify",
        "extract",
    ):
        sub = commands.add_parser(action)
        sub.add_argument("id")
        if action in ("archive", "export", "restore", "verify", "extract"):
            sub.add_argument(
                "--archive",
                type=Path,
                required=True,
                help="Absolute private archive file",
            )
        if action == "extract":
            sub.add_argument(
                "--source-device",
                required=True,
                help="Device name at the time of export",
            )
            sub.add_argument(
                "--output", type=Path, required=True, help="New absolute JSON file"
            )
    for action in ("plan", "sweep"):
        sub = commands.add_parser(action)
        sub.add_argument("--archive", required=True, type=Path)
        sub.add_argument("--keep-days", type=int)
        sub.add_argument("--max-live-bytes", type=int)
        sub.add_argument("--limit", type=int, default=25)
    options = parser.parse_args()
    root = options.root.resolve()
    action = options.action
    for name in ("archive", "output"):
        dest = getattr(options, name, None)
        if dest is not None and Path(dest).resolve().is_relative_to(KIT):
            raise ValueError(
                "Keep private mail archives and extracts outside the product or source directory"
            )
    if action == "extract":
        return mail_retention.extract(
            options.archive, options.source_device, options.id, options.output
        )
    if action == "folder-usage":
        return mail_retention.folder_usage(options.directory)
    if action in ("usage", "compact"):
        return getattr(mail_retention, action)(root)
    if action in ("plan", "sweep"):
        return mail_retention.policy(
            root,
            options.archive,
            options.keep_days,
            options.max_live_bytes,
            options.limit,
            apply=action == "sweep",
        )
    if action.startswith("github-"):
        if action == "github-sweep":
            if (root / "deps").is_dir():
                site.addsitedir(str(root / "deps"))
            from src.runtime import github_mail

            github_mail._sweep_sent(github_mail.Api(root), github_mail.Settings(root))
        return github_capacity.cleanup_status(root)
    return mail_retention.record(
        root, options.id, action, getattr(options, "archive", None)
    )


if __name__ == "__main__":
    try:
        result = main()
        print(json.dumps(result, ensure_ascii=True))
        sys.exit(1 if result.get("status") == "partial" else 0)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(
            json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=True),
            file=sys.stderr,
        )
        sys.exit(1)
