"""Run the loaded release's CLI dispatcher without spawning another CLI.

Only cooperative emitters are collected. No global stream redirection,
monkeypatching, environment changes, or version/module hot swapping occurs.
"""

import argparse
from pathlib import Path
import re

from src.runtime import cli_result, transport


class CLIArgumentError(ValueError):
    pass


class CLIHelpRequested(Exception):
    def __init__(self, text):
        self.text = text


class QuietArgumentParser(argparse.ArgumentParser):
    """Argparse's normal validation with no writes to the MCP protocol streams."""

    def error(self, message):
        raise CLIArgumentError(message)

    def exit(self, status=0, message=None):
        if status == 0:
            raise CLIHelpRequested(self.format_help())
        raise CLIArgumentError(message or "Invalid CLI arguments")

    def print_help(self, file=None):
        # argparse's help action calls this and then exit(). exit carries help.
        return None

    def print_usage(self, file=None):
        return None

    def _print_message(self, message, file=None):
        # Defensive: all argparse diagnostics must be returned as structured data.
        return None


def runtime_metadata():
    source = Path(__file__).resolve().parents[2]
    from src.cli import main as dispatcher
    dispatcher_source = str(Path(dispatcher.__file__).resolve())
    pinned = source.name if source.parent.name == "releases" and re.fullmatch(r"[0-9a-f]{64}", source.name) else None
    active = None
    try:
        with (transport.home() / "current.txt").open(encoding="ascii") as stream:
            # Pointer format is active on line one, rollback release on line two.
            raw = stream.readline(256).rstrip("\r\n")
        if re.fullmatch(r"[0-9a-f]{64}", raw):
            active = raw
    except (OSError, UnicodeError, ValueError, RuntimeError):
        pass
    return {"dispatch": "in_process", "source_root": str(source), "dispatcher_source": dispatcher_source, "pinned_release": pinned,
            "active_release": active, "reload_required": active is not None and active != pinned,
            "version_policy": "loaded_modules_remain_pinned; reconnect_MCP_after_release_change"}


def _parser(group=None):
    if group == "file":
        from src.file_layer import xdev_file
        return xdev_file.build_parser(parser_class=QuietArgumentParser)
    from src.cli import main
    return main._parser(parser_class=QuietArgumentParser)


def help_text(group="file", operation=""):
    """Format the requested parser directly; no parser exit or child process."""
    if group not in {"file", "exec", "job", "msg", "ai"}:
        raise CLIArgumentError("Unknown tool group")
    if not isinstance(operation, str) or operation.startswith("-"):
        raise CLIArgumentError("Invalid help operation")
    parser = _parser("file" if group == "file" else None)
    names = ([operation] if operation else []) if group == "file" else [group, *([operation] if operation else [])]
    for name in names:
        action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if action is None or name not in action.choices:
            raise CLIArgumentError("Unknown help operation")
        parser = action.choices[name]
    return parser.format_help()


def invoke(argv, root=None):
    """Return one structured CLI result, leaving stdin/stdout/stderr untouched."""
    from src.runtime.device_names import UnknownDeviceError
    metadata = runtime_metadata()
    try:
        if not isinstance(argv, (list, tuple)) or not all(isinstance(x, str) for x in argv):
            raise CLIArgumentError("CLI arguments must be a list of strings")
        argv = list(argv)
        if not argv or argv[0] not in {"who", "exec", "job", "msg", "ai", "file"}:
            raise CLIArgumentError("Unsupported MCP dispatcher operation")
        selected_root = Path(root) if root is not None else transport.home()
        with cli_result.capture(), transport.routing_snapshot(selected_root):
            if argv[0] == "file":
                from src.file_layer import xdev_file
                parser = _parser("file")
                # Preflight parse before file main's generic exception envelope.
                parsed = parser.parse_args(argv[1:])
                if getattr(parsed, "stdin", False):
                    raise CLIArgumentError("MCP stdin is reserved for its protocol; use --content or --content-file")
                result = xdev_file.main(argv[1:], parser=parser, allow_stdin=False, root=selected_root)
            else:
                from src.cli import main
                parser = _parser()
                parsed = parser.parse_args(argv)
                if parsed.service == "msg" and getattr(parsed, "stdin", False):
                    raise CLIArgumentError("MCP stdin is reserved for its protocol; use --text or --text-file")
                result = main._parse_and_run(selected_root, argv, parser=parser)
        if not isinstance(result, dict):
            return {"ok": False, "error_code": "CLI_RESULT_NOT_STRUCTURED", "_mcp_runtime": metadata}
        return {**result, "_mcp_runtime": metadata}
    except CLIHelpRequested as exc:
        return {"ok": True, "help": exc.text, "_mcp_runtime": metadata}
    except (CLIArgumentError, UnknownDeviceError) as exc:
        return {"ok": False, "error_code": "CLI_ARGUMENT_ERROR", "error_message": str(exc), "_mcp_runtime": metadata}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "error_code": type(exc).__name__, "error_message": str(exc), "_mcp_runtime": metadata}
