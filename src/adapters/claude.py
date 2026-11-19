"""Pinned native Claude inbox candidate; no CLI resume or permission changes.

Only a matching live 2.1.325 peerProtocol=1 registration can be written.
Published peerToken is read inside a bounded local helper, never returned.
A pipe write has no admission ACK: only the original user trace can confirm it.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
from uuid import UUID


VERSION = "2.1.325"
WRITE_TIMEOUT = 8
TRACE_BYTES = 64 * 1024 * 1024


def _canonical_session(value):
    try:
        result = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Supply the canonical UUID of the existing Claude chat") from exc
    if result != value.lower():
        raise ValueError("Supply the canonical UUID of the existing Claude chat")
    return result


def _regular(path, maximum):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > maximum):
        raise ValueError("Claude registration file is not a bounded regular file")
    return info


def _home(override):
    raw = override or os.environ.get("XDEV_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(raw).expanduser() if raw else Path.home() / ".claude"
    if not home.is_absolute():
        raise ValueError("Claude home must be an absolute path on the recipient device")
    return home


def _process(pid):
    """Read identity/time/image and compare owner to ourselves; never read env."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]

    def owner(handle):
        token = wintypes.HANDLE()
        if not security.OpenProcessToken(handle, 8, ctypes.byref(token)):
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

    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in (created, exited, system, user))):
            return None
        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return {"start": str((created.dwHighDateTime << 32) | created.dwLowDateTime),
                "image": buffer.value, "same_user": owner(handle) == owner(kernel.GetCurrentProcess()),
                "pid_domain": "win32:" + socket.gethostname().lower()}
    except (OSError, ValueError):
        return None
    finally:
        kernel.CloseHandle(handle)


def _pipe_name(value):
    # Pinned 2.1.325 CM/oye/yzo: rebuild \\.\pipe\ plus ASCII-lowercase
    # LOCAL\name, then sD hashes its UTF-8 bytes. The accepted alphabet is ASCII.
    if not isinstance(value, str):
        raise ValueError("Invalid Claude pipe")
    match = re.fullmatch(r"\\\\\.\\pipe\\(?:LOCAL\\)?(cc-msg-[0-9a-f]{32})", value, re.I)
    if match is None:
        raise ValueError("Invalid Claude native pipe namespace")
    return value.lower()


def _registrations(home, session):
    directory = home / "sessions"
    if not directory.is_dir() or directory.is_symlink():
        return []
    found = []
    for index, metadata in enumerate(directory.glob("*.json")):
        if index >= 1024:
            raise ValueError("Claude registration discovery limit reached")
        if not metadata.stem.isascii() or not metadata.stem.isdigit():
            continue
        try:
            _regular(metadata, 32768)
            row = json.loads(metadata.read_text(encoding="utf-8-sig"))
            if not isinstance(row, dict) or row.get("sessionId") != session:
                continue
            pid = int(metadata.stem)
            if (pid <= 0 or metadata.stem != str(pid) or row.get("pid") != pid or row.get("version") != VERSION
                    or type(row.get("peerProtocol")) is not int or row["peerProtocol"] != 1):
                continue
            process = _process(pid)
            if not process or not process["same_user"]:
                continue
            start = row.get("procStartFt", row.get("procStart"))
            if not isinstance(start, str) or start != process["start"]:
                continue
            if row.get("pidDomain") != process["pid_domain"]:
                continue
            image = process["image"].replace("\\", "/")
            if not re.search(r"/claude-code/2\.1\.325/[0-9a-f]{12}/claude\.exe$", image, re.I):
                continue
            pipe = row.get("messagingSocketPath")
            canonical = _pipe_name(pipe)
            key = directory / f"{pid}.{hashlib.sha256(canonical.encode()).hexdigest()}.key"
            _regular(key, 4096)  # Contents are deliberately not read here.
            found.append({"metadata": str(metadata), "key_file": str(key), "pipe": pipe,
                          "pid": pid, "start": start, "pid_domain": process["pid_domain"],
                          "session": session, "home": str(home), "version": VERSION, "peer_protocol": 1})
        except (OSError, ValueError, TypeError):
            continue
    return found


def _discover(home, session):
    found = _registrations(home, session)
    return found[0] if len(found) == 1 else None


def _candidate_homes():
    """Only the known user config and bounded Desktop managed-session trees."""
    candidates = [Path.home() / ".claude"]
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return candidates, False
    nodes = 0
    for family in ("Claude-3p", "Claude", "Claude-Data"):
        frontier = [Path(local) / family / "local-agent-mode-sessions"]
        for _ in range(3):
            following = []
            for parent in frontier:
                try:
                    with os.scandir(parent) as entries:
                        for count, entry in enumerate(entries):
                            nodes += 1
                            if count >= 32 or nodes > 192:
                                return candidates, True
                            if entry.is_dir(follow_symlinks=False):
                                following.append(Path(entry.path))
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError:
                    return candidates, True
            frontier = following
        candidates.extend(path / ".claude" for path in frontier)
    unique = {os.path.normcase(str(path)): path for path in candidates}
    return list(unique.values()), False


