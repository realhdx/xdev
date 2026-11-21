"""Original Antigravity conversations through an explicitly enabled native Sidecar.

Only the documented agentapi executable runs with the Sidecar's own inherited
environment. No upstream credential is read, copied, logged or returned. Native
submission is original-session admission; its busy/queue placement is unknown.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from uuid import UUID


SCHEMA = "xdev.antigravity.native-sidecar.v1"
TIMEOUT = 10
MAX_BYTES = 65536


def _session(value):
    try:
        result = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Supply the UUID of the existing Antigravity conversation") from error
    if result != value.lower():
        raise ValueError("Use a canonical existing conversation UUID")
    return result


def _read(path, limit=MAX_BYTES):
    path = Path(path)
    if path.is_symlink() or (os.name == "nt" and path.is_junction()) or path.stat().st_size > limit:
        raise ValueError("Invalid native Sidecar bridge document")
    return json.loads(path.read_text(encoding="utf-8"))


def _request(binding, payload):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            raise HTTPError(request.full_url, code, "Native loopback bridge refused redirection", headers, fp)

    data = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(f"http://127.0.0.1:{binding['port']}/rpc", data=data,
                      headers={"Content-Type": "application/json", "X-Xdev-Auth": binding["token"]})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=TIMEOUT * 2 + 3) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Native Sidecar response too large")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("Native Sidecar response schema mismatch")
    return result


def _binding(root, session):
    path = Path(root) / "state/antigravity-native/binding.json"
    try:
        binding = _read(path, 16384)
        if (not isinstance(binding, dict) or binding.get("schema") != SCHEMA
                or (session not in binding.get("sessions", []) and binding.get("scope") != "verified_project_roots")
                or type(binding.get("port")) is not int or not 1 <= binding["port"] <= 65535
                or type(binding.get("pid")) is not int
                or not re.fullmatch(r"[a-f0-9]{64}", binding.get("token", ""))):
            return None
        if os.name == "nt":
            from src.adapters.claude import _process
            identity = _process(binding["pid"])
            if not identity["same_user"] or identity["start"] != binding.get("process_start"):
                return None
        return binding
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def inspect(root, session):
    session = _session(session)
    binding = _binding(root, session)
    if binding is None:
        return {"ok": False, "status": "requires_activation", "client": "antigravity", "session": session}
    result = _request(binding, {"op": "inspect", "session": session})
    if result.get("session") != session or result.get("client") != "antigravity":
        return {"ok": False, "status": "binding_unverified", "client": "antigravity", "session": session}
    return result


def submit(root, identifier, session, steer=False, idle_only=False):
    if type(steer) is not bool or type(idle_only) is not bool or steer or idle_only:
        raise ValueError("Antigravity agentapi has no verified steer/idle-only contract; explicitly allow delayed native submission")
    session = _session(session)
    from src.runtime import messages
    query = messages.handle(root, "wait", {"id": identifier, "timeout": 0})
    item = query.get("message")
    if query.get("status") != "received" or not item:
        raise ValueError("This message is not in the local inbox")
    notice_id = "wake-xdev-" + hashlib.sha256(f"{session}\n{identifier}".encode()).hexdigest()[:24]
    receipt = {"client": "antigravity", "session": session, "message_id": identifier,
               "notice_id": notice_id, "model_status": "unverified", "delivery_mode": "queue",
               "delivery_scope": "original_session", "placement": "unverified"}
    binding = _binding(root, session)
    if binding is None:
        return {**receipt, "ok": False, "status": "requires_activation", "requires_activation": True,
                "error_code": "ANTIGRAVITY_SIDECAR_NOT_ACTIVE"}
    from src.adapters.notices import body as notice_body
    notice = f"[xdev external AI notice {notice_id}, from device {json.dumps(item['from'])}] " + notice_body(item, identifier, "antigravity", session)
    payload = {"op": "notify", "session": session, "message_id": identifier, "notice_id": notice_id, "notice": notice}
    if item.get("envelope_version", 1) >= 2 and item.get("subject") is not None:
        from src.runtime.subjects import normalize_subject
        payload["subject"] = normalize_subject(item["subject"])
    try:
        result = _request(binding, payload)
        if any(result.get(key) != value for key, value in (("session", session), ("message_id", identifier),
                                                          ("notice_id", notice_id))):
            raise ValueError("Native Sidecar receipt target mismatch")
        return {**receipt, **result}
    except (OSError, ValueError, TimeoutError) as error:
        return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                "error_code": "ANTIGRAVITY_ADMISSION_UNVERIFIED", "error_class": type(error).__name__,
                "retry_policy": "reuse_same_message_and_notice_id_no_new_native_write"}


def _native(executable, operation, *args, timeout=TIMEOUT):
    """A complete official CLI JSON frame settles this call, not child EOF."""
    received = queue.Queue()
    with tempfile.TemporaryFile() as stderr:
        child = subprocess.Popen([str(executable), "agentapi", operation, *args], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def read():
            try:
                while chunk := os.read(child.stdout.fileno(), 4096):
                    received.put(chunk)
                received.put(None)
            except OSError:
                received.put(None)

        threading.Thread(target=read, daemon=True).start()
        deadline, raw = time.monotonic() + timeout, b""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("native agentapi deadline")
                try:
                    chunk = received.get(timeout=remaining)
                except queue.Empty as error:
                    raise TimeoutError("native agentapi deadline") from error
                if chunk is None:
                    raise RuntimeError("native agentapi ended without a JSON receipt")
                raw += chunk
                if len(raw) > MAX_BYTES:
                    raise ValueError("native agentapi result exceeds limit")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(result, dict) or "response" not in result or result.get("error"):
                    raise RuntimeError("native agentapi rejected operation")
                return result
        finally:
            def cleanup():
                try:
                    if child.poll() is None:
                        child.kill()
                except OSError:
                    pass
                try:
                    child.stdout.close()
                except OSError:
                    pass
            threading.Thread(target=cleanup, daemon=True).start()


def _metadata_value(result):
    value = result.get("response")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _metadata_shape(result):
    value = _metadata_value(result)
    return {"response_type": "text" if isinstance(value, str) else type(value).__name__,
            "response_keys": sorted(value) if isinstance(value, dict) else [], "busy_observable": False}


def _project_for_root(result, session):
    """The native root ID also appears in child metadata, so verify parent and depth."""
    value = _metadata_value(result)
    conversation = value.get("conversationMetadata") if isinstance(value, dict) else None
    metadata = conversation.get("metadata") if isinstance(conversation, dict) else None
    if (not isinstance(metadata, dict) or metadata.get("rootConversationId") != session
            or metadata.get("parentConversationId") != ""
            or type(metadata.get("nestingDepth")) is not int or metadata["nestingDepth"] != 0):
        return None
    project = metadata.get("projectId")
    return project if isinstance(project, str) and 0 < len(project) <= 256 and project.isprintable() else None


def _bind_project(directory, executable, anchor):
    project = _project_for_root(_native(executable, "get-conversation-metadata", anchor), anchor)
    if project is None:
        raise ValueError("The native host could not verify the selected root conversation and its project")
    # A chat moved to a different project must not widen the previous grant after a host restart.
    with closing(sqlite3.connect(directory / "admissions.sqlite3", timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS project_scopes (anchor TEXT PRIMARY KEY, project TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO project_scopes VALUES (?,?)", (anchor, project))
        pinned = db.execute("SELECT project FROM project_scopes WHERE anchor=?", (anchor,)).fetchone()
        if pinned != (project,):
            raise ValueError("The anchor chat changed projects; select a root chat in the intended project")
    return project


def _trace_notice(session, notice):
    """Observe the original native input only; never assistant/tool quotations."""
    target = Path.home() / ".gemini/antigravity/brain" / _session(session) / ".system_generated/logs/transcript.jsonl"
    for path in [target, *target.parents]:
        if path.is_symlink() or path.is_junction():
            return None
    try:
        if target.stat().st_size > 64 * 1024 * 1024:
            return None
        identifier = re.search(r"wake-xdev-[a-f0-9]{24}", notice)
        if identifier is None:
            return None
        marker = identifier[0].encode("ascii")
        with target.open("rb") as stream:
            for raw in stream:
                if len(raw) > 1024 * 1024 or marker not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if (event.get("type") in {"USER_INPUT", "SYSTEM_MESSAGE"}
                        and isinstance(event.get("content"), str) and notice in event["content"]):
                    return {"kind": "native_original_session_notice", "type": event["type"],
                            "step_index": event.get("step_index"), "created_at": event.get("created_at")}
    except OSError:
        pass
    return None


def _handle(directory, executable, sessions, payload, *, project=None):
    if not isinstance(payload, dict):
        raise ValueError("Supply one native Sidecar request object")
    session = _session(payload.get("session"))
    if project is None and session not in sessions:
        raise ValueError("Conversation is not explicitly bound to this native Sidecar")
    if payload.get("op") == "inspect":
        result = _native(executable, "get-conversation-metadata", session)
        if project is not None and _project_for_root(result, session) != project:
            return {"ok": False, "status": "out_of_scope", "session": session, "client": "antigravity",
                    "model_calls": 0, "error_code": "ANTIGRAVITY_PROJECT_OR_CHAT_UNVERIFIED"}
        return {"ok": True, "status": "ready", "session": session, "client": "antigravity",
                "model_calls": 0, "native_api": "agentapi get-conversation-metadata",
                "delivery_scope": "original_session", "placement": "unverified", **_metadata_shape(result)}
    if payload.get("op") != "notify":
        raise ValueError("Unsupported native Sidecar operation")
    identifier, notice_id, notice = (payload.get(key) for key in ("message_id", "notice_id", "notice"))
    expected = "wake-xdev-" + hashlib.sha256(f"{session}\n{identifier}".encode()).hexdigest()[:24]
    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or notice_id != expected
            or not isinstance(notice, str) or len(notice) > 8192 or notice_id not in notice):
        raise ValueError("Invalid attributed exact-message notice")
    title = "Xdev external AI notice"
    if "subject" in payload:
        subject = payload["subject"]
        # This file also runs as a standalone sidecar. Bind the title to the
        # subject in the immutable notice whose digest the existing ledger uses.
        header, _, content = notice.partition("] ")
        if (not isinstance(subject, str) or not 0 < len(subject) <= 96
                or subject != " ".join(subject.split())
                or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in subject)
                or sum(2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1 for ch in subject) > 96
                or not header.startswith(f"[xdev external AI notice {notice_id}, from device ")
                or not content.startswith(f"主题 {json.dumps(subject, ensure_ascii=False)}。xdev 消息 ")):
            raise ValueError("Invalid native title or mismatch with the stored notice subject")
        title = subject
    digest = hashlib.sha256(notice.encode()).hexdigest()
    receipt = {"session": session, "client": "antigravity", "message_id": identifier,
               "notice_id": notice_id, "delivery_scope": "original_session", "placement": "unverified",
               "delivery_mode": "queue", "model_status": "unverified"}
    if project is not None:
        # Verify the address and authorized project before reserving this ID. A read-only failure is retryable.
        try:
            metadata = _native(executable, "get-conversation-metadata", session)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            return {**receipt, "ok": False, "status": "requires_activation",
                    "error_code": "ANTIGRAVITY_METADATA_UNAVAILABLE", "native_write_attempted": False}
        if _project_for_root(metadata, session) != project:
            return {**receipt, "ok": False, "status": "out_of_scope",
                    "error_code": "ANTIGRAVITY_PROJECT_OR_CHAT_UNVERIFIED", "native_write_attempted": False}
    with closing(sqlite3.connect(directory / "admissions.sqlite3", timeout=5)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS admissions (session TEXT,message_id TEXT,digest TEXT,state TEXT,evidence TEXT,PRIMARY KEY(session,message_id))")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT digest,state,evidence FROM admissions WHERE session=? AND message_id=?", (session, identifier)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError("The same notification ID already reserves a different payload")
            observed = json.loads(old[2]) if old[2] else _trace_notice(session, notice)
            accepted = old[1] == "accepted" or observed is not None
            if accepted and old[1] != "accepted":
                db.execute("UPDATE admissions SET state='accepted',evidence=? WHERE session=? AND message_id=?",
                           (json.dumps(observed), session, identifier))
                db.commit()
            return {**receipt, "ok": accepted, "status": "already_admitted" if accepted else "unknown",
                    "native_write_confirmed": accepted,
                    "admission_evidence": "native_original_session_notice" if accepted else "unverified",
                    "native_notice": observed,
                    **({} if accepted else {"outcome": "unknown"})}
        db.execute("INSERT INTO admissions VALUES (?,?,?,'unknown',NULL)", (session, identifier, digest))
        db.commit()
    try:
        # Existence is checked through the public API. No second daemon/writer is opened.
        if project is None:
            _native(executable, "get-conversation-metadata", session)
        response = _native(executable, "send-message", "--title=" + title, session, notice)
        if response.get("response") is None:
            raise ValueError("native API supplied no admission receipt")
        observed = _trace_notice(session, notice)
        if observed is None:
            return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                    "native_submission_returned": True, "native_write_confirmed": False,
                    "admission_evidence": "unverified", "error_code": "ANTIGRAVITY_NATIVE_NOTICE_NOT_OBSERVED",
                    "retry_policy": "inspect_same_notice_only_no_native_resend"}
        with closing(sqlite3.connect(directory / "admissions.sqlite3", timeout=5)) as db:
            db.execute("UPDATE admissions SET state='accepted',evidence=? WHERE session=? AND message_id=?",
                       (json.dumps(observed), session, identifier))
            db.commit()
        return {**receipt, "ok": True, "status": "admitted", "native_write_confirmed": True,
                "native_api": "agentapi send-message", "admission_evidence": "native_original_session_notice",
                "native_notice": observed}
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        return {**receipt, "ok": False, "status": "unknown", "outcome": "unknown",
                "native_write_confirmed": False, "error_class": type(error).__name__,
                "error_code": "ANTIGRAVITY_NATIVE_ADMISSION_UNVERIFIED"}


def _start_token():
    if os.name != "nt":
        return str(os.getpid())
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *[ctypes.byref(x) for x in times]):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def _prepare_bridge(directory):
    marker = directory / ".xdev-owned.json"
    if not directory.exists():
        parent_marker = directory.parent.parent / ".xdev-install.json"
        if _read(parent_marker) != {"app": "CrossDeviceCollab", "schema": 1}:
            raise ValueError("Initialize the Antigravity receiver under the selected xdev installation")
        directory.mkdir()
        marker.write_text(json.dumps({"kind": "antigravity-native", "version": 1}), encoding="utf-8")
    if marker.is_symlink() or (os.name == "nt" and marker.is_junction()) or not marker.is_file():
        raise ValueError("Sidecar bridge ownership marker is missing")


def sidecar(directory, executable, sessions, *, project_of=None):
    directory, executable = Path(directory), Path(executable)
    if (not directory.is_absolute() or directory.name != "antigravity-native" or directory.parent.name != "state"
            or not executable.is_absolute() or executable.name.lower() != "language_server.exe"):
        raise ValueError("Use the configured native Sidecar's dedicated paths")
    for item in [directory, *directory.parents]:
        if item.is_symlink() or item.is_junction():
            raise ValueError("Linked bridge paths are not allowed")
    # These are this process's native inherited plumbing; never export their values.
    if not os.environ.get("ANTIGRAVITY_LS_ADDRESS") or not os.environ.get("ANTIGRAVITY_CSRF_TOKEN"):
        raise RuntimeError("Launch the receiver as an explicitly enabled native Sidecar")
    _prepare_bridge(directory)
    sessions = [_session(value) for value in sessions or []]
    project = None
    if project_of is not None:
        if sessions:
            raise ValueError("Select fixed chats or the project of one existing root chat")
        project = _bind_project(directory, executable, _session(project_of))
    elif not sessions:
        raise ValueError("Select at least one existing conversation or its project")
    token = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if (self.path != "/rpc" or not hmac.compare_digest(self.headers.get("X-Xdev-Auth", ""), token)
                    or not 0 < size <= MAX_BYTES):
                self.send_error(403)
                return
            self.connection.settimeout(3)
            try:
                payload = json.loads(self.rfile.read(size))
                result = _handle(directory, executable, sessions, payload, project=project)
            except (OSError, ValueError, RuntimeError, TypeError, TimeoutError) as error:
                result = {"ok": False, "status": "rejected", "error_class": type(error).__name__}
            raw = json.dumps({"schema": SCHEMA, **result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    binding = {"schema": SCHEMA, "pid": os.getpid(), "process_start": _start_token(),
               "port": server.server_port, "token": token, "sessions": sessions,
               "native_parent_pid": os.getppid(), "created_at": datetime.now(timezone.utc).isoformat()}
    if project is not None:
        binding["scope"] = "verified_project_roots"
    binding_path = directory / "binding.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    print(json.dumps({"status": "ready", "schema": SCHEMA, "model_requests": 0}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if _read(binding_path).get("pid") == os.getpid():
                binding_path.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--agentapi", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--session", action="append", help="Limit delivery to one existing chat (repeatable)")
    scope.add_argument("--project-of", metavar="ROOT_CHAT_ID",
                       help="Resolve original chats in this root chat's project using native metadata on each delivery")
    args = parser.parse_args()
    sidecar(args.bridge, args.agentapi, args.session, project_of=args.project_of)
