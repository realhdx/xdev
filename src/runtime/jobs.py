"""Run an explicitly requested command independently of its SSH connection."""

from __future__ import annotations

import base64
import codecs
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time


_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}", re.IGNORECASE | re.ASCII)
_FINISHED = {"succeeded", "failed", "canceled", "interrupted"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TIMEOUT = 20
_MAX_LOG_BYTES = 128 * 1024

_WSL_RUNNER = r"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

cancel = Path(os.fsdecode(subprocess.check_output(
    ["wslpath", "-u", sys.argv[1]]
).rstrip(b"\r\n")))
confirmed = cancel.with_name("wsl-canceled")
if cancel.exists():
    confirmed.touch()
    sys.exit(130)
child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, start_new_session=True)
while child.poll() is None:
    if cancel.exists():
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(0.5)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        confirmed.touch()
        sys.exit(130)
    time.sleep(0.15)
sys.exit(child.returncode if child.returncode >= 0 else 128 - child.returncode)
"""


@contextlib.contextmanager
def _db(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(base / "index.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    request TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created REAL NOT NULL,
                    heartbeat REAL NOT NULL,
                    started REAL,
                    finished REAL,
                    returncode INTEGER,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                )"""
            )
            yield connection
    finally:
        connection.close()


def _job_id(payload: dict) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("job id must be 1–63 ASCII letters, digits, underscores or hyphens")
    return value.lower()


def _job_dir(base: Path, job_id: str) -> Path:
    return base / ("job-" + job_id)


def _task_name(base: Path, job_id: str) -> str:
    scope = hashlib.sha256(os.fsencode(os.path.normcase(str(base)))).hexdigest()[:16]
    return f"CrossDeviceCollab-Job-{scope}-{job_id}"


def _read(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("unknown job id")
    return row


def _reply(row: sqlite3.Row) -> dict:
    return {
        "ok": True,
        "id": row["id"],
        "state": row["state"],
        "created": row["created"],
        "started": row["started"],
        "finished": row["finished"],
        "returncode": row["returncode"],
        "cancel_requested": bool(row["cancel_requested"]),
        "error": row["error"],
    }


def _definition(payload: dict) -> str:
    argv = payload.get("argv")
    if (
        not isinstance(argv, list) or not argv or
        any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]
    ):
        raise ValueError("argv must be an array of strings beginning with a program")
    cwd, wsl = payload.get("cwd"), payload.get("wsl")
    if cwd is not None and (not isinstance(cwd, str) or not cwd or "\0" in cwd):
        raise ValueError("cwd must be a nonempty path")
    if wsl is not None and (
        not isinstance(wsl, str) or not wsl or wsl.startswith("-") or "\0" in wsl
    ):
        raise ValueError("wsl must be a distribution name")
    return json.dumps(
        {"argv": argv, "cwd": cwd, "wsl": wsl},
        ensure_ascii=False, separators=(",", ":"),
    )


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _scheduler(script: str) -> str:
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) /
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    source = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;\n" + script
    )
    payload = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", payload],
        stdin=subprocess.DEVNULL, capture_output=True,
        creationflags=_NO_WINDOW, timeout=_TIMEOUT,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace")
        if "Access is denied" in detail or "0x80070005" in detail:
            raise RuntimeError("Windows Task Scheduler denied the current user's request")
        raise RuntimeError("Windows Task Scheduler did not accept the job")
    return completed.stdout.decode("utf-8-sig", "replace").strip()


def _lookup_task(name: str) -> str:
    return _scheduler(
        f"$task=Get-ScheduledTask -TaskName {_ps_literal(name)} "
        "-TaskPath '\\' -ErrorAction SilentlyContinue; "
        "if ($task) { [string]$task.State } else { 'Absent' }"
    )


