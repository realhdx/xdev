"""Prefer registered native send-now; retain the official queue as an explicit fallback."""

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from uuid import UUID

from src.runtime import messages


def _executable(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file() or (sys.platform == "win32" and path.suffix.lower() != ".exe"):
        raise RuntimeError("Codex needs an existing native executable; set XDEV_CODEX_CLI.")
    return path.resolve()


def _running_desktop_cli():
    if sys.platform != "win32":
        return None
    # Read executable paths, without inspecting process credentials/environment.
    script = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "@(Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
        "Where-Object {$_.ExecutablePath -like '*\\WindowsApps\\OpenAI.Codex_*\\app\\resources\\codex.exe' "
        "-and $_.CommandLine -match '(?:^|\\s)app-server(?:\\s|$)'} | "
        "Select-Object -ExpandProperty ExecutablePath -Unique) | ConvertTo-Json -Compress"
    )
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        paths = json.loads(result.stdout.decode("utf-8-sig")) if result.returncode == 0 else []
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return None
    if len(paths) > 1:
        raise RuntimeError("Multiple Codex Desktop versions are running; set XDEV_CODEX_CLI explicitly.")
    return _executable(paths[0]) if paths else None


def _cli():
    for variable in ("XDEV_CODEX_CLI", "CODEX_CLI_PATH"):
        if os.environ.get(variable):
            return _executable(os.environ[variable])
    resources = os.environ.get("CODEX_ELECTRON_RESOURCES_PATH")
    if resources and (Path(resources) / "codex.exe").is_file():
        return _executable(Path(resources) / "codex.exe")
    running = _running_desktop_cli()
    if running:
        return running
    if sys.platform == "win32":
        packages = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        try:
            candidates = []
            for path in packages.glob("OpenAI.Codex_*/app/resources/codex.exe"):
                match = re.fullmatch(r"OpenAI\.Codex_(\d+\.\d+\.\d+\.\d+)_(?:x64|arm64)__[A-Za-z0-9]+",
                                     path.parents[2].name)
                if match:
                    candidates.append((tuple(map(int, match[1].split("."))), path))
            if candidates:
                return _executable(max(candidates, key=lambda item: item[0])[1])
        except OSError:
            pass
    found = shutil.which("codex.exe" if sys.platform == "win32" else "codex")
    if found:
        return _executable(found)
    raise RuntimeError("No native Codex CLI found; set XDEV_CODEX_CLI to the installed executable.")


