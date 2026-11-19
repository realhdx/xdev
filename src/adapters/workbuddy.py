"""Exact-session WorkBuddy notices through its loaded native function-hook host.

The adapter never starts an independent CLI session or copies gateway/provider
credentials. A durable request file is only a transport request. Native prompt
admission is a separate receipt written by the plugin in that original session.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import UUID

from src.runtime import local_ipc, messages


SCHEMA = "xdev.workbuddy-bridge.v1"
FRESH_SECONDS = 15
ADMISSION_TIMEOUT = 10


def write_plugin_bundle(root):
    """Materialize an immutable local marketplace from release-packaged assets."""
    assets = Path(__file__).with_name("workbuddy_plugin")
    mapping = {"plugin/.codebuddy-plugin/plugin.json": "plugin.json",
               "plugin/package.json": "package.json", "plugin/hooks/hooks.json": "hooks.json",
               "plugin/hooks/receiver.js": "receiver.js"}
    payloads = {name: (assets / source).read_bytes() for name, source in mapping.items()}
    marketplace = {"name": "xdev-local", "owner": {"name": "CrossDeviceCollab local integration"},
                   "plugins": [{"name": "xdev-session-receiver", "source": "./plugin", "version": json.loads(payloads["plugin/package.json"])["version"],
                                "description": "Attributed exact-session xdev receiver with native admission receipts."}]}
    payloads[".codebuddy-plugin/marketplace.json"] = json.dumps(marketplace, sort_keys=True).encode()
    digest = sha256()
    for name, raw in sorted(payloads.items()):
        digest.update(name.encode() + b"\0" + raw + b"\0")
    version = digest.hexdigest()
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.is_junction():
        raise ValueError("Use the absolute, unlinked xdev installation root")
    destination = root / "integrations" / "workbuddy" / version / "marketplace"
    for name, raw in payloads.items():
        path = destination / name
        for ancestor in [path, destination, *path.parents]:
            if ancestor == root.parent:
                break
            if ancestor.is_symlink() or ancestor.is_junction():
                raise ValueError("WorkBuddy bundle paths must not be linked")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(raw)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("Existing WorkBuddy bundle differs from its version hash")
    hashes = {name: sha256((destination / name).read_bytes()).hexdigest() for name in payloads}
    return {"ok": True, "bundle_hash": version, "marketplace": str(destination), "files": hashes,
            "installed_in_host": False, "evidence_scope": "packaged_files_only"}


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError("Invalid WorkBuddy bridge document")
    return json.loads(path.read_text(encoding="utf-8"))


def _result(receipt, admission, already=False):
    if (admission.get("schema") != SCHEMA or admission.get("session_id") != receipt["session"]
            or admission.get("message_id") != receipt["message_id"]
            or admission.get("notice_id") != receipt["notice_id"]):
        return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                "error_code": "WORKBUDDY_RECEIPT_MISMATCH"}
    state = admission.get("state")
    if state == "input_surface_accepted":
        return {**receipt, "ok": True, "status": "input_surface_accepted", "outcome": "unknown",
                "already_observed": already, "session_admission": "unverified",
                "admission_evidence": "native_input_surface_ack",
                "native_observed_at_ms": admission.get("observed_at_ms")}
    if state in {"rejected", "not_submitted"}:
        return {**receipt, "ok": False, "status": "rejected", "error_code": "WORKBUDDY_NATIVE_REJECTED",
                "native_reason": admission.get("reason")}
    return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
            "error_code": "WORKBUDDY_ADMISSION_UNVERIFIED", "native_state": state}


def _submit_legacy(root, identifier, session, steer=False, idle_only=False):
    if steer or idle_only:
        raise ValueError("WorkBuddy supports native queue delivery only")
    try:
        canonical = str(UUID(session))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Supply the UUID of the existing WorkBuddy chat") from exc
    if canonical != session.lower():
        raise ValueError("Supply the canonical UUID of the existing WorkBuddy chat")
    session = canonical
    query = messages.handle(root, "wait", {"id": identifier, "timeout": 0})
    item = query.get("message")
    if query.get("status") != "received" or not item:
        raise ValueError("This message is not in the local inbox")
    notice_id = "wake-xdev-" + sha256(f"{session}\n{identifier}".encode()).hexdigest()[:24]
    receipt = {"client": "workbuddy", "session": session, "message_id": identifier,
               "notice_id": notice_id, "model_status": "unverified"}
    base = Path(root) / "state" / "workbuddy-sessions" / session
    admission = base / "admissions" / f"{notice_id}.json"
    request = base / "requests" / f"{notice_id}.json"
    if admission.exists():
        try:
            observed = local_ipc.read_response(lambda: _read(admission),
                                               deadline=time.monotonic() + ADMISSION_TIMEOUT,
                                               stage="workbuddy.receipt_read")
            return _result(receipt, observed, already=True)
        except (OSError, ValueError, TypeError) as error:
            return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                    "error_code": "WORKBUDDY_RECEIPT_UNREADABLE",
                    **local_ipc.diagnostics(error, stage="workbuddy.receipt_read")}
    try:
        bridge = _read(base / "bridge.json")
        age = time.time() - bridge.get("observed_at_ms", 0) / 1000
        ready = (bridge.get("schema") == SCHEMA and bridge.get("session_id") == session
                 and bridge.get("state") == "loaded" and -5 <= age <= FRESH_SECONDS
                 and not base.is_symlink() and not base.parent.is_symlink())
    except (OSError, ValueError, TypeError):
        ready = False
    if not ready:
        return {**receipt, "ok": False, "status": "requires_activation", "requires_activation": True,
                "gui_required": True, "error_code": "WORKBUDDY_PLUGIN_NOT_ACTIVE",
                "error_message": "Load xdev-session-receiver in the selected existing WorkBuddy chat; no notice was submitted."}
    from src.adapters.notices import body as notice_body
    notice = f"[xdev notice {notice_id}, from {json.dumps(item['from'], ensure_ascii=False)}] " + notice_body(item, identifier, "workbuddy", session)
    # The v1 JS plugin counts UTF-16 code units and requires the literal ID.
    if identifier not in notice or len(identifier.encode("utf-16-le")) > 512 or len(notice.encode("utf-16-le")) > 2400:
        if request.exists():
            return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                    "error_code": "WORKBUDDY_PREVIOUS_REQUEST_UNVERIFIED"}
        return {**receipt, "ok": False, "status": "unsupported_notice", "outcome": "not_submitted",
                "error_code": "WORKBUDDY_RECEIVER_NOTICE_UNSUPPORTED",
                "error_message": "The v1 receiver requires the literal message ID and accepts at most 1200 UTF-16 units. The mailbox retains the full envelope."}
    payload = {"schema": SCHEMA, "session_id": session, "message_id": identifier,
               "notice_id": notice_id, "notice": notice}
    # Exclusive create reserves an attempt across sender processes. A partial
    # write can only remain unverified; the sender never rewrites an old attempt.
    try:
        with request.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        try:
            if _read(request) != payload:
                return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                        "error_code": "WORKBUDDY_REQUEST_CONFLICT"}
        except (OSError, ValueError, TypeError):
            return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                    "error_code": "WORKBUDDY_REQUEST_UNREADABLE"}
    deadline = time.monotonic() + ADMISSION_TIMEOUT
    last_read_error = None
    while time.monotonic() < deadline:
        if admission.exists():
            try:
                observed = local_ipc.read_response(lambda: _read(admission), deadline=deadline,
                                                   stage="workbuddy.receipt_read")
                result = _result(receipt, observed)
                if result.get("native_state") != "dispatching":
                    return result
            except (OSError, ValueError, TypeError) as error:
                last_read_error = error
        time.sleep(0.15)
    return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
            "error_code": "WORKBUDDY_ADMISSION_TIMEOUT",
            "error_message": "The original host has not confirmed admission. Check this notice ID before retrying; it will not be rewritten.",
            **(local_ipc.diagnostics(last_read_error, stage="workbuddy.receipt_read") if last_read_error else {})}


def submit(root, identifier, session, steer=False, idle_only=False):
    from src.adapters import official_channel
    with official_channel.delivery_lock(root, identifier, session, client="workbuddy"):
        previous = official_channel.channel_prior(root, identifier, session, client="workbuddy")
        if previous is not None:
            return {**previous, "backend_switch_blocked": True}
        return _submit_legacy(root, identifier, session, steer, idle_only)
