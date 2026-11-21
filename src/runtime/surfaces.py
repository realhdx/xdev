"""Semantic views into an explicitly selected browser tab or application window."""

from src.runtime import transport


PROVIDERS = ("browser", "windows")
READ_ACTIONS = ("targets", "snapshot", "tools")
ACTIONS = (*READ_ACTIONS, "open", "invoke", "set-value", "close")
ACTION_HELP = {
    "targets": "List available tabs or windows and their target IDs.",
    "tools": "Read the public WebMCP catalog offered by this browser tab without calling a tool.",
    "snapshot": "Read the current view. A loading screen can appear before the expected content.",
    "open": "Open a browser tab once. If opened_pending, wait briefly and snapshot the returned target.",
    "invoke": "Activate a referenced control once, then snapshot the same target to check the result.",
    "set-value": "Fill a referenced control once, then check for visible application feedback.",
    "close": "Close or return a browser tab. If the outcome is unknown, inspect targets.",
}
FIRST_USE = ("The first call can take a few seconds. Let it finish. A new tab or a changed view may "
             "still be loading. Follow the returned next_call; it keeps the same host and target. "
             "If the expected content is still missing, wait briefly and read once more. "
             "If still missing, report what you saw. Do not replay an action whose result is unknown.")


def add_parser(sub):
    surface = sub.add_parser("surface", help="Read or operate a named tab or window by element reference",
                             description=FIRST_USE)
    actions = surface.add_subparsers(dest="action", required=True)
    for action in ACTIONS:
        parser = actions.add_parser(action, help=ACTION_HELP[action], description=ACTION_HELP[action],
                                    epilog=FIRST_USE)
        providers = ("browser",) if action in ("open", "close", "tools") else PROVIDERS
        parser.add_argument("--provider", required=True, choices=providers)
        parser.add_argument("--host", default="local", help="Local or a paired, opted-in Windows host")
        if action in ("targets", "open"):
            parser.add_argument("--app", help="Process name for Windows discovery, or browser for opening a tab")
        if action not in ("targets", "open"):
            parser.add_argument("--target", required=True, help="Exact tab or window returned by the provider")
        if action in ("invoke", "set-value"):
            parser.add_argument("--ref", required=True, help="Element reference from the target snapshot")
        if action in ("open", "set-value"):
            parser.add_argument("--value", required=True, help="Web URL to open" if action == "open" else "Plain field text")


def guide(result, action, provider, host, target):
    """Attach a read-only next call using the address as known to the caller."""
    status = result.get("status") or result.get("error_code", "")
    follow = "snapshot"
    delay = 0
    target = result.get("target") or target
    if action == "open" and result.get("ok"):
        if status == "opened_pending":
            delay = 2
            hint = "The tab is open and still loading. After a short wait, read this same tab."
        else:
            hint = "Read this tab and check for the content you need; the page may still be rendering."
    elif result.get("outcome") == "unknown" and action not in READ_ACTIONS:
        if action == "close" or not target:
            follow = "targets"
            hint = "Check the available targets before acting again."
            if result.get("tab_name"):
                hint += " Match the returned tab_name to recover the tab."
        else:
            hint = "The action may already have taken effect. Check the same target before acting again."
        delay = 2
    elif action in ("invoke", "set-value") and result.get("ok"):
        hint = "The action was dispatched. Read this target for the expected change or confirmation."
    elif status in ("stale_ref", "STALE_REF"):
        hint = "Read this target again to obtain its current element references."
    elif action == "snapshot" and target and (
            status in ("browse_timeout", "browse_command_failed", "UI_TIMEOUT", "SSH_TIMEOUT")
            or (result.get("ok") and ((provider == "browser" and not result.get("snapshot", "").strip())
                                     or (provider == "windows" and not result.get("nodes"))))):
        delay = 2
        hint = "If the view is still loading, wait briefly and read it once more. If unchanged, report what you observed."
    else:
        return result
    if follow == "snapshot" and not target:
        return result
    next_call = {"action": follow, "provider": provider, "host": host}
    if follow == "snapshot":
        next_call["target"] = str(target)
    return {**result, "guidance": hint, "next_call": next_call,
            **({"suggested_wait_seconds": delay} if delay else {})}


def handle(root, action, payload, *, remote=False):
    provider = payload.get("provider")
    if provider not in PROVIDERS or action not in ACTIONS or (action == "tools" and provider != "browser"):
        raise ValueError("Unknown surface provider or action")
    if remote:
        # The SSH account is the trust boundary. No peer-provided identity or
        # value in the request can opt a receiver in to UI access.
        settings = transport.config(root).get("surface_remote", {})
        mode = settings.get(provider) if isinstance(settings, dict) else None
        if mode != "interact" and not (mode == "read" and action in READ_ACTIONS):
            return {"ok": False, "error_code": "SURFACE_REMOTE_DISABLED",
                    "error_message": "The target has not enabled this level of remote surface access."}
    fields = {
        "targets": ("app",) if provider == "windows" else (),
        "open": ("value", "app"), "snapshot": ("target",), "tools": ("target",), "close": ("target",),
        "invoke": ("target", "ref"), "set-value": ("target", "ref", "value"),
    }[action]
    if provider == "windows" and action in ("open", "close"):
        raise ValueError("Opening and closing targets is supported by the browser provider only")
    if set(payload) - {"provider", *fields}:
        raise ValueError("Unexpected options for this surface operation")
    values = {key: payload[key] for key in fields if payload.get(key) is not None}
    for key in fields:
        if key == "app" and key not in values:
            continue
        value = values.get(key)
        if not isinstance(value, str) or len(value) > 8192 or "\0" in value or (key != "value" and not value):
            raise ValueError(f"Invalid surface option: {key}")
    if provider == "browser":
        from src.adapters import surface_browser as backend
    else:
        from src.adapters import surface_windows as backend
    result = backend.run(action, **values)
    return {"ok": True, **result, "provider": provider, "action": action}