def _home(override):
    raw = override or os.environ.get("XDEV_CODEX_HOME") or os.environ.get("CODEX_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".codex"
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Codex home must be an existing absolute directory; use --codex-home or XDEV_CODEX_HOME.")
    return path.resolve()


def _admissions(root):
    directory = root / "state"
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "codex-admissions.sqlite3", timeout=10)
    with connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS admissions (
            session TEXT NOT NULL, message_id TEXT NOT NULL,
            notice_id TEXT NOT NULL, codex_home TEXT NOT NULL, cli_path TEXT NOT NULL,
            queue_id TEXT, queued INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session, message_id))""")
    return connection


def _unknown(receipt, code="CODEX_ADMISSION_UNVERIFIED", **details):
    return {"ok": False, "status": "unknown", "outcome": "unknown", "error_code": code,
            "error_message": "Check the target chat/queue for notice_id before retrying; xdev will not submit it again.",
            "model_status": "unverified", **receipt, **details}


def _existing(connection, receipt):
    row = connection.execute(
        "SELECT notice_id,codex_home,cli_path,queue_id,queued FROM admissions WHERE session=? AND message_id=?",
        (receipt["session"], receipt["message_id"]),
    ).fetchone()
    if row is None:
        return None
    stored = {**receipt, "notice_id": row[0], "codex_home": row[1], "cli_path": row[2], "queue_id": row[3]}
    stored["delivery_mode"] = "queue"
    stored["current_turn_accepted"] = False
    if os.path.normcase(row[1]) != os.path.normcase(receipt["codex_home"]):
        return _unknown(stored, "CODEX_HOME_CONFLICT")
    if row[4]:
        return {"ok": True, "status": "already_queued", "model_status": "unverified", **stored}
    return _unknown(stored)


def _failure_details(process):
    # Classify a few native diagnostics without returning potentially sensitive stderr.
    error = (process.stderr or b"")[:8192].decode("utf-8-sig", errors="replace").lower()
    diagnostic = "native_response_unverified"
    if "unrecognized subcommand 'queue'" in error or 'unrecognized subcommand "queue"' in error:
        diagnostic = "queue_unsupported_by_selected_cli"
    elif "thread not found" in error or "no active session found" in error:
        diagnostic = "target_not_found_in_selected_home"
    elif "is archived" in error:
        diagnostic = "target_archived"
    return {"cli_exit_code": process.returncode, "failure_stage": "native_queue",
            "diagnostic_code": diagnostic}


@contextmanager
def _delivery_lock(root, session, identifier):
    """Serialize backend selection for one message across processes and threads."""
    folder = root / "state" / "codex-delivery-locks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (sha256((session + "\n" + identifier).encode()).hexdigest() + ".lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 10
        acquired = False
        while not acquired:
            stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another delivery for this exact message is still active")
                time.sleep(0.02)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def submit(root, identifier, session, steer=False, idle_only=False, codex_home=None, *, force_queue=False):
    from src.adapters.sessions import validate
    if isinstance(codex_home, os.PathLike):
        codex_home = os.fspath(codex_home)
    validate("codex", session, steer, idle_only, codex_home)
    if type(force_queue) is not bool:
        raise ValueError("force_queue must be a boolean")
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
        raise ValueError("Supply the stored message ID")
    session = str(UUID(session))
    # Legacy reserved attempts are already durable unknown results. Return them
    # promptly without waiting for the owner's client call or changing backend.
    if (root / "state" / "codex-admissions.sqlite3").is_file():
        home = _home(codex_home)
        receipt = {"client": "codex", "session": session, "message_id": identifier,
                   "codex_home": str(home)}
        with closing(_admissions(root)) as connection:
            previous = _existing(connection, receipt)
        if previous is not None:
            return previous
    try:
        with _delivery_lock(root, session, identifier):
            return _submit_locked(root, identifier, session, steer, idle_only, codex_home, force_queue=force_queue)
    except TimeoutError:
        return _unknown({"client": "codex", "session": session, "message_id": identifier}, "CODEX_ADMISSION_BUSY")


def _submit_locked(root, identifier, session, steer=False, idle_only=False, codex_home=None, *, force_queue=False):
    """Queue acceptance is durable; model wake/read/reply require separate evidence."""
    if steer or idle_only:
        raise ValueError("Codex queue does not support --steer or --idle-only; observe the target separately.")
    try:
        canonical = str(UUID(session))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Supply the UUID of an existing Codex chat.") from exc
    if canonical != session.lower():
        raise ValueError("Supply the canonical UUID of an existing Codex chat.")
    session = canonical
    query = messages.handle(root, "wait", {"id": identifier, "timeout": 0})
    item = query.get("message")
    if query.get("status") != "received" or not item:
        raise ValueError("This message is not in the local inbox.")
    home = _home(codex_home)
    notice_id = "wake-xdev-" + sha256(f"{session}\n{identifier}".encode()).hexdigest()[:24]
    receipt = {"client": "codex", "message_id": identifier, "session": session,
               "notice_id": notice_id, "codex_home": str(home)}
    with closing(_admissions(root)) as connection:
        previous = _existing(connection, receipt)
    if previous is not None:
        return previous
    from src.adapters.notices import body as notice_body
    notice = f"[xdev notice {notice_id}, from {json.dumps(item['from'], ensure_ascii=False)}] " + notice_body(item, identifier, "codex", session, codex_home)
    from src.adapters import codex_receiver
    previous_native = codex_receiver.peek(root, session, identifier, notice)
    if previous_native is not None:
        return {**receipt, **previous_native}
    if not force_queue:
        native = codex_receiver.notify(root, session, notice, identifier)
        if native.get("status") != "not_registered":
            # Once native submission was reserved, an unknown outcome must never
            # fall through to the queue and create a duplicate model turn.
            return {**receipt, **native}
        return {**receipt, **native, "ok": False, "status": "requires_activation",
                "requires_activation": True, "gui_required": False,
                "delivery_mode": "send_now", "error_code": "CODEX_NATIVE_RECEIVER_REQUIRES_REGISTRATION",
                "next_action": "Register this native host from its own Codex executor with xdev ai --register. Use --queue only when delayed delivery is explicitly intended."}
    cli = _cli()
    receipt["cli_path"] = str(cli)
    argv = [str(cli), "queue", "--thread", session, "--message", notice]
    with closing(_admissions(root)) as connection:
        with connection:
            reserved = connection.execute(
                "INSERT OR IGNORE INTO admissions (session,message_id,notice_id,codex_home,cli_path) VALUES (?,?,?,?,?)",
                (session, identifier, notice_id, str(home), str(cli)),
            ).rowcount
        if not reserved:
            return _existing(connection, receipt)
    try:
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                 env={**os.environ, "CODEX_HOME": str(home)}, timeout=45)
    except subprocess.TimeoutExpired:
        return _unknown(receipt, "CODEX_TIMEOUT")
    except OSError:
        return _unknown(receipt, "CODEX_START_UNVERIFIED")
    try:
        output = process.stdout.decode("utf-8-sig")
    except UnicodeError:
        output = ""
    acknowledgements = re.findall(
        r"^Queued message ([0-9a-fA-F-]{36}) for thread ([0-9a-fA-F-]{36})\.[ \t\r]*$", output, re.MULTILINE)
    queue_id = None
    if len(acknowledgements) == 1:
        raw_id, target = acknowledgements[0]
        try:
            if str(UUID(target)) == session:
                queue_id = str(UUID(raw_id))
        except ValueError:
            pass
    if process.returncode or queue_id is None:
        return _unknown(receipt, **_failure_details(process))
    try:
        with closing(_admissions(root)) as connection:
            with connection:
                connection.execute("UPDATE admissions SET queued=1,queue_id=? WHERE session=? AND message_id=?",
                                   (queue_id, session, identifier))
    except sqlite3.Error:
        # The reserved attempt remains durable even if recording its ACK fails.
        return _unknown({**receipt, "queue_id": queue_id}, "CODEX_ACK_PERSIST_UNVERIFIED")
    return {"ok": True, "status": "queued", "queue_id": queue_id,
            "model_status": "unverified", "delivery_mode": "queue",
            "native_receiver_status": "explicit_queue" if force_queue else "not_registered", **receipt}
