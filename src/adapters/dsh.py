"""Notify a live original DSH Host through an explicitly installed Host plugin.

This adapter never opens/resumes a DSH Session or copies DSH credentials.
The supplied Host-only plugin uses its own sessionController service. Local
file IPC is authenticated and event-driven; its capability stays on this user.
"""

from contextlib import closing
import hashlib
import hmac
import inspect as source_inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from src.runtime import local_ipc, messages


PROTOCOL = 1
TIMEOUT = 8
BRIDGE_MARKER = ".xdev-dsh-bridge.json"


def _bridge_scope(directory, profile):
    """Limit ACL changes to the explicitly prepared, user-local IPC directory."""
    directory = Path(directory)
    if (not directory.is_absolute() or ".." in directory.parts
            or directory.name != profile or directory.parent.name != "dsh"
            or directory.parent.parent.name != "state"):
        raise ValueError("Use a dedicated absolute state/dsh/<profile> bridge directory")
    for ancestor in [directory, *directory.parents]:
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError("DSH bridge paths must not be linked")
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if not local.is_absolute() or not directory.is_relative_to(local):
            raise ValueError("Windows DSH bridge directories must be inside LOCALAPPDATA")
    return directory


def _windows_user_sid():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        rendered = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return rendered.value
        finally:
            kernel.LocalFree(ctypes.cast(rendered, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _grant_bridge_user(directory):
    """Grant TokenUser, not default Owner Rights, for elevated SSH / GUI IPC."""
    if sys.platform != "win32":
        return
    sid = _windows_user_sid()
    if not re.fullmatch(r"S-1-5-21-(?:\d+-){3}\d+", sid):
        raise RuntimeError("Unable to identify the current Windows bridge user")
    import ctypes
    from ctypes import wintypes

    class Trustee(ctypes.Structure):
        _fields_ = [("multiple", ctypes.c_void_p), ("operation", ctypes.c_int),
                    ("form", ctypes.c_int), ("kind", ctypes.c_int), ("name", ctypes.c_void_p)]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [("permissions", wintypes.DWORD), ("mode", ctypes.c_int),
                    ("inheritance", wintypes.DWORD), ("trustee", Trustee)]

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    security.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, pointer]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                             ctypes.c_void_p, ctypes.c_void_p, pointer,
                                             ctypes.c_void_p, pointer]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.POINTER(ExplicitAccess),
                                         ctypes.c_void_p, pointer]
    security.SetEntriesInAclW.restype = wintypes.DWORD
    security.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                             ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    native_sid, descriptor, original, updated = (ctypes.c_void_p() for _ in range(4))
    try:
        if not security.ConvertStringSidToSidW(sid, ctypes.byref(native_sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = security.GetNamedSecurityInfoW(str(directory), 1, 4, None, None,
                                               ctypes.byref(original), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        # GRANT_ACCESS merges with the existing DACL, preserving owner, SYSTEM,
        # Administrators and existing ACEs. 3 = OBJECT|CONTAINER inheritance.
        entry = ExplicitAccess(0x1F01FF, 1, 3, Trustee(None, 0, 0, 1, native_sid.value))
        error = security.SetEntriesInAclW(1, ctypes.byref(entry), original, ctypes.byref(updated))
        if error:
            raise ctypes.WinError(error)
        error = security.SetNamedSecurityInfoW(str(directory), 1, 4, None, None, updated, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        for allocation in (updated, descriptor, native_sid):
            if allocation.value:
                kernel.LocalFree(allocation)


def _prepare_bridge(directory, profile):
    directory = _bridge_scope(directory, profile)
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / BRIDGE_MARKER
    expected = {"protocol": PROTOCOL, "kind": "xdev-dsh-private-bridge", "profile": profile,
                "directory": str(directory)}
    if existed and not marker.exists() and any(directory.iterdir()):
        # The first installed version predates the marker. Migrate only its
        # exact published backend/profile binding, never an unknown directory.
        try:
            legacy = json.loads(_read(directory / "binding.json", 16384))
            valid = (legacy.get("protocol") == PROTOCOL and legacy.get("profile") == profile
                     and legacy.get("backend") == "dsh_same_host_plugin")
        except (OSError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Existing DSH bridge has no matching ownership marker or legacy binding")
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(expected, stream, sort_keys=True)
    except FileExistsError:
        if json.loads(_read(marker, 4096)) != expected:
            raise ValueError("DSH bridge ownership marker does not match")
    # Grant the exact user on each directory, including existing request folders;
    # inherited OWNER RIGHTS alone follows the elevated creator's default owner.
    for target in (directory, directory / "requests", directory / "responses"):
        _bridge_scope(directory, profile)
        if target.is_symlink() or target.is_junction():
            raise ValueError("DSH bridge directories must not be linked")
        target.mkdir(exist_ok=True, mode=0o700)
        _grant_bridge_user(target)
    return directory


def _host_alive(pid):
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a harmless existence probe on native Windows.
        from src.adapters.claude import _process
        identity = _process(pid)
        return bool(identity and identity["same_user"])
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def canonical_session(value):
    if not isinstance(value, str):
        raise ValueError("Supply the existing DSH session UUID or session-UUID")
    raw = value.removeprefix("session-")
    try:
        canonical = str(UUID(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Supply the existing DSH session UUID or session-UUID") from exc
    if canonical != raw.lower():
        raise ValueError("Supply the canonical existing DSH session ID")
    return "session-" + canonical


def _read(path, maximum=65536):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > maximum):
        raise ValueError("Invalid DSH bridge file")
    return path.read_bytes()


def _binding(root, session):
    override = os.environ.get("XDEV_DSH_BRIDGE")
    paths = [Path(override) / "binding.json"] if override else list((root / "state/dsh").glob("*/binding.json"))
    if len(paths) > 32:
        raise ValueError("Too many DSH bridge bindings; select XDEV_DSH_BRIDGE on the target")
    found = []
    for path in paths:
        try:
            row = json.loads(_read(path, 16384))
            if (not isinstance(row, dict) or row.get("protocol") != PROTOCOL
                    or row.get("backend") != "dsh_same_host_plugin" or session not in row.get("sessions", [])):
                continue
            if (not isinstance(row.get("profile"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["profile"])
                    or not isinstance(row.get("token"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["token"])
                    or type(row.get("pid")) is not int or row["pid"] <= 0):
                continue
            if not _host_alive(row["pid"]):
                continue
            found.append({**row, "directory": path.parent})
        except (OSError, ValueError, TypeError):
            continue
    if len(found) > 1:
        raise ValueError("Ambiguous DSH profile: several live bridges bind the same exact session")
    return found[0] if found else None


def _envelope(body, token):
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    mac = hmac.new(bytes.fromhex(token), encoded.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"body": encoded, "mac": mac}, ensure_ascii=False).encode("utf-8")


def _response(path, binding, call_id, session, request_id, *, deadline=None):
    try:
        raw_file = (_read(path) if deadline is None else local_ipc.read_response(
            lambda: _read(path), deadline=deadline, stage="dsh.response_read"))
        envelope = json.loads(raw_file)
        raw, signature = envelope["body"], envelope["mac"]
        expected = hmac.new(bytes.fromhex(binding["token"]), raw.encode(), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
            raise ValueError("DSH bridge response authentication failed")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("profile") != binding["profile"]
                or value.get("call_id") != call_id or value.get("session") != session
                or value.get("request_id") != request_id):
            raise ValueError("DSH bridge response target mismatch")
        return value
    except FileNotFoundError:
        return None


def _exchange(binding, body, call_id, timeout=TIMEOUT):
    directory = binding["directory"]
    request_path = directory / "requests" / (call_id + ".json")
    response_path = directory / "responses" / (call_id + ".json")
    wire = _envelope(body, binding["token"])
    deadline = time.monotonic() + timeout
    with messages._watch(directory / "responses") as changed:
        ready = _response(response_path, binding, call_id, body["session"], body["request_id"], deadline=deadline)
        if ready is not None and ready.get("status") != "unknown":
            return ready
        previous_response = (local_ipc.read_response(lambda: response_path.stat().st_mtime_ns,
                             deadline=deadline, stage="dsh.response_stat") if ready is not None else None)
        if request_path.exists():
            if _read(request_path) != wire:
                raise ValueError("DSH bridge call ID already has another payload")
            # Re-signal the identical durable call after plugin restart/backlog.
            # Neither native request_id nor the original payload is changed.
            os.utime(request_path, None)
        else:
            descriptor, temporary = tempfile.mkstemp(prefix="request-", dir=request_path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(wire)
                    stream.flush()
                os.replace(temporary, request_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        while True:
            ready = _response(response_path, binding, call_id, body["session"], body["request_id"], deadline=deadline)
            if ready is not None and (previous_response is None or local_ipc.read_response(
                    lambda: response_path.stat().st_mtime_ns, deadline=deadline,
                    stage="dsh.response_stat") != previous_response):
                return ready
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not changed(remaining):
                # A writer can settle immediately as the directory wait expires.
                final = _response(response_path, binding, call_id, body["session"], body["request_id"], deadline=deadline)
                if final is not None:
                    return final
                return {"status": "unknown", "error_code": "DSH_BRIDGE_TIMEOUT"}


def _admissions(root):
    (root / "state").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "state/dsh-admissions.sqlite3", timeout=5)
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS admissions (
            session TEXT NOT NULL, message_id TEXT NOT NULL, profile TEXT NOT NULL,
            directory TEXT NOT NULL, request_id TEXT NOT NULL, call_id TEXT NOT NULL,
            admitted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session,message_id))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(admissions)")}
        if "promotion_call_id" not in columns:
            db.execute("ALTER TABLE admissions ADD COLUMN promotion_call_id TEXT")
        if "notification_mode" not in columns:
            db.execute("ALTER TABLE admissions ADD COLUMN notification_mode TEXT NOT NULL DEFAULT 'queue'")
    return db


def _unknown(receipt, **details):
    return {"ok": False, "status": "unknown", "outcome": "unknown", "model_status": "unverified",
            "error_code": "DSH_ADMISSION_UNVERIFIED", "retry_policy": "reuse_original_request_and_response",
            **receipt, **details}


def inspect(root, session):
    """Read the loaded original Host/Agent and its tool catalog; no prompt."""
    session = canonical_session(session)
    binding = _binding(root, session)
    if binding is None:
        return {"ok": False, "status": "requires_activation", "session": session,
                "error_code": "DSH_RECEIVER_NOT_LOADED"}
    call_id = str(uuid4())
    result = _exchange(binding, {"op": "inspect", "profile": binding["profile"], "call_id": call_id,
                                 "session": session, "request_id": str(uuid4())}, call_id)
    return {"ok": result.get("status") == "ready", **result, "model_calls": 0}


def submit(root, identifier, session, steer=False, idle_only=False):
    if type(steer) is not bool or type(idle_only) is not bool or (steer and idle_only):
        raise ValueError("DSH steer and idle-only must be booleans and cannot be combined")
    session = canonical_session(session)
    query = messages.handle(root, "wait", {"id": identifier, "timeout": 0})
    item = query.get("message")
    if query.get("status") != "received" or not item:
        raise ValueError("This message is not in the local device inbox")
    notice_id = "wake-xdev-" + hashlib.sha256((session + "\n" + identifier).encode()).hexdigest()[:24]
    receipt = {"client": "dsh", "session": session, "message_id": identifier,
               "notice_id": notice_id, "model_status": "unverified"}
    with closing(_admissions(root)) as db:
        prior = db.execute("SELECT profile,request_id,admitted FROM admissions WHERE session=? AND message_id=?",
                           (session, identifier)).fetchone()
    if prior and prior[2] and not steer:
        return {"ok": True, "status": "already_admitted", **receipt, "profile": prior[0], "request_id": prior[1]}
    binding = _binding(root, session)
    if binding is None:
        return {"ok": False, "status": "requires_activation", "error_code": "DSH_RECEIVER_NOT_LOADED",
                **receipt, "requires_activation": True,
                "next_action": "Enable the xdev Host receiver in the original DSH profile and bind this exact live root session; do not start a second headless or ACP writer."}
    features = binding.get("features", [])
    if steer and (not isinstance(features, list) or not all(isinstance(value, str) for value in features)
                  or not {"steer", "promote"}.issubset(set(features))):
        return {"ok": False, "status": "requires_activation", **receipt,
                "requires_activation": True, "requires_receiver_upgrade": True,
                "error_code": "DSH_RECEIVER_MODE_UNSUPPORTED",
                "next_action": "Enable the v2 same-Host receiver for native steering; the existing message was not resent."}
    request_id = str(uuid5(NAMESPACE_URL, "xdev-dsh-v1\n" + binding["profile"] + "\n" + session + "\n" + identifier))
    receipt.update(profile=binding["profile"], request_id=request_id, notice_id=notice_id)
    from src.adapters.notices import body as notice_body
    notice = f"[xdev external AI notice {notice_id}, from device {json.dumps(item['from'])}] " + notice_body(item, identifier, "dsh", session)
    promotion_call_id = None
    with closing(_admissions(root)) as db:
        row = db.execute("SELECT profile,directory,request_id,call_id,admitted,promotion_call_id,notification_mode FROM admissions WHERE session=? AND message_id=?",
                         (session, identifier)).fetchone()
        if row and (row[0] != binding["profile"] or os.path.normcase(row[1]) != os.path.normcase(str(binding["directory"]))):
            return _unknown(receipt, error_code="DSH_PROFILE_CONFLICT")
        if row and steer:
            with db:
                db.execute("UPDATE admissions SET promotion_call_id=COALESCE(promotion_call_id,?) WHERE session=? AND message_id=?",
                           (str(uuid4()), session, identifier))
            promotion_call_id = db.execute("SELECT promotion_call_id FROM admissions WHERE session=? AND message_id=?",
                                           (session, identifier)).fetchone()[0]
        elif row and row[4]:
            return {"ok": True, "status": "already_admitted", **receipt}
        if row is None:
            check_id = str(uuid4())
            try:
                checked = _exchange(binding, {"op": "inspect", "profile": binding["profile"], "call_id": check_id,
                                             "session": session, "request_id": request_id}, check_id)
            except (OSError, ValueError, RuntimeError) as error:
                return _unknown(receipt, error_code="DSH_BRIDGE_PREFLIGHT_UNVERIFIED", diagnostic_code=type(error).__name__,
                                **local_ipc.diagnostics(error, stage="dsh.preflight"))
            if checked.get("active") is not True:
                if checked.get("status") == "unknown":
                    return _unknown(receipt, error_code=checked.get("error_code", "DSH_BRIDGE_TIMEOUT"))
                return {"ok": False, "status": "requires_activation", **receipt,
                        "error_code": "DSH_ORIGINAL_AGENT_NOT_ACTIVE", "requires_activation": True,
                        "next_action": "Open the exact original DSH chat in the bound profile; the adapter will not resume another writer."}
            if idle_only and checked.get("running") is not False:
                return {"ok": True, "status": "deferred_in_inbox", **receipt,
                        "observed_before_submit": "busy_or_unknown"}
            call_id = str(uuid4())
            with db:
                db.execute("INSERT OR IGNORE INTO admissions(session,message_id,profile,directory,request_id,call_id,notification_mode) VALUES(?,?,?,?,?,?,?)",
                           (session, identifier, binding["profile"], str(binding["directory"]), request_id, call_id,
                            "steer" if steer else "queue"))
            row = db.execute("SELECT profile,directory,request_id,call_id,admitted,promotion_call_id,notification_mode FROM admissions WHERE session=? AND message_id=?",
                             (session, identifier)).fetchone()
            if steer and row[6] != "steer":
                # A simultaneous queue caller reserved this ID first. Promote
                # that occurrence; never mutate/replay its original payload.
                with db:
                    db.execute("UPDATE admissions SET promotion_call_id=COALESCE(promotion_call_id,?) WHERE session=? AND message_id=?",
                               (str(uuid4()), session, identifier))
                promotion_call_id = db.execute("SELECT promotion_call_id FROM admissions WHERE session=? AND message_id=?",
                                               (session, identifier)).fetchone()[0]
    if promotion_call_id:
        return _promote_existing(root, binding, receipt, row[2], promotion_call_id, bool(row[4]))
    request_id, call_id = row[2], row[3]
    receipt["request_id"] = request_id
    try:
        result = _exchange(binding, {"op": "notify", "profile": binding["profile"], "call_id": call_id,
                                    "session": session, "request_id": request_id, "notice": notice,
                                    "mode": row[6], "idle_only": idle_only}, call_id)
    except (OSError, ValueError, RuntimeError) as error:
        return _unknown(receipt, error_code="DSH_BRIDGE_TRANSPORT_UNVERIFIED", diagnostic_code=type(error).__name__,
                        **local_ipc.diagnostics(error, stage="dsh.notification_response"))
    if result.get("accepted") is True and result.get("api") == "sessionController.prompt":
        try:
            with closing(_admissions(root)) as db:
                with db:
                    db.execute("UPDATE admissions SET admitted=1 WHERE session=? AND message_id=?", (session, identifier))
        except sqlite3.Error:
            return _unknown(receipt, error_code="DSH_ACK_PERSIST_UNVERIFIED")
        return {"ok": True, "status": "admitted", **receipt, "delivery_mode": row[6], "admission": {key: result[key] for key in
                ("accepted", "api", "host_id", "observed_running") if key in result}}
    if result.get("submitted") is False:
        with closing(_admissions(root)) as db:
            with db:
                db.execute("DELETE FROM admissions WHERE session=? AND message_id=? AND call_id=? AND admitted=0",
                           (session, identifier, call_id))
        return {"ok": result.get("status") == "deferred_in_inbox", **receipt,
                "status": result.get("status", "requires_activation"), "requires_activation": result.get("status") == "requires_activation"}
    return _unknown(receipt, error_code=result.get("error_code", "DSH_ADMISSION_UNVERIFIED"))


def _promote_existing(root, binding, receipt, request_id, call_id, admitted):
    receipt = {**receipt, "request_id": request_id, "delivery_mode": "steer"}
    try:
        result = _exchange(binding, {"op": "promote", "profile": binding["profile"],
                                    "call_id": call_id, "session": receipt["session"],
                                    "request_id": request_id}, call_id)
    except (OSError, ValueError, RuntimeError) as error:
        return _unknown(receipt, error_code="DSH_PROMOTION_UNVERIFIED", diagnostic_code=type(error).__name__,
                        **local_ipc.diagnostics(error, stage="dsh.promotion_response"))
    if result.get("accepted") is True and result.get("status") in {"promoted", "already_steering", "already_consumed"}:
        try:
            with closing(_admissions(root)) as db:
                with db:
                    db.execute("UPDATE admissions SET admitted=1 WHERE session=? AND message_id=?",
                               (receipt["session"], receipt["message_id"]))
        except sqlite3.Error:
            return _unknown(receipt, error_code="DSH_ACK_PERSIST_UNVERIFIED")
        return {"ok": True, "status": "admitted" if result["status"] == "promoted" else "already_admitted",
                **receipt, "promotion": {key: result[key] for key in
                    ("status", "api", "item_id", "from_placement", "to_placement", "observed_running") if key in result}}
    if result.get("status") in {"requires_activation", "not_pending", "steer_unavailable"}:
        return {"ok": False, **receipt, "status": result["status"],
                "original_admitted": admitted or result.get("original_admitted") is True,
                "requires_activation": result["status"] == "requires_activation",
                "error_code": result.get("error_code", "DSH_PROMOTION_NOT_APPLIED")}
    return _unknown(receipt, error_code=result.get("error_code", "DSH_PROMOTION_UNVERIFIED"))


HOST_PLUGIN = r'''import { promises as fs, watch } from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
export const inject = ['sessionController', 'agents'];
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sid = /^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const mac = (text, token) => crypto.createHmac('sha256', Buffer.from(token, 'hex')).update(text).digest('hex');
async function atomic(file, value) { const tmp = file + '.tmp-' + crypto.randomUUID(); await fs.writeFile(tmp, JSON.stringify(value), {mode:0o600}); await fs.rename(tmp,file); }
async function json(file) { const st=await fs.lstat(file); if(!st.isFile()||st.isSymbolicLink()||st.size>65536) throw Error('invalid bridge file'); return JSON.parse(await fs.readFile(file,'utf8')); }
function originalInput(agent,requestId) {
  let admitted=false,consumed=false;
  if(typeof agent.session.snapshotEvents!=='function')return {admitted,consumed};
  for(const event of agent.session.snapshotEvents(0)) {
    if(event.seq<(agent.session.inheritedEventCount||0))continue;
    const messages=event.type==='agent/inbox/spliced'?event.data.inserted:event.type==='user/message'?[event.data]:[];
    for(const message of messages||[])if(message.source?.kind==='user'&&message.source.rpcId===requestId) {
      admitted=true;if(event.type==='user/message')consumed=true;
    }
  }
  return {admitted,consumed};
}
function existingPlacement(agent,state,requestId) {
  const matches=Array.isArray(state.queue)?state.queue.filter(item=>item.rpcId===requestId):[];
  if(matches.length>1)return {status:'unknown',error_code:'DSH_NATIVE_QUEUE_AMBIGUOUS'};
  if(matches.length===1) {
    const item=matches[0];
    if(typeof item.id!=='string'||!item.id)return {status:'unknown',error_code:'DSH_NATIVE_ITEM_UNVERIFIED'};
    if(item.placement==='steering')return {status:'already_steering',accepted:true,api:'sessionController.state',item_id:item.id,to_placement:'steering'};
    if(item.placement==='queued')return {status:'queued',item_id:item.id,original_admitted:true};
    return {status:'unknown',error_code:'DSH_NATIVE_PLACEMENT_UNVERIFIED'};
  }
  const history=originalInput(agent,requestId);
  if(history.consumed)return {status:'already_consumed',accepted:true,api:'native_user_message_history'};
  return {status:history.admitted?'not_pending':'unknown',original_admitted:history.admitted,error_code:history.admitted?'DSH_NATIVE_ITEM_NO_LONGER_PENDING':'DSH_NATIVE_REQUEST_NOT_OBSERVED'};
}
export async function promoteExisting(ctx,req,agent,state) {
  if(ctx.agents.get(req.session)!==agent||agent.session?.header?.id!==req.session||agent.session.header.origin==='subagent')return {status:'requires_activation',submitted:false};
  let observed=existingPlacement(agent,state,req.request_id);
  if(observed.status!=='queued')return observed;
  if(state.running!==true)return {status:'steer_unavailable',error_code:'DSH_STEER_UNAVAILABLE',item_id:observed.item_id,original_admitted:true};
  if(ctx.agents.get(req.session)!==agent||agent.session?.header?.id!==req.session||agent.session.header.origin==='subagent')return {status:'requires_activation',submitted:false};
  try {
    const answer=await ctx.sessionController.updateQueue({sessionId:req.session,itemId:observed.item_id,action:{kind:'steer'}});
    if(answer?.accepted!==true)return {status:'unknown',error_code:'DSH_PROMOTION_ACK_UNVERIFIED'};
    return {status:'promoted',accepted:true,api:'sessionController.updateQueue',item_id:observed.item_id,
      from_placement:'queued',to_placement:'steering',observed_running:state.running};
  } catch {
    // A person or another retry may already have promoted/consumed this exact
    // occurrence. Reconcile it; never replace it with another prompt.
    if(ctx.agents.get(req.session)!==agent)return {status:'requires_activation',submitted:false};
    const current=await ctx.sessionController.state({sessionId:req.session},AbortSignal.timeout(5000));
    observed=existingPlacement(agent,current,req.request_id);
    return observed.status==='queued'?{status:'unknown',error_code:'DSH_PROMOTION_UNVERIFIED'}:observed;
  }
}
async function unlinked(dir) {
  for(let item=dir;;item=path.dirname(item)) {
    try {if((await fs.lstat(item)).isSymbolicLink())throw Error('linked bridge path');}
    catch(error){if(error.code!=='ENOENT')throw error;}
    if(path.dirname(item)===item)break;
  }
}
export async function runSecurityTool(executable,args,{spawnProcess=spawn,timeoutMs=8000}={}) {
  if(!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>8000)throw Error('invalid ACL helper deadline');
  return new Promise((resolve,reject)=>{
    const deadline=performance.now()+timeoutMs;
    let child,settled=false,buffer=Buffer.alloc(0);
    const cleanup=()=>{
      if(!child)return;
      try{child.unref?.();}catch{}
      for(const stream of [child.stdin,child.stdout,child.stderr])try{stream?.unref?.();}catch{}
      // ACL setup has already committed before its receipt. Never wait for
      // Python atexit or stdio EOF, including on the failure/timeout path.
      const stop=setImmediate(()=>{
        try{child.kill();}catch{}
        for(const stream of [child.stdin,child.stdout,child.stderr])try{stream?.destroy?.();}catch{}
      });
      stop.unref?.();
    };
    const finish=(error,receipt)=>{
      if(settled)return;settled=true;clearTimeout(timer);cleanup();
      error?reject(error):resolve(receipt);
    };
    const timer=setTimeout(()=>finish(Error('ACL helper receipt timed out')),timeoutMs);
    try {
      child=spawnProcess(executable,args,{shell:false,windowsHide:true,stdio:['ignore','pipe','pipe']});
      if(performance.now()>=deadline){finish(Error('ACL helper receipt timed out'));return;}
      child.once('error',()=>finish(Error('ACL helper process failed')));
      child.once('close',()=>finish(Error('ACL helper closed without a valid receipt')));
      child.stdout.on('error',()=>finish(Error('ACL helper output failed')));
      child.stderr.on('error',()=>finish(Error('ACL helper diagnostics failed')));
      child.stderr.on('data',()=>finish(Error('ACL helper reported an error')));
      child.stdout.on('end',()=>finish(Error('ACL helper ended without a complete receipt')));
      child.stdout.on('data',chunk=>{
        if(settled)return;
        if(performance.now()>=deadline){finish(Error('ACL helper receipt timed out'));return;}
        const data=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);
        if(buffer.length+data.length>4096){finish(Error('ACL helper output exceeds its limit'));return;}
        buffer=Buffer.concat([buffer,data]);
        const newline=buffer.indexOf(10);if(newline<0)return;
        try {
          const receipt=new TextDecoder('utf-8',{fatal:true}).decode(buffer.subarray(0,newline));
          const value=JSON.parse(receipt);
          if(value===null||Array.isArray(value)||typeof value!=='object'||value.ok!==true||
             Object.keys(value).length!==1||buffer.subarray(newline+1).toString('utf8').trim())throw Error('invalid receipt');
          finish(undefined,receipt);
        } catch {finish(Error('ACL helper returned an invalid receipt'));}
      });
    } catch {finish(Error('ACL helper could not start'));}
  });
}
export async function prepareBridge(dir,profile,pythonExecutable) {
  if(!path.isAbsolute(dir)||path.basename(dir)!==profile||path.basename(path.dirname(dir))!=='dsh'||
     path.basename(path.dirname(path.dirname(dir)))!=='state'||dir.split(/[\\/]/).includes('..'))throw Error('invalid dedicated bridge directory');
  if(process.platform==='win32') {
    const local=process.env.LOCALAPPDATA,relative=local&&path.relative(path.resolve(local),path.resolve(dir));
    if(!local||relative===undefined||relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))throw Error('bridge outside LOCALAPPDATA');
  }
  await unlinked(dir);
  const marker=await json(path.join(dir,'.xdev-dsh-bridge.json'));
  if(marker.protocol!==1||marker.kind!=='xdev-dsh-private-bridge'||marker.profile!==profile||
     typeof marker.directory!=='string'||path.resolve(marker.directory)!==path.resolve(dir))throw Error('invalid bridge ownership marker');
  for(const name of ['requests','responses'])await fs.mkdir(path.join(dir,name),{recursive:true,mode:0o700});
  const directories=[dir,path.join(dir,'requests'),path.join(dir,'responses')];
  for(const target of directories)await unlinked(target);
  if(process.platform==='win32') {
    if(typeof pythonExecutable!=='string'||!path.isAbsolute(pythonExecutable)||path.basename(pythonExecutable).toLowerCase()!=='python.exe')throw Error('invalid ACL Python runtime');
    const helper=fileURLToPath(new URL('./acl.py',import.meta.url));
    const answer=await runSecurityTool(pythonExecutable,['-B','-X','utf8',helper,dir,profile]);
    if(JSON.parse(answer).ok!==true)throw Error('Windows bridge ACL setup was not confirmed');
  }
}
export async function startReceiver(ctx, config) {
  const dir=config.bridgeDirectory, profile=config.profile, sessions=config.sessions;
  if(!path.isAbsolute(dir)||!/^[A-Za-z0-9_-]{1,64}$/.test(profile)||!Array.isArray(sessions)||!sessions.length||sessions.some(s=>!sid.test(s))) throw Error('invalid receiver binding');
  // Complete exact-user ACL setup before exposing any binding or capability.
  // OWNER RIGHTS alone follows the elevated SSH creator, not its GUI user.
  await prepareBridge(dir,profile,config.aclPython);
  const bindingFile=path.join(dir,'binding.json'); let prior;
  try { prior=await json(bindingFile); } catch {}
  const token=prior?.profile===profile&&/^[a-f0-9]{64}$/.test(prior.token||'')?prior.token:crypto.randomBytes(32).toString('hex');
  const hostId=crypto.randomUUID(), accepted=new Map(), pending=new Map(), processing=new Set(); let disposed=false;
  const ledgerFile=path.join(dir,'accepted.json');
  try { const saved=await json(ledgerFile); for(const [k,v] of Object.entries(saved)) accepted.set(k,v); } catch {}
  const binding={protocol:1,backend:'dsh_same_host_plugin',profile,sessions,pid:process.pid,host_id:hostId,token,
    features:['inspect_queue','steer','promote']};
  await atomic(bindingFile,binding);
  function toolsOf(agent) {
    let names=[],servers=[];
    try {const tools=ctx.get?.('tools');names=(tools?.schemas(agent)||[]).map(t=>t.name).filter(n=>n.startsWith('mcp__xdev__')).sort();}catch{}
    try {const manager=ctx.get?.('mcpManager');servers=(manager?.list()||[]).filter(s=>s.name==='xdev').map(s=>({name:s.name,configured:s.configured===true,status:s.status?.kind??s.status?.state??s.status?.status??'unavailable'}));}catch{}
    return {xdev_tool_names:names,xdev_manager:servers,tool_catalog_scope:'current_host_agent_registered_not_step_execution'};
  }
  async function reply(file, req, value) { const body=JSON.stringify({profile,call_id:req.call_id,session:req.session,request_id:req.request_id,...value}); await atomic(file,{body,mac:mac(body,token)}); }
  async function notify(req, agent, state) {
    const key=req.session+'\n'+req.request_id, hash=crypto.createHash('sha256').update(req.notice).digest('hex');
    const cached=accepted.get(key); if(cached) {if(cached.hash!==hash) throw Error('request conflict'); return cached.value;}
    if(pending.has(key)) return pending.get(key);
    const work=(async()=>{
      if(ctx.agents.get(req.session)!==agent||agent.session?.header?.id!==req.session||agent.session.header.origin==='subagent') return {status:'requires_activation',submitted:false};
      const answer=await ctx.sessionController.prompt({sessionId:req.session,requestId:req.request_id,mode:req.mode,content:[{type:'text',text:req.notice}]},AbortSignal.timeout(5000));
      if(answer?.accepted!==true) return {status:'unknown',error_code:'DSH_NATIVE_ACK_UNVERIFIED'};
      const value={status:'accepted',accepted:true,api:'sessionController.prompt',host_id:hostId,observed_running:state.running};
      accepted.set(key,{hash,value}); await atomic(ledgerFile,Object.fromEntries(accepted)); return value;
    })(); pending.set(key,work); try{return await work;}finally{pending.delete(key);}
  }
  async function handle(name) {
    if(disposed||!uuid.test(name.replace(/\.json$/,''))||!name.endsWith('.json')||processing.has(name)) return;
    processing.add(name); const input=path.join(dir,'requests',name), output=path.join(dir,'responses',name);
    try {
      const envelope=await json(input); if(typeof envelope.body!=='string'||! /^[a-f0-9]{64}$/.test(envelope.mac||'')||!crypto.timingSafeEqual(Buffer.from(envelope.mac,'hex'),Buffer.from(mac(envelope.body,token),'hex'))) return;
      const req=JSON.parse(envelope.body);
      if(req.call_id+'.json'!==name||req.profile!==profile||!sessions.includes(req.session)||!uuid.test(req.request_id)||!['inspect','notify','promote'].includes(req.op)) return;
      const agent=ctx.agents.get(req.session);
      if(!agent||agent.session?.header?.id!==req.session||agent.session.header.origin==='subagent') {await reply(output,req,{status:'requires_activation',active:false,submitted:false});return;}
      const state=await ctx.sessionController.state({sessionId:req.session},AbortSignal.timeout(5000));
      if(!state.active||ctx.agents.get(req.session)!==agent) {await reply(output,req,{status:'requires_activation',active:false,submitted:false});return;}
      if(req.op==='inspect') {await reply(output,req,{status:'ready',active:true,running:state.running,host_id:hostId,
        cursor:state.cursor,queue:Array.isArray(state.queue)?state.queue.map(item=>({id:item.id,placement:item.placement,...typeof item.rpcId==='string'?{rpcId:item.rpcId}:{}})):null,
        pendingInput:Array.isArray(state.pendingInput)?state.pendingInput.filter(x=>['approval','question','other'].includes(x)):[],...toolsOf(agent)});return;}
      if(req.op==='promote') {await reply(output,req,await promoteExisting(ctx,req,agent,state));return;}
      if(!['queue','steer'].includes(req.mode)||typeof req.notice!=='string'||!req.notice.startsWith('[xdev external AI notice ')||req.notice.length>4096) return;
      if(req.idle_only&&state.running!==false) {await reply(output,req,{status:'deferred_in_inbox',submitted:false,active:true,running:state.running});return;}
      const work=notify(req,agent,state);
      const timeout=new Promise(resolve=>{const t=setTimeout(()=>resolve({status:'unknown',error_code:'DSH_NATIVE_TIMEOUT'}),6000);t.unref?.();});
      const result=await Promise.race([work,timeout]); await reply(output,req,result);
      if(result.status==='unknown') void work.then(value=>!disposed&&reply(output,req,value)).catch(()=>{});
    } catch { /* Never publish exception text or credentials. Caller retains unknown. */ }
    finally {processing.delete(name);}
  }
  const watcher=watch(path.join(dir,'requests'),(_event,file)=>{if(file) void handle(String(file));});
  for(const file of (await fs.readdir(path.join(dir,'requests'))).slice(0,256)) void handle(file);
  return {profile,host_id:hostId, async dispose(){disposed=true;watcher.close();try{const now=await json(bindingFile);if(now.host_id===hostId)await fs.unlink(bindingFile);}catch{}}};
}
export async function apply(ctx,config) { const receiver=await startReceiver(ctx,config);ctx.effect(()=>()=>receiver.dispose()); }
'''


def write_plugin_bundle(destination, bridge_directory, session_ids, profile="default"):
    """Prepare a reviewable new local bundle; does not install or touch DSH config."""
    destination, bridge_directory = Path(destination), Path(bridge_directory)
    if destination.exists() or not destination.is_absolute() or not bridge_directory.is_absolute():
        raise ValueError("Use a new absolute bundle directory and absolute private bridge directory")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
        raise ValueError("Invalid DSH profile name")
    session_ids = [canonical_session(value) for value in session_ids]
    if not session_ids:
        raise ValueError("Bind at least one exact original DSH session")
    _prepare_bridge(bridge_directory, profile)
    destination.mkdir(parents=True, mode=0o700)
    package = {"name": "@local/xdev-dsh-receiver-v2", "version": "1.0.1", "private": True, "type": "module",
               "exports": {".": "./index.js", "./locale/*.json": "./locale/*.json"},
               "files": ["index.js", "acl.py", "cordis.patch.yml", "locale", "README.md"],
               "dsh": {"bundle": {"patch": "./cordis.patch.yml"}}}
    (destination / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")
    config = {"bridgeDirectory": str(bridge_directory), "profile": profile, "sessions": session_ids,
              "aclPython": sys.executable}
    patch = "- insert:\n    - id: xdev-dsh-receiver-v2\n      name: '@local/xdev-dsh-receiver-v2'\n      config: " + json.dumps(config) + "\n"
    (destination / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    (destination / "index.js").write_text(HOST_PLUGIN, encoding="utf-8")
    # The Host launches only this bundled ACL helper in a bounded child. It
    # reads its own TokenUser and marked directories, never a bridge token.
    helper = "import json, os, re, stat, sys\nfrom pathlib import Path\nPROTOCOL=1\nBRIDGE_MARKER='.xdev-dsh-bridge.json'\n"
    for function in (_bridge_scope, _windows_user_sid, _grant_bridge_user, _read, _prepare_bridge):
        helper += "\n" + source_inspect.getsource(function) + "\n"
    helper += ("\nif __name__ == '__main__':\n"
               " try:\n"
               "  directory,profile=Path(sys.argv[1]),sys.argv[2]\n"
               "  if not (directory/BRIDGE_MARKER).is_file():raise ValueError('marker required')\n"
               "  _prepare_bridge(directory,profile)\n"
               "  print('{\"ok\":true}',flush=True)\n"
               " except Exception:\n"
               "  print('{\"ok\":false,\"error_code\":\"ACL_SETUP_FAILED\"}',flush=True)\n"
               "  sys.exit(1)\n")
    (destination / "acl.py").write_text(helper, encoding="utf-8")
    (destination / "locale").mkdir()
    for language, title, description in (("en", "Xdev inbox receiver", "Notify explicitly bound live DSH chats through their original Host. Default queue, explicit native steering; admission is not model processing."),
                                          ("zh", "Xdev 信箱接收", "通过原 Host 通知明确绑定的活动聊天。默认排队，可明确要求原生插话；接纳不等于模型已处理。")):
        (destination / "locale" / (language + ".json")).write_text(json.dumps({"meta": {"title": title, "description": description}}, ensure_ascii=False), encoding="utf-8")
    (destination / "README.md").write_text(
        "# Xdev same-Host DSH receiver\n\nInstall this new local bundle through DSH's official Plugin Manager. "
        "Do not replace/restart an active Host to force activation. Confirm the Host reports this plugin active. "
        "Its marked state/dsh/<profile> bridge directory must be private to the current Windows user. "
        "On Windows it grants that exact TokenUser SID inheritable FullControl on its three private IPC directories before publishing a binding, so elevated SSH files remain accessible to the GUI user. "
        "The plugin admits only explicitly bound active original sessions with sessionController.prompt; no headless/resume, model API or credential export. "
        "Default queue preserves current work. Explicit steering targets the native next step; an existing request is promoted by exact item ID, never resent or flushed with unrelated inputs. "
        "Steering does not cancel an already running tool. Unload removes binding and stops reception. "
        "The bundle intentionally has no custom icon; it uses the native generic plugin icon.\n", encoding="utf-8")
    return {"bundle": str(destination), "bridge_directory": str(bridge_directory), "profile": profile,
            "bridge_security": "marked_directory_exact_user_inheritable_acl",
            "sessions": session_ids, "installed": False}