def _launch(base: Path, job_id: str) -> None:
    name = _task_name(base, job_id)
    arguments = subprocess.list2cmdline(
        [str(Path(__file__).resolve()), "_worker", str(base), job_id]
    )
    # pythonw.exe avoids opening a console in the signed-in user's desktop.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("The Windows Python runtime has no pythonw.exe")
    _scheduler(
        f"$name={_ps_literal(name)}; "
        "$task=Get-ScheduledTask -TaskName $name -TaskPath '\\' -ErrorAction SilentlyContinue; "
        "if (!$task) { "
        f"$action=New-ScheduledTaskAction -Execute {_ps_literal(pythonw)} "
        f"-Argument {_ps_literal(arguments)} -WorkingDirectory {_ps_literal(base)}; "
        "$principal=New-ScheduledTaskPrincipal "
        "-UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) "
        "-LogonType Interactive -RunLevel Limited; "
        "$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([timespan]::Zero) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Hidden; "
        "try { Register-ScheduledTask -TaskName $name -TaskPath '\\' -Action $action "
        "-Principal $principal -Settings $settings | Out-Null } "
        "catch { if (!(Get-ScheduledTask -TaskName $name -TaskPath '\\' "
        "-ErrorAction SilentlyContinue)) { throw } } "
        "}; "
        # An elevated SSH token can create a task whose limited token only has
        # read access to that registration. Give the same user rights to clean
        # up this task without altering the scheduler or running elevated.
        "$service=New-Object -ComObject 'Schedule.Service'; $service.Connect(); "
        "$registered=$service.GetFolder('\\').GetTask($name); "
        "$descriptor=$registered.GetSecurityDescriptor(4); "
        "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "$ace='(A;;GA;;;' + $sid + ')'; "
        "if (!$descriptor.Contains($ace)) { $registered.SetSecurityDescriptor($descriptor + $ace,0) }; "
        "Start-ScheduledTask -TaskName $name -TaskPath '\\'"
    )


def _unregister(base: Path, job_id: str) -> None:
    _scheduler(
        f"Unregister-ScheduledTask -TaskName {_ps_literal(_task_name(base, job_id))} "
        "-TaskPath '\\' -Confirm:$false -ErrorAction SilentlyContinue"
    )


def _finish(
    base: Path, job_id: str, previous: str, state: str,
    returncode: int | None = None, error: str | None = None,
) -> None:
    now = time.time()
    with _db(base) as connection:
        connection.execute(
            """UPDATE jobs SET state=?, finished=?, returncode=?, error=?, heartbeat=?
            WHERE id=? AND state=?""",
            (state, now, returncode, error, now, job_id, previous),
        )


def _status(base: Path, job_id: str) -> sqlite3.Row:
    with _db(base) as connection:
        row = _read(connection, job_id)
    if row["state"] in {"queued", "running"} and time.time() - row["heartbeat"] > 30:
        try:
            scheduled = _lookup_task(_task_name(base, job_id))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return row
        if scheduled in {"Absent", "Ready", "Disabled"}:
            _finish(
                base, job_id, row["state"], "interrupted", error=(
                    "Task did not start. The Windows user must be signed in."
                    if row["state"] == "queued" else
                    "The detached worker stopped without recording a result"
                ),
            )
            with _db(base) as connection:
                row = _read(connection, job_id)
    return row


def _start(base: Path, job_id: str, payload: dict) -> dict:
    definition = _definition(payload)
    now = time.time()
    with _db(base) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO jobs (id, request, state, created, heartbeat) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (job_id, definition, now, now),
        )
        duplicate = cursor.rowcount == 0
        row = _read(connection, job_id)
        if row["request"] != definition:
            raise ValueError("job id already belongs to a different command")
    if row["state"] == "queued":
        _job_dir(base, job_id).mkdir(exist_ok=True)
        try:
            _launch(base, job_id)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            # The start request may have reached Task Scheduler despite an error.
            # A retry with the same id may start it, but only one worker can claim it.
            with _db(base) as connection:
                row = _read(connection, job_id)
            if row["state"] == "queued":
                raise RuntimeError(
                    "Job was recorded, but Windows Task Scheduler did not confirm its start. "
                    "Check its status before retrying with the same id."
                ) from None
    with _db(base) as connection:
        result = _reply(_read(connection, job_id))
    result["duplicate"] = duplicate
    return result


