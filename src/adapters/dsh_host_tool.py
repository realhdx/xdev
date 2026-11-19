"""One isolated dispatch for a DSH native tool call. No credentials or model API."""

import json
import os
from pathlib import Path
import re
import site
import sys


def dispatch(request, root):
    from src.adapters import caller_context, inprocess_cli

    if not isinstance(request, dict) or set(request) != {"session", "action", "arguments"}:
        raise ValueError("Invalid native tool request")
    action, arguments = request["action"], request["arguments"]
    if action not in ("send", "forward", "reply", "read", "whoami", "inbox", "list", "wait", "receipt", "bind"):
        raise ValueError("Invalid mailbox action")
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise ValueError("Pass each CLI argument as a separate string")
    with caller_context.bind_dsh(request["session"]):
        return inprocess_cli.invoke(["msg", action, *arguments], root=root)


def main():
    # This file is addressed by the installed release, just as the shared
    # stdio server stays pinned until its configuration is refreshed.
    release = Path(__file__).resolve().parents[2]
    root = release.parent.parent
    if release.parent.name != "releases" or not re.fullmatch(r"[0-9a-f]{64}", release.name):
        raise ValueError("Expected an installed xdev release")
    os.environ["XDEV_HOME"] = str(root)
    if (root / "deps").is_dir():
        site.addsitedir(str(root / "deps"))
    sys.path.insert(0, str(release))
    frame = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    if len(frame) > 2 * 1024 * 1024:
        raise ValueError("The mailbox request is too large")
    return dispatch(json.loads(frame), root)


if __name__ == "__main__":
    try:
        result = main()
    except (OSError, ValueError, RuntimeError) as error:
        result = {"ok": False, "error_code": "DSH_MAILBOX_ERROR", "error_message": str(error)}
    print(json.dumps(result, ensure_ascii=False), flush=True)
