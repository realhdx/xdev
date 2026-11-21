"""Dispatch a short notice to an explicitly bound, host-supplied local receiver."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from src.runtime import messages, transport


PROTOCOL = "xdev.receiver.v1"
TIMEOUT = 15
MAX_ACK = 8192
_NOT_LAUNCHED = object()


def _receivers(root):
    settings = transport.config(root).get("session_receivers", {})
    if not isinstance(settings, dict):
        raise ValueError("session_receivers must be keyed by client and exact session ID")
    return settings


def _host_receivers(root):
    hosts = transport.config(root).get("session_hosts", {})
    if not isinstance(hosts, dict):
        raise ValueError("session_hosts must map client names to host receiver commands")
    return hosts


def _command(bindings, client, session):
    sessions = bindings.get(client, {})
    if not isinstance(sessions, dict):
        raise ValueError("Each client in session_receivers must be keyed by exact session ID")
    if session not in sessions:
        return None
    return _argv(sessions[session])


def _argv(binding):
    argv = binding.get("argv") if isinstance(binding, dict) else None
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or not all(isinstance(s, str) and "\x00" not in s for s in argv)
            or not Path(argv[0]).is_absolute()
            or (os.name == "nt" and (Path(argv[0]).suffix.lower() != ".exe"
                                     or Path(argv[0]).drive.startswith("\\\\")))):
        raise ValueError("Configure argv with an absolute local executable and fixed arguments")
    return argv


def command(root, client, session):
    exact = _command(_receivers(root), client, session)
    if exact is not None:
        return exact
    hosts = _host_receivers(root)
    return _argv(hosts[client]) if client in hosts and "argv" in hosts[client] else None


def _host(binding):
    if not isinstance(binding, dict) or not ({"resident", "argv"} & binding.keys()):
        raise ValueError("A session host requires a resident endpoint or a fixed local argv")
    if "resident" in binding:
        from src.adapters import resident
        resident.validate(binding["resident"])
    if "argv" in binding:
        _argv(binding)
    return binding


def explicit(root, client, session):
    argv = _command(_receivers(root), client, session)
    if argv is not None:
        return {"argv": argv}
    hosts = _host_receivers(root)
    return _host(hosts[client]) if client in hosts else None


def registered_hosts(root):
    from src.adapters import sessions
    hosts = _host_receivers(root)
    for client, binding in hosts.items():
        if sessions.client_name(client) in sessions.ADAPTERS:
            raise ValueError("Use session_hosts for third-party applications")
        _host(binding)
    return sorted(hosts)


def registered(root):
    from src.adapters import sessions
    result = {}
    bindings = _receivers(root)
    for client, entries in bindings.items():
        sessions.client_name(client)
        if client in sessions.ADAPTERS or not isinstance(entries, dict):
            raise ValueError("session_receivers needs a third-party client and exact sessions")
        result[client] = []
        for session in entries:
            sessions.canonical_session(client, session)
            _command(bindings, client, session)
            result[client].append(session)
    return result


def _invoke(argv, frame, root, *, timeout, maximum):
    env = os.environ.copy()
    env.pop("CODEX_THREAD_ID", None)  # The helper belongs to the receiving host.
    env["XDEV_HOME"] = str(root.resolve())
    wire = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
    # A host may keep a child process alive. Redirect output to a file so an
    # inherited stdout handle cannot extend our wait beyond the receiver deadline.
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                    env=env, cwd=root.resolve(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (FileNotFoundError, PermissionError):
            return _NOT_LAUNCHED
        with proc:
            try:
                proc.communicate(input=wire, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
        output.seek(0)
        raw = output.read(maximum + 1)
    if proc.returncode or len(raw) > maximum:
        return None
    return json.loads(raw.decode("utf-8"))


def _matches_notice(ack, notice):
    return isinstance(ack, dict) and ack.get("status") in ("accepted", "requires_activation", "unknown") and all(
        ack.get(k) == notice[k] for k in ("protocol", "client", "session", "message_id", "notice_id"))


def _exchange(argv, notice, root):
    ack = _invoke(argv, notice, root, timeout=TIMEOUT, maximum=MAX_ACK)
    if ack is _NOT_LAUNCHED:
        return {"status":"unavailable_before_submit"}
    if _matches_notice(ack, notice):
        return ack
    return None


def _dispatch(root, client, session, notice, binding):
    from src.adapters import host_catalog
    if binding is not None:
        if "resident" in binding:
            from src.adapters import resident
            ack = resident.exchange(binding["resident"], notice)
            if not isinstance(ack, dict) or ack.get("status") != "unavailable_before_submit":
                return ack if _matches_notice(ack, notice) else None
        if "argv" in binding:
            return _exchange(_argv(binding), notice, root)
    discovery, route = host_catalog.discover(root, client, session)
    if route is None:
        if binding is not None and not discovery["providers"]:
            return {"status": "unavailable_before_submit"}
        status = discovery["status"]
        return {"status": "no_matching_host", "discovery_status": status}
    return host_catalog.submit(root, route, notice)


def _admission(placement, duplicate):
    if placement == "unknown":
        return {"ok": False, "status": "unknown", "outcome": "unknown",
                "error_code": "PORTABLE_RECEIVER_ACK_UNVERIFIED"}
    if placement == "next_turn":
        return {"ok": True, "status": "already_queued" if duplicate else "queued",
                "host_placement": placement, "admission_evidence": "portable_host_ack"}
    return {"ok": True, "status": "already_admitted" if duplicate else "admitted",
            "host_placement": placement, "admission_evidence": "portable_host_ack"}


def submit(root, client, session, item, identifier, *, queue):
    endpoint = {"device": transport.node(root), "client": client, "session": session}
    result = {"backend": "portable_local_receiver", "client": client, "session": session,
              "id": identifier, "model_status": "unverified", "delivery_mode": "queue" if queue else "send_now"}
    intended = item.get("recipient_identity") if item else None
    if (not intended or not messages.matches_participant(intended, endpoint, exact=True, root=root)
            or (item.get("recipient_endpoint") or {}).get("channel") == "tool_result"):
        return {**result, "ok": False, "status": "target_mismatch", "error_code": "SESSION_TARGET_MISMATCH"}
    # Stable across alias changes and retries. One committed inbox ID identifies
    # immutable content on this device. Host-side dedup can use the same notice ID.
    key = json.dumps([PROTOCOL, client, session, identifier], separators=(",", ":"))
    notice_id = "wake-xdev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    result["notice_id"] = notice_id
    notice = {**endpoint, "protocol": PROTOCOL, "message_id": identifier, "notice_id": notice_id,
              "mode": result["delivery_mode"], "subject": item.get("subject"),
              "sender": item.get("sender_endpoint") or {"device": item["from"]}}
    state = root / "state"
    state.mkdir(exist_ok=True, parents=True)
    with closing(sqlite3.connect(state / "portable-notices.sqlite3", timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, placement TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("SELECT placement FROM notices WHERE id=?", (notice_id,)).fetchone()
            if record is not None:
                return {**result, **_admission(record[0], True), "duplicate": True}
            if item.get("read_at") is not None:
                return {**result, "ok": True, "status": "already_read", "read_at": item["read_at"]}
            # A malformed local grant cannot have submitted this notice.
            binding = explicit(root, client, session)
            if binding is None:
                from src.adapters import host_catalog
                host_catalog.sources(root, client)
            db.execute("INSERT INTO notices VALUES (?, 'unknown')", (notice_id,))
        # An exception or restart after reservation may follow an actual wake.
        # Only a proven pre-launch failure or an explicit non-admission permits a retry.
        try:
            ack = _dispatch(root, client, session, notice, binding)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            ack = None
        if ack and ack.get("status") in {"requires_activation","unavailable_before_submit",
                                          "no_matching_host","placement_unavailable"}:
            with db:
                db.execute("DELETE FROM notices WHERE id=?", (notice_id,))
            if ack["status"]=="no_matching_host":
                return {**result, "ok":False, "status":"requires_receiver_binding",
                        "discovery_status":ack["discovery_status"], "error_code":"SESSION_RECEIVER_NOT_REGISTERED"}
            if ack["status"]=="placement_unavailable":
                return {**result, "ok":False, "status":"unsupported", "submitted":False,
                        "error_code":"HOST_PLACEMENT_UNAVAILABLE"}
            return {**result, "ok": False, "status": "requires_activation",
                    "error_code": "PORTABLE_HOST_UNAVAILABLE"}
        placement = ack.get("placement") if ack and ack.get("status") == "accepted" else None
        if placement not in ("current_turn", "next_turn", "original_session"):
            return {**result, **_admission("unknown", False)}
        with db:
            db.execute("UPDATE notices SET placement=? WHERE id=?", (placement, notice_id))
        return {**result, **_admission(placement, False)}