def _select_home(override, session):
    if override:
        candidates, incomplete = [_home(override)], False
    else:
        candidates, incomplete = _candidate_homes()
    active = []
    for home in candidates:
        try:
            active.extend((home, item) for item in _registrations(home, session))
        except (OSError, ValueError):
            incomplete = True
    if incomplete:
        return None, None, "discovery_incomplete"
    if len(active) > 1:
        return None, None, "ambiguous_live_registration"
    if active:
        return *active[0], None
    offline = [home for home in candidates if (home / "projects/session" / (session + ".jsonl")).is_file()]
    if len(offline) > 1:
        return None, None, "ambiguous_original_trace"
    if offline:
        return offline[0], None, "session_offline"
    return (candidates[0] if override else None), None, "no_live_registration"


def _peer_token(registration):
    path = Path(registration["key_file"])
    _regular(path, 4096)
    key = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(key, dict) or not isinstance(key.get("peerToken"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", key["peerToken"])
            or key.get("procStartFt", key.get("procStart")) != registration["start"]
            or key.get("pidDomain") != registration["pid_domain"]):
        raise ValueError("Published Claude peer key does not match the registered process")
    return key["peerToken"]


def _open_pipe(registration):
    """OPEN_EXISTING, then bind the actual pipe server PID to the registry."""
    if sys.platform != "win32":
        raise OSError("Native Windows pipe required")
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(registration["pipe"], 0x40000000, 0, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("Claude pipe could not be opened")
    try:
        server = wintypes.DWORD()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(server)) or server.value != registration["pid"]:
            raise ValueError("Claude pipe server does not match the registered PID")
        process = _process(server.value)
        if not process or not process["same_user"] or process["start"] != registration["start"]:
            raise ValueError("Claude pipe process changed before write")
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY | os.O_BINARY)
        handle = None  # The CRT descriptor now owns the handle.
        try:
            return os.fdopen(descriptor, "wb", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


def _worker(request):
    expected = request["registration"]
    current = _discover(Path(expected["home"]), expected["session"])
    if current != expected:
        raise ValueError("Claude registration changed before write")
    token = _peer_token(current)
    frames = [{"type": "auth", "token": token},
              {"type": "user", "session_id": current["session"],
               "message": {"role": "user", "content": request["notice"]}}]
    wire = ("\n".join(json.dumps(frame, ensure_ascii=False) for frame in frames) + "\n").encode("utf-8")
    with _open_pipe(current) as pipe:
        view = memoryview(wire)
        while view:
            written = pipe.write(view)
            if not written:
                raise OSError("Incomplete Claude pipe write")
            view = view[written:]
    return {"ok": True, "write_status": "written_unverified", "auth_kind": "published_peer"}


def _write_bounded(registration, notice):
    payload = json.dumps({"registration": registration, "notice": notice}).encode("utf-8")
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--pipe-write"],
                                input=payload, capture_output=True, timeout=WRITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"write_status": "unknown", "error_code": "CLAUDE_PIPE_TIMEOUT"}
    except OSError:
        return {"write_status": "unknown", "error_code": "CLAUDE_HELPER_UNAVAILABLE"}
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        response = {}
    if result.returncode or not isinstance(response, dict) or response.get("write_status") != "written_unverified":
        return {"write_status": "unknown", "error_code": "CLAUDE_PIPE_WRITE_UNVERIFIED"}
    return {"write_status": "written_unverified", "auth_kind": "published_peer"}


