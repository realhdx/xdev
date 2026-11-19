"""Self-registered Codex host receiver with durable, at-most-once submissions.

Only this process's host-published pipe and a per-call declared chat may register.
The declaration is routing metadata, never human authority. Host ACK, explicit
mailbox read receipt, and model execution remain separate evidence layers.
"""

from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import socket
import sqlite3
import stat
import sys
from uuid import UUID

from src.adapters import caller_context, codex_native
from src.adapters.claude import _process
from src.runtime import messages


def _thread(value):
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Supply the canonical UUID of the existing Codex chat") from exc
    if canonical != value.lower():
        raise ValueError("Supply the canonical UUID of the existing Codex chat")
    return canonical


def _now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _private(path, *, directory=False):
    """Protect only our dedicated receiver directory/files, never its parents."""
    if sys.platform != "win32":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    import ctypes
    from ctypes import wintypes
    from src.adapters.dsh import _windows_user_sid

    sid = _windows_user_sid()
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    inheritance = "OICI" if directory else ""
    sddl = f"D:P(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;{sid})"
    descriptor = ctypes.c_void_p()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not security.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Codex receiver state must be an unlinked regular file")


@contextmanager
def _database(root):
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Codex receiver requires an absolute local state root")
    directory = root / "state" / "codex-receiver"
    for ancestor in [directory, *directory.parents]:
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("Codex receiver state must not use linked directories")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(directory, directory=True)
    path = directory / "receiver.sqlite3"
    if path.exists():
        _regular(path)
        _private(path)
    with closing(sqlite3.connect(path, timeout=10)) as db:
        _regular(path)
        _private(path)
        db.row_factory = sqlite3.Row
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS receivers (
                thread TEXT PRIMARY KEY, pipe_path TEXT NOT NULL, host_pid INTEGER NOT NULL,
                host_started_at TEXT NOT NULL, host_domain TEXT NOT NULL,
                registered_at TEXT NOT NULL, source TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                thread TEXT NOT NULL, message_id TEXT NOT NULL, notice_id TEXT NOT NULL,
                notice_hash TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT NOT NULL,
                result_json TEXT, PRIMARY KEY(thread, message_id))""")
        yield db


def _binding(row):
    return {key: row[key] for key in ("pipe_path", "host_pid", "host_started_at")}


def _live_host(binding):
    process = _process(binding["host_pid"])
    image = PureWindowsPath(process["image"]) if process else None
    return bool(process and process["same_user"] and process["start"] == binding["host_started_at"]
                and image.name.lower() in {"chatgpt.exe", "codex.exe"}
                and any(part.lower().startswith("openai.codex_") for part in image.parts))


def _unavailable(code="CODEX_NATIVE_RECEIVER_NOT_REGISTERED"):
    return {"ok": False, "status": "not_registered", "client": "codex",
            "native_receiver_available": False, "error_code": code, "model_status": "unverified",
            "delivery_mode": "send_now"}


def register(root, thread, *, source="mcp_host_declaration"):
    """Publish our own verified host for an exact, host-declared current chat."""
    thread = _thread(thread)
    if source not in {"mcp_host_declaration", "own_cli_host"}:
        raise ValueError("Unsupported Codex receiver registration source")
    caller = caller_context.current()
    if caller.get("status") != "ready" or caller.get("client") != "codex" or caller.get("session") != thread:
        return _unavailable("CODEX_NATIVE_CALLER_META_REQUIRED")
    if not codex_native.environment_available():
        return _unavailable("CODEX_NATIVE_OWN_HOST_REQUIRED")
    try:
        binding = codex_native.describe_host(timeout=3)
        pipe, pid, started = (binding.get(key) for key in ("pipe_path", "host_pid", "host_started_at"))
        if (not isinstance(pipe, str) or not pipe.startswith("\\\\.\\pipe\\") or "\0" in pipe
                or len(pipe) > 1024 or type(pid) is not int or pid <= 0
                or not isinstance(started, str) or not started or not _live_host(binding)):
            return _unavailable("CODEX_NATIVE_HOST_UNVERIFIED")
        with _database(root) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM receivers WHERE thread=?", (thread,)).fetchone()
            if (previous is not None and previous["host_domain"] == socket.gethostname().lower()
                    and _binding(previous) != binding and _live_host(_binding(previous))):
                return _unavailable("CODEX_NATIVE_LIVE_HOST_CONFLICT")
            with db:
                db.execute("""INSERT INTO receivers VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread) DO UPDATE SET pipe_path=excluded.pipe_path,
                    host_pid=excluded.host_pid, host_started_at=excluded.host_started_at,
                    host_domain=excluded.host_domain, registered_at=excluded.registered_at, source=excluded.source
                    """, (thread, pipe, pid, started, socket.gethostname().lower(), _now(), source))
    except (OSError, ValueError, sqlite3.Error, codex_native.NativeBridgeError):
        return _unavailable("CODEX_NATIVE_REGISTRATION_UNVERIFIED")
    return {"ok": True, "status": "registered", "client": "codex", "session": thread,
            "backend": "registered_native_send_now", "source": source,
            "delivery_mode": "send_now",
            "evidence": "own_host_verified_and_current_chat_declared",
            "authority": "host_routing_declaration_not_user_authorization",
            "model_status": "unverified"}


def _unknown(receipt, code):
    return {"ok": False, "status": "unknown", "outcome": "unknown", **receipt,
            "error_code": code, "model_status": "unverified", "read_status": "unverified",
            "next_action": "Check this message ID and native notice in the exact chat; do not resubmit."}


def _previous(row, receipt, notice_hash):
    if row["notice_hash"] != notice_hash:
        return _unknown(receipt, "CODEX_NATIVE_MESSAGE_ID_CONFLICT")
    if row["status"] == "admitted":
        return {"ok": True, "status": "already_admitted", **receipt,
                "admission_evidence": "native_send_now_accepted", "model_status": "unverified",
                "read_status": "unverified"}
    return _unknown(receipt, "CODEX_NATIVE_PREVIOUS_ATTEMPT_UNVERIFIED")


def _native_payload(raw):
    if not isinstance(raw, dict) or raw.get("success") is not True:
        return None
    items = raw.get("contentItems")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    if item.get("type") != "inputText" or not isinstance(item.get("text"), str) or len(item["text"]) > 65536:
        return None
    try:
        value = json.loads(item["text"])
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _acknowledged(raw, thread):
    value = _native_payload(raw)
    return value is not None and value.get("threadId") == thread


def _requires_binding(code):
    return {"ok": False, "status": "requires_receiver_binding", "error_code": code,
            "client": "codex", "delivery_mode": "send_now", "send_attempted": False,
            "model_status": "unverified", "read_status": "unverified"}


def _select_host(db, thread):
    domain = socket.gethostname().lower()
    registered = db.execute("SELECT * FROM receivers WHERE thread=?", (thread,)).fetchone()
    if registered is not None and registered["host_domain"] == domain and _live_host(_binding(registered)):
        return {"binding": _binding(registered), "source_thread": thread, "gateway": False}
    # Same Host may have several genuinely self-declared chats. They count as one
    # transport, while distinct live Hosts are ambiguous and must never be guessed.
    hosts = {}
    for row in db.execute("SELECT * FROM receivers WHERE host_domain=? ORDER BY thread", (domain,)):
        binding = _binding(row)
        if _live_host(binding):
            key = tuple(binding.values())
            hosts.setdefault(key, {"binding": binding, "source_thread": row["thread"], "gateway": True})
    if not hosts:
        return _unavailable()
    if len(hosts) != 1:
        return _requires_binding("CODEX_NATIVE_MULTIPLE_LIVE_HOSTS")
    return next(iter(hosts.values()))


def _notice_inputs(targetthread, shortnotice, msgid):
    thread = _thread(targetthread)
    if not isinstance(msgid, str) or not msgid.strip() or len(msgid) > 128:
        raise ValueError("Supply a nonblank stored message ID of at most 128 characters")
    if not isinstance(shortnotice, str) or not shortnotice.strip() or len(shortnotice) > 4096:
        raise ValueError("Codex native notices must contain 1-4096 characters")
    notice_id = "wake-xdev-" + sha256(f"{thread}\n{msgid}".encode()).hexdigest()[:24]
    receipt = {"client": "codex", "session": thread, "message_id": msgid, "notice_id": notice_id,
               "backend": "registered_native_send_now", "delivery_mode": "send_now"}
    return thread, receipt, sha256(shortnotice.encode("utf-8")).hexdigest()


def peek(root, session, msgid, notice):
    """Inspect a prior native attempt without submitting or checking host liveness.

    The shared Codex dispatcher uses this before selecting another backend, so an
    explicit queue request cannot repeat an already admitted or unknown native send.
    """
    thread, receipt, notice_hash = _notice_inputs(session, notice, msgid)
    try:
        with _database(root) as db:
            row = db.execute("SELECT * FROM notifications WHERE thread=? AND message_id=?",
                             (thread, msgid)).fetchone()
        return _previous(row, receipt, notice_hash) if row is not None else None
    except (OSError, ValueError, sqlite3.Error):
        return _unknown(receipt, "CODEX_NATIVE_LEDGER_UNVERIFIED")


def notify(root, targetthread, shortnotice, msgid):
    """Submit once through the selected registered host; unknown never retries."""
    thread, receipt, notice_hash = _notice_inputs(targetthread, shortnotice, msgid)
    item = messages.handle(root, "wait", {"id": msgid, "timeout": 0}).get("message")
    if item is None:
        raise ValueError("This message is not in the local inbox")
    if item["kind"] == "receipt":
        return {"ok": True, "status": "receipt_suppressed", **receipt,
                "model_status": "unverified", "read_status": "unverified"}
    try:
        with _database(root) as db:
            # The durable ID ledger is checked before host liveness, so a stale or
            # re-registered host cannot make an unknown submission eligible again.
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM notifications WHERE thread=? AND message_id=?",
                                  (thread, msgid)).fetchone()
            if previous is not None:
                return _previous(previous, receipt, notice_hash)
            selected = _select_host(db, thread)
            if "binding" not in selected:
                return selected
    except (OSError, ValueError, sqlite3.Error):
        return _unknown(receipt, "CODEX_NATIVE_LEDGER_UNVERIFIED")
    if selected["gateway"]:
        try:
            raw = codex_native.call_registered(selected["binding"], "read_thread",
                                               {"threadId": thread, "hostId": "local", "turnLimit": 1,
                                                "includeOutputs": False, "maxOutputCharsPerItem": 0},
                                               source_thread=selected["source_thread"], timeout=8)
            readback = _native_payload(raw)
            target = readback.get("thread") if readback is not None else None
            if not isinstance(target, dict) or target.get("id") != thread:
                return _requires_binding("CODEX_NATIVE_GATEWAY_TARGET_UNVERIFIED")
        except (OSError, ValueError, RuntimeError):
            # A failed read cannot have submitted this notice. No send or fallback
            # occurs; an explicit later retry may recheck this read-only preflight.
            return _requires_binding("CODEX_NATIVE_GATEWAY_TARGET_UNVERIFIED")
    try:
        with _database(root) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM notifications WHERE thread=? AND message_id=?",
                                  (thread, msgid)).fetchone()
            if previous is not None:
                return _previous(previous, receipt, notice_hash)
            refreshed = _select_host(db, thread)
            if "binding" not in refreshed or refreshed["binding"] != selected["binding"]:
                return _requires_binding("CODEX_NATIVE_GATEWAY_BINDING_CHANGED")
            selected = refreshed
            with db:
                db.execute("INSERT INTO notifications VALUES (?, ?, ?, ?, 'reserved', ?, NULL)",
                           (thread, msgid, receipt["notice_id"], notice_hash, _now()))
    except (OSError, ValueError, sqlite3.Error):
        return _unknown(receipt, "CODEX_NATIVE_LEDGER_UNVERIFIED")
    try:
        raw = codex_native.call_registered(selected["binding"], "send_message_to_thread",
                                           {"threadId": thread, "hostId": "local", "prompt": shortnotice},
                                           source_thread=selected["source_thread"], timeout=8)
        accepted = _acknowledged(raw, thread)
    except (OSError, ValueError, RuntimeError):
        accepted = False
    if not accepted:
        # The reserved row is already a durable unknown outcome. Do not persist
        # arbitrary provider text, invoke another backend, or send again.
        return _unknown(receipt, "CODEX_NATIVE_ACK_UNVERIFIED")
    result = {"ok": True, "status": "admitted", **receipt,
              "admission_evidence": "native_send_now_accepted", "read_status": "unverified",
              "model_status": "unverified", "dispatcher_session": selected["source_thread"],
              "gateway": selected["gateway"]}
    try:
        with _database(root) as db, db:
            db.execute("UPDATE notifications SET status='admitted',result_json=? WHERE thread=? AND message_id=?",
                       (json.dumps(result, ensure_ascii=False, separators=(",", ":")), thread, msgid))
    except (OSError, ValueError, sqlite3.Error):
        return _unknown(receipt, "CODEX_NATIVE_ACK_PERSIST_UNVERIFIED")
    return result
