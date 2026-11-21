"""Semantic access to a selected window on the current Windows input desktop."""

import json
import os
from pathlib import Path
import re
import subprocess


def run(action, target=None, ref=None, value=None, app=None):
    """Return structured UI Automation data. No user input is evaluated as script."""
    if action not in ("targets", "snapshot", "invoke", "set-value"):
        return {"ok": False, "error_code": "UNKNOWN_ACTION"}
    mutates = action in ("invoke", "set-value")
    if action != "targets" and (not isinstance(target, str) or not re.fullmatch(r"win(?::[0-9]{1,19}){4}", target)):
        return {"ok": False, "error_code": "INVALID_TARGET"}
    if mutates and (not isinstance(ref, str) or not re.fullmatch(r"u:[0-9a-f]{64}", ref)):
        return {"ok": False, "error_code": "INVALID_REF"}
    if action == "set-value" and (not isinstance(value, str) or len(value) > 2048 or "\0" in value):
        return {"ok": False, "error_code": "INVALID_VALUE"}
    if app is not None and (action != "targets" or not isinstance(app, str) or not 1 <= len(app) <= 160):
        return {"ok": False, "error_code": "INVALID_APP_FILTER"}
    if os.name != "nt":
        return {"ok": False, "error_code": "WINDOWS_REQUIRED"}
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = Path(__file__).with_suffix(".ps1")
    request = {"action": action, "target": target, "ref": ref, "value": value, "app": app}
    try:
        reply = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
            input=json.dumps(request, ensure_ascii=True).encode("ascii"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=18, creationflags=subprocess.CREATE_NO_WINDOW, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "UI_TIMEOUT", "outcome": "unknown" if mutates else "timeout"}
    except OSError:
        return {"ok": False, "error_code": "UI_PROVIDER_UNAVAILABLE"}
    if reply.returncode and b"UnauthorizedAccess" in reply.stderr:
        return {"ok": False, "error_code": "UI_SCRIPT_POLICY",
                "message": "Windows PowerShell denied the helper. Check its installation location and signing policy."}
    try:
        result = json.loads(reply.stdout.decode("utf-8-sig"))
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            return result
    except (ValueError, UnicodeError):
        pass
    return {"ok": False, "error_code": "UI_PROVIDER_FAILED", "outcome": "unknown" if mutates else "failed"}