def _trace_marker(home, session, marker):
    # The native Desktop managed-session layout. Do not scan unrelated chats.
    path = home / "projects" / "session" / (session + ".jsonl")
    try:
        _regular(path, 1024 * 1024 * 1024)
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > TRACE_BYTES:
                stream.seek(size - TRACE_BYTES)
                stream.readline()
            while line := stream.readline(1024 * 1024 + 1):
                if len(line) > 1024 * 1024:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(1024 * 1024 + 1)
                    continue
                if marker.encode("utf-8") not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "user" or row.get("sessionId", session) != session:
                    continue
                content = (row.get("message") or {}).get("content")
                texts = [content] if isinstance(content, str) else [
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ] if isinstance(content, list) else []
                if any(marker in text for text in texts if isinstance(text, str)):
                    return {"evidence": "original_trace_user_marker", "timestamp": row.get("timestamp")}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _admissions(root):
    directory = root / "state"
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "claude-admissions.sqlite3", timeout=5)
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS admissions (
            session TEXT NOT NULL, message_id TEXT NOT NULL, home TEXT NOT NULL,
            notice_id TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(session,message_id))""")
    return db


def _unknown(receipt, **details):
    return {"ok": False, "status": "unknown", "outcome": "unknown", "model_status": "unverified",
            "error_code": "CLAUDE_ADMISSION_UNVERIFIED", "retry_policy": "check_only_no_rewrite",
            "error_message": "No admission ACK. Check the exact original chat marker or receipt; this ID will not be written again.",
            **receipt, **details}


def _activation(receipt, reason):
    return {"ok": False, "status": "requires_activation", "error_code": "CLAUDE_REQUIRES_ACTIVATION",
            **receipt, "requires_activation": True, "gui_required": True, "diagnostic_code": reason,
            "next_action": "Activate the exact original Claude Desktop chat and submit a short attributed notice with notice_id; verify its marker/receipt. No CLI resume or permission changes are performed."}


def _submit_legacy(root, identifier, session, steer=False, idle_only=False, claude_home=None):
    if steer or idle_only:
        raise ValueError("Claude native candidate does not support steer or idle-only")
    session = _canonical_session(session)
    from src.runtime import messages
    query = messages.handle(root, "wait", {"id": identifier, "timeout": 0})
    item = query.get("message")
    if query.get("status") != "received" or not item:
        raise ValueError("This message is not in the local inbox")
    notice_id = "wake-xdev-" + hashlib.sha256(f"claude\n{session}\n{identifier}".encode()).hexdigest()[:24]
    marker = f"[xdev external AI notice {notice_id}]"
    with closing(_admissions(root)) as db:
        previous = db.execute("SELECT home,admitted FROM admissions WHERE session=? AND message_id=?",
                              (session, identifier)).fetchone()
        override = claude_home or os.environ.get("XDEV_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
        if previous and not override:
            home, registration, reason = Path(previous[0]), None, None
        else:
            home, registration, reason = _select_home(override, session)
        receipt = {"client": "claude", "session": session, "message_id": identifier,
                   "notice_id": notice_id, "claude_home": str(home) if home is not None else None,
                   "model_status": "unverified"}
        if home is None:
            return _activation(receipt, reason)
        if previous and os.path.normcase(previous[0]) != os.path.normcase(str(home)):
            return _unknown(receipt, error_code="CLAUDE_HOME_CONFLICT")
        if previous and previous[1]:
            return {"ok": True, "status": "already_admitted", **receipt}
        confirmed = _trace_marker(home, session, marker)
        if confirmed:
            with db:
                db.execute("INSERT INTO admissions(session,message_id,home,notice_id,admitted) VALUES(?,?,?,?,1) "
                           "ON CONFLICT(session,message_id) DO UPDATE SET admitted=1",
                           (session, identifier, str(home), notice_id))
            return {"ok": True, "status": "admitted", **receipt, **confirmed}
        if previous:
            return _unknown(receipt)
        if registration is None:
            return _activation(receipt, reason or "no_live_registration")
        with db:
            reserved = db.execute("INSERT OR IGNORE INTO admissions(session,message_id,home,notice_id) VALUES(?,?,?,?)",
                                  (session, identifier, str(home), notice_id)).rowcount
        if not reserved:
            return _unknown(receipt)
    from src.adapters.notices import body as notice_body
    notice = marker + " " + notice_body(item, identifier, "claude", session, claude_home)
    written = _write_bounded(registration, notice)
    confirmed = _trace_marker(home, session, marker)
    if confirmed:
        try:
            with closing(_admissions(root)) as db:
                with db:
                    db.execute("UPDATE admissions SET admitted=1 WHERE session=? AND message_id=?", (session, identifier))
        except sqlite3.Error:
            return _unknown(receipt, error_code="CLAUDE_ACK_PERSIST_UNVERIFIED", **written)
        return {"ok": True, "status": "admitted", **receipt, **confirmed}
    return _unknown(receipt, **written)


def submit(root, identifier, session, steer=False, idle_only=False, claude_home=None):
    from src.adapters import official_channel
    with official_channel.delivery_lock(root, identifier, session, client="claude"):
        previous = official_channel.channel_prior(root, identifier, session, client="claude")
        if previous is not None:
            return {**previous, "backend_switch_blocked": True}
        return _submit_legacy(root, identifier, session, steer, idle_only, claude_home)


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--pipe-write"]:
            raise ValueError("Internal helper only")
        request = json.loads(sys.stdin.buffer.read(65537))
        answer = _worker(request)
        print(json.dumps(answer), flush=True)
    except BaseException:
        # Never serialize exception text or a key file/token to the parent.
        print('{"ok":false,"write_status":"unknown"}', flush=True)
        raise SystemExit(1)