def _cancel(base: Path, job_id: str) -> dict:
    _status(base, job_id)
    with _db(base) as connection:
        connection.execute(
            """UPDATE jobs SET cancel_requested=1,
            state=CASE WHEN state='queued' THEN 'canceled' ELSE state END,
            finished=CASE WHEN state='queued' THEN ? ELSE finished END
            WHERE id=? AND state IN ('queued','running')""",
            (time.time(), job_id),
        )
        row = _read(connection, job_id)
    if row["state"] == "running" and row["cancel_requested"]:
        # The detached worker owns process handles. A new SSH process must
        # never kill by PID read from disk.
        _job_dir(base, job_id).mkdir(exist_ok=True)
        (_job_dir(base, job_id) / "cancel").touch(exist_ok=True)
    return _reply(row)


def _log(base: Path, job_id: str, payload: dict, *, tail: bool) -> dict:
    row = _status(base, job_id)
    stream = payload.get("stream", "stdout")
    if stream not in ("stdout", "stderr"):
        raise ValueError("stream must be stdout or stderr")
    offset, limit = payload.get("offset", 0), payload.get("limit", 8192)
    if type(offset) is not int or offset < 0 or offset > 2**63 - 1:
        raise ValueError("offset must be a nonnegative byte offset")
    if type(limit) is not int or not 1 <= limit <= _MAX_LOG_BYTES:
        raise ValueError(f"limit must be between 1 and {_MAX_LOG_BYTES} bytes")
    try:
        file = (_job_dir(base, job_id) / (stream + ".log")).open("rb")
    except FileNotFoundError:
        size, data = 0, b""
        offset = 0 if tail else offset
    else:
        with file:
            size = os.fstat(file.fileno()).st_size
            offset = max(0, size - limit) if tail else offset
            file.seek(offset)
            data = file.read(max(0, min(size - offset, limit)))
            if tail and offset:
                # A tail may start in the middle of a UTF-8 character.
                first = next((i for i, byte in enumerate(data) if byte & 0xC0 != 0x80), len(data))
                offset, data = offset + first, data[first:]
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            decoder.decode(data, final=False)
            # Read up to three extra bytes to finish an ordinary UTF-8
            # character at the limit. Invalid output cannot bypass the limit.
            for _ in range(3):
                if not decoder.getstate()[0] or offset + len(data) >= size:
                    break
                extra = file.read(1)
                if not extra:
                    break
                data += extra
                decoder.decode(extra, final=False)
    terminal = row["state"] in _FINISHED
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=terminal and offset + len(data) >= size)
    unread = len(decoder.getstate()[0])
    next_offset = offset + len(data) - unread
    return {
        "ok": True, "id": job_id, "state": row["state"], "stream": stream,
        "offset": offset, "next_offset": next_offset, "size": size,
        "eof": next_offset >= size, "complete": terminal and next_offset >= size,
        "text": text,
    }


def handle(root: Path, action: str, payload: dict) -> dict:
    """Handle target-local job calls; root is this device's private state path."""
    if os.name != "nt":
        raise RuntimeError("The persistent job worker runs on the Windows host")
    if not isinstance(payload, dict):
        raise ValueError("job payload must be an object")
    base, job_id = root.resolve() / "jobs", _job_id(payload)
    try:
        if action == "start":
            return _start(base, job_id, payload)
        if action == "status":
            return _reply(_status(base, job_id))
        if action == "log" or action == "tail":
            return _log(base, job_id, payload, tail=action == "tail")
        if action == "cancel":
            return _cancel(base, job_id)
        raise ValueError("unknown job action")
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not access the job's local state ({type(exc).__name__})") from None


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


