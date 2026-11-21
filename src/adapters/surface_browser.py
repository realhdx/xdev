"""Address a shared tab through the installed browse extension."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4


_GROUP = "xdev"
_MAX_OUTPUT = 512 * 1024
_MAX_SNAPSHOT = 24000
_REF = re.compile(r"(?:e|(?:f[0-9]+)*a)[0-9]{1,10}\Z")


class BrowseError(Exception):
    def __init__(self, status, unknown=False):
        super().__init__(status)
        self.status = status
        self.unknown = unknown


def _call(tool, options, *, script=None, writing=False):
    # Prefer the user's standard installation independently of IDE PATH.
    name = "browse.exe" if os.name == "nt" else "browse"
    installed = Path.home() / ".local" / "bin" / name
    executable = str(installed) if installed.is_file() else shutil.which(name)
    if not executable:
        raise BrowseError("browse_not_installed")
    argv = [executable, tool]
    if script is None:
        argv += ["--json", json.dumps(options, ensure_ascii=True)]
    else:
        argv += ["--tab", str(options["tab"]), "--file", "-"]
    try:
        # The relay may outlive the CLI and inherit its standard handles. A pipe
        # would then keep a finished command waiting for EOF from the relay.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                argv, input=script if script is not None else b"",
                stdout=output, stderr=subprocess.DEVNULL, timeout=22,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output.seek(0)
            raw = output.read(_MAX_OUTPUT + 1)
    except subprocess.TimeoutExpired:
        raise BrowseError("browse_timeout", writing) from None
    except OSError:
        raise BrowseError("browse_unavailable") from None
    if result.returncode != 0:
        detail = raw[:4096].lower()
        if b"unknown aria snapshot ref" in detail or b"unknown ref" in detail:
            raise BrowseError("stale_ref")
        if b"not connected" in detail or b"no browser" in detail:
            raise BrowseError("browser_disconnected")
        raise BrowseError("browse_command_failed", writing)
    if len(raw) > _MAX_OUTPUT:
        raise BrowseError("browse_output_too_large", writing)
    return raw.decode("utf-8", "replace").strip()


def _json(tool, options, *, writing=False):
    raw = _call(tool, options, writing=writing)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BrowseError("unrecognized_browse_result", writing) from None
    if not isinstance(payload, dict) or payload.get("error") or payload.get("isError"):
        raise BrowseError("unrecognized_browse_result", writing)
    return payload


def _origin(url):
    try:
        if not isinstance(url, str) or len(url) > 8192 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
            return None
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        _ = parts.port  # reject malformed or out-of-range ports
        return f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return None


def _targets(requested=None):
    rows = _json("tabs", {"action": "list"}).get("tabs")
    if not isinstance(rows, list):
        raise BrowseError("unrecognized_browse_result")
    occurrences = Counter(str(row.get("tab")) for row in rows if isinstance(row, dict))
    result = {}
    for row in rows:
        if not isinstance(row, dict) or (row.get("lent") is not True and row.get("group") != _GROUP):
            continue
        tab = str(row.get("tab", ""))
        if not re.fullmatch(r"[0-9]{1,16}", tab):
            continue
        if occurrences[tab] > 1:
            if tab == requested:
                raise BrowseError("ambiguous_tab_id")
            continue
        title = row.get("title")
        # Some pages set their full URL as the title.
        title = re.sub(r"https?://\S+", lambda m: _origin(m[0]) or "", title, flags=re.I) if isinstance(title, str) else ""
        # While loading, Edge can use the address *without the scheme* as its title.
        title = title.partition("?")[0] if "?" in str(row.get("url", "")) and "?" in title else title
        result[tab] = {"target": tab, "title": title[:160], "origin": _origin(row.get("url")),
                       "lent": row.get("lent") is True}
        if isinstance(row.get("id"), str) and re.fullmatch(r"xdev-[a-f0-9]{32}", row["id"]):
            result[tab]["tab_name"] = row["id"]
    return result


def _fill(tab, ref, value):
    if not isinstance(value, str) or len(value) > 4096:
        raise BrowseError("invalid_value")
    # One fixed operation in the relay, with data encoded as JSON literals.
    # The value travels on stdin, never in the process command line or result.
    ref_literal = json.dumps(ref)
    args = json.dumps({"tab": tab, "ref": ref, "value": value}, ensure_ascii=True)
    body = (
        f"const field=ref({ref_literal});"
        "const type=await field.getAttribute('type');"
        "const hint=await field.getAttribute('autocomplete');"
        "if (String(type).toLowerCase()==='password' || "
        "/(?:password|one-time-code|cc-number|cc-csc)/i.test(String(hint))) return 'sensitive_field';"
        f"const result=await browse.fill({args});"
        "return result.filled && result.filled.length===1 ? 'filled' : 'unconfirmed';"
    )
    raw = _call("execute", {"tab": tab}, script=body.encode("ascii"), writing=True)
    lines = raw.splitlines()
    if lines and lines[-1] == "sensitive_field":
        raise BrowseError("sensitive_field")
    if not lines or lines[-1] != "filled":
        raise BrowseError("action_unconfirmed", True)


def run(action, target=None, ref=None, value=None, app=None):
    """Read or act on an explicitly borrowed or xdev-opened browser tab."""
    try:
        if action == "targets":
            return {"ok": True, "targets": list(_targets().values())}
        if action == "open":
            origin = _origin(value)
            if not origin or (app is not None and (not isinstance(app, str) or not 1 <= len(app) <= 128)):
                raise BrowseError("invalid_browser_or_url")
            name = "xdev-" + uuid4().hex
            options = {"action": "new", "id": name, "group": _GROUP, "allowedOrigins": [origin]}
            if app:
                options["browser"] = app
            # Acquire a handle first so a load timeout cannot lose a newly opened tab.
            try:
                created = _json("tabs", options, writing=True)
            except BrowseError as exc:
                if exc.unknown:
                    return {"ok": False, "status": exc.status, "outcome": "unknown", "tab_name": name}
                raise
            tab = str(created.get("tab", ""))
            if not re.fullmatch(r"[0-9]{1,16}", tab):
                return {"ok": False, "status": "open_unconfirmed", "outcome": "unknown", "tab_name": name}
            try:
                page = _json("navigate", {"tab": name, "url": value, "waitUntil": "commit",
                                          "timeout": 7000}, writing=True)
            except BrowseError as exc:
                return {"ok": False, "status": exc.status, "outcome": "unknown",
                        "target": tab, "tab_name": name}
            status = page.get("status")
            if isinstance(status, int) and status >= 400:
                return {"ok": False, "status": "http_error", "http_status": status, "target": tab}
            return {"ok": True, "status": "opened_pending" if page.get("pending") else "opened", "target": tab}
        if action not in {"snapshot", "tools", "invoke", "set-value", "close"}:
            raise BrowseError("unsupported_action")
        if not isinstance(target, (int, str)) or not re.fullmatch(r"[0-9]{1,16}", str(target)):
            raise BrowseError("invalid_target")
        tab = str(target)
        if tab not in _targets(tab):
            raise BrowseError("tab_not_available")
        options = {"tab": int(tab)}
        if action == "tools":
            catalog = _json("webmcp", {**options, "action": "list", "timeout": 5000})
            rows = catalog.get("tools")
            if not isinstance(rows, list) or type(catalog.get("available")) is not bool:
                raise BrowseError("unrecognized_webmcp_catalog")
            tools = []
            for row in rows[:128]:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    continue
                schema = row.get("inputSchema")
                fields = schema.get("properties") if isinstance(schema, dict) else None
                tools.append({"name": row["name"][:128],
                              "input_fields": sorted(str(key)[:128] for key in fields)[:64] if isinstance(fields, dict) else []})
            return {"ok": True, "status": "catalog_read", "target": tab, "available": catalog["available"],
                    "tools": tools, "truncated": len(rows) > 128, "evidence": "page_declared_tools_only"}
        if action == "snapshot":
            raw = _call("snapshot", {**options, "mode": "aria", "frames": False, "maxItems": 120, "timeout": 8000})
            tree = re.sub(r"(?m)^(\s*-\s*/url:).*$", r"\1 [omitted]", raw)
            return {"ok": True, "target": tab, "snapshot": tree[:_MAX_SNAPSHOT],
                    "truncated": len(tree) > _MAX_SNAPSHOT}
        if action == "close":
            result = _json("tabs", {**options, "action": "close"}, writing=True)
            if not any(isinstance(result.get(k), list) and int(tab) in result[k] for k in ("closed", "returned", "released")):
                raise BrowseError("close_unconfirmed", True)
            return {"ok": True, "target": tab, "status": "closed_or_returned"}
        if not isinstance(ref, str) or len(ref) > 64 or not _REF.fullmatch(ref):
            raise BrowseError("invalid_ref")
        if action == "invoke":
            result = _json("click", {**options, "ref": ref, "timeout": 8000}, writing=True)
            if not result.get("clicked"):
                raise BrowseError("action_unconfirmed", True)
        else:
            _fill(int(tab), ref, value)
        return {"ok": True, "status": "invoked" if action == "invoke" else "value_set", "target": tab, "ref": ref}
    except BrowseError as exc:
        result = {"ok": False, "status": exc.status}
        if exc.unknown:
            result["outcome"] = "unknown"
        return result