@contextlib.contextmanager
def _process_job():
    """Contain the child and its descendants without relying on stale PIDs."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        yield kernel, handle
    finally:
        kernel.CloseHandle(handle)


def _assign(kernel, job, child: subprocess.Popen) -> None:
    process = kernel.OpenProcess(0x0101, False, child.pid)  # SET_QUOTA | TERMINATE
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(process)


def _execute(base: Path, job_id: str) -> int:
    # Only the worker that attached this launcher to a Job Object holds the
    # pipe used to release the gate.
    if sys.stdin.buffer.read(2) != b"OK":
        return 125
    with _db(base) as connection:
        request = json.loads(_read(connection, job_id)["request"])
    argv, cwd, distro = request["argv"], request["cwd"], request["wsl"]
    if distro:
        argv = [
            "wsl.exe", "--distribution", distro, "--cd", cwd or "~",
            "--exec", "python3", "-c", _WSL_RUNNER,
            str(_job_dir(base, job_id) / "cancel"), *argv,
        ]
        cwd = None
    try:
        return subprocess.run(
            argv, cwd=cwd,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, check=False,
            env={**os.environ, "WSL_UTF8": "1"} if distro else None,
        ).returncode
    except (OSError, ValueError) as exc:
        print(f"Could not launch program ({type(exc).__name__}, code {getattr(exc, 'winerror', None)}).",
              file=sys.stderr)
        return 126


def _run(base: Path, job_id: str, distro: str | None) -> tuple[str, int | None, str | None]:
    folder = _job_dir(base, job_id)
    folder.mkdir(exist_ok=True)
    cancel = folder / "cancel"
    if cancel.exists():
        return "canceled", None, None
    python = Path(sys.executable).with_name("python.exe")
    child_args = [str(python), str(Path(__file__).resolve()), "_exec", str(base), job_id]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with (
        (folder / "stdout.log").open("wb") as stdout,
        (folder / "stderr.log").open("wb") as stderr,
        _process_job() as (kernel, job),
        subprocess.Popen(
            child_args, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
            cwd=Path.home(), env=env, creationflags=_NO_WINDOW,
        ) as child,
    ):
        try:
            _assign(kernel, job, child)
            child.stdin.write(b"OK")
            child.stdin.close()
            heartbeat = 0.0
            wsl_deadline = None
            while child.poll() is None:
                now = time.monotonic()
                if now - heartbeat >= 2:
                    with _db(base) as connection:
                        row = _read(connection, job_id)
                        connection.execute(
                            "UPDATE jobs SET heartbeat=? WHERE id=? AND state='running'",
                            (time.time(), job_id),
                        )
                    heartbeat = now
                    if row["cancel_requested"]:
                        cancel.touch(exist_ok=True)
                if cancel.exists():
                    if distro is None:
                        if not kernel.TerminateJobObject(job, 130):
                            raise ctypes.WinError(ctypes.get_last_error())
                        child.wait()
                        return "canceled", None, None
                    if wsl_deadline is None:
                        wsl_deadline = now + 30
                    elif now >= wsl_deadline:
                        kernel.TerminateJobObject(job, 130)
                        child.wait()
                        return "interrupted", None, "Could not verify WSL process-group cancellation"
                time.sleep(0.15)
            if distro and (folder / "wsl-canceled").exists():
                return "canceled", None, None
            return "succeeded" if child.returncode == 0 else "failed", child.returncode, None
        except BaseException:
            with contextlib.suppress(OSError):
                child.kill()
            raise


def _worker(base: Path, job_id: str) -> int:
    now = time.time()
    with _db(base) as connection:
        claim = connection.execute(
            """UPDATE jobs SET state='running', started=?, heartbeat=?
            WHERE id=? AND state='queued' AND cancel_requested=0""",
            (now, now, job_id),
        ).rowcount
        row = _read(connection, job_id)
    if claim:
        try:
            state, returncode, error = _run(base, job_id, json.loads(row["request"])["wsl"])
        except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
            state, returncode, error = "failed", None, f"Detached worker failed ({type(exc).__name__})"
        _finish(base, job_id, "running", state, returncode, error)
    elif row["state"] not in _FINISHED:
        return 0
    try:
        _unregister(base, job_id)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        # The result is durable even if Task Scheduler could not remove its
        # one-shot registration.
        pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4 or not _ID.fullmatch(sys.argv[3]):
        sys.exit(2)
    mode, state_path, identifier = sys.argv[1:]
    if mode == "_worker":
        sys.exit(_worker(Path(state_path), identifier.lower()))
    if mode == "_exec":
        sys.exit(_execute(Path(state_path), identifier.lower()))
    sys.exit(2)
