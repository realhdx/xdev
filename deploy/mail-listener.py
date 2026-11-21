"""Manage the current Windows user's background receiver for an xdev GitHub mailbox."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import uuid


DESCRIPTION = "xdev private GitHub mailbox receiver v1"
STARTUP_BEAT = 2
BEAT_SECONDS = 60
FRESH_SECONDS = 2 * BEAT_SECONDS + 10
SPOOL = "github-mail-listener-status.json"
TASK = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskName = 'xdev GitHub mailbox ' + $sid
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task -and $task.Description -ne $env:XDEV_MAIL_DESCRIPTION) { throw 'Task name already in use' }
if ($env:XDEV_MAIL_ACTION -eq 'restart') {
  if (-not $task -or $task.State -eq 'Disabled') { throw 'Receiver is absent or disabled' }
  $owner = $task.Principal.UserId
  if ($owner -ne $sid) { $owner = ([Security.Principal.NTAccount]::new($owner)).Translate([Security.Principal.SecurityIdentifier]).Value }
  if ($owner -ne $sid -or $task.Actions.Count -ne 1 -or
      $task.Actions[0].Execute -ne $env:XDEV_MAIL_PYTHON -or
      $task.Actions[0].Arguments -cne $env:XDEV_MAIL_ARGS) { throw 'Unexpected receiver task identity or command' }
}
if ($env:XDEV_MAIL_ACTION -in 'install','restart' -and $task -and $task.State -in 'Running','Queued') {
  Stop-ScheduledTask -TaskName $taskName
  $deadline = [DateTime]::UtcNow.AddSeconds(10)
  while ((Get-ScheduledTask -TaskName $taskName).State -in 'Running','Queued') {
    if ([DateTime]::UtcNow -ge $deadline) { throw 'Previous receiver still running' }
    Start-Sleep -Milliseconds 200
  }
}
switch ($env:XDEV_MAIL_ACTION) {
  'install' {
    $cmd = New-ScheduledTaskAction -Execute $env:XDEV_MAIL_PYTHON -Argument $env:XDEV_MAIL_ARGS
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
    $user = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $taskName -Action $cmd -Trigger $trigger -Principal $user -Settings $settings -Description $env:XDEV_MAIL_DESCRIPTION -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
  }
  'restart' { Start-ScheduledTask -TaskName $taskName }
  'remove' {
    if ($task) {
      if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
      Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
  }
}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$info = if ($task) { Get-ScheduledTaskInfo -TaskName $taskName }
$launch = if ($task -and $task.Actions.Count -eq 1 -and $task.Actions[0].Arguments -match ' --launch-id ([0-9a-f]{32})$') { $Matches[1] }
@{registered=[bool]$task; state=$(if ($task) { [string]$task.State } else { 'absent' }); launch_id=$launch; last_task_result=$(if ($info) { $info.LastTaskResult })} | ConvertTo-Json -Compress
"""


def check_release(root):
    pointer = (root / "current.txt").read_text(encoding="ascii").splitlines()[0]
    if not re.fullmatch("[0-9a-f]{64}", pointer):
        raise ValueError("Invalid installation pointer")
    version = json.loads((root / "releases" / pointer / "src/product.json").read_text(encoding="utf-8"))["version"]
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-preview\.(\d+))?", version)
    if not match:
        raise ValueError("Unrecognized xdev version")
    core = tuple(map(int, match.groups()[:3]))
    if core < (1, 1, 0) or (core == (1, 1, 0) and match[4] and int(match[4]) < 8):
        raise ValueError("This mailbox listener needs xdev 1.1.0-preview.8 or later")
    config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    if "github_mail" in config or not isinstance(config.get("github_mailbox"), dict):
        raise ValueError("Configure the local github_mailbox before starting the receiver")
    return pointer


def _save(path, record):
    path.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="ascii", delete=False) as target:
        staged = Path(target.name)
        json.dump(record, target)
    try:
        for attempt in range(4):
            try:
                staged.replace(path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(.05)
    finally:
        staged.unlink(missing_ok=True)


def _stored(path):
    try:
        value = json.loads(path.read_text(encoding="ascii"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _reason(error):
    if type(error).__name__ == "AccessDenied":
        return "github_access_denied"
    if isinstance(error, FileNotFoundError):
        return "required_file_missing"
    if isinstance(error, (ImportError, AttributeError)):
        return "runtime_load_failed"
    if isinstance(error, (ValueError, KeyError, IndexError)):
        return "configuration_or_provider_data_invalid"
    if isinstance(error, OSError):
        return "local_io_error"
    return "receiver_exited"


def _process_birth(pid):
    # A persisted heartbeat from a crashed process must not match a recycled PID.
    import ctypes
    from ctypes import wintypes
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if kernel.WaitForSingleObject(handle, 0) != 258 or not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _receiver(root, installed, launch_id):
    path = root / "state" / SPOOL
    record = {"launch_id": launch_id, "pid": os.getpid(), "generation": hashlib.sha256(installed.read_bytes()).hexdigest(),
              "process_birth": _process_birth(os.getpid()), "started_at": time.time(), "phase": "starting",
              "last_provider_event": "unconfirmed"}
    lock, stop = threading.Lock(), threading.Event()

    def update(**changes):
        with lock:
            record.update(changes, updated_at=time.time())
            _save(path, record)

    def heartbeat():
        delay = STARTUP_BEAT
        while not stop.wait(delay):
            update(phase="running")
            delay = BEAT_SECONDS

    def observed(event):
        # Keep only local diagnostics. The carrier owns the durable arrival events.
        provider = {"disconnected": "retry_reported", "rejected": "rejected", "asset_pending": "asset_pending",
                    "expired": "expired"}.get(event.get("status"), "activity_observed")
        reason = event.get("reason")
        if reason not in {"asset_not_visible", "incomplete_download", "asset_metadata_pending",
                          "download_digest_mismatch", "download_policy_rejected", "envelope_rejected",
                          "provider_metadata_rejected", "mailbox_rejected"}:
            reason = None
        update(last_provider_event=provider, last_provider_reason=reason, last_provider_at=time.time())

    pulse = None
    previous_stdin, previous_argv = sys.stdin, sys.argv
    try:
        update(stage="release_check")
        update(release=check_release(root), stage="load_runtime")
        with open(os.devnull, "r", encoding="utf-8") as source, open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                sys.stdin = source
                # The installed launcher selects the immutable release and its dependencies.
                launch = str(root / "launch.py")
                sys.argv = [launch, "msg", "events", "--transport", "github", "--limit", "1"]
                try:
                    runpy.run_path(launch, run_name="__main__")
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise RuntimeError("Mailbox event store preflight failed") from None
                from src.runtime import github_mail
                update(stage="configure_mailbox")
                github_mail.Settings(root)
                update(stage="follow")
                pulse = threading.Thread(target=heartbeat, daemon=True)
                pulse.start()
                github_mail.follow(root, observed)
                raise RuntimeError("Receiver returned")
    except (Exception, SystemExit) as exc:
        stop.set()
        if pulse is not None:
            pulse.join()
        update(phase="failed", reason=_reason(exc))
        return 1
    finally:
        stop.set()
        sys.stdin, sys.argv = previous_stdin, previous_argv


def _scheduler(action, pythonw, installed, launch_id):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    args = ["-I", "-B", "-X", "utf8", str(installed), "run", "--launch-id", launch_id]
    env = {**os.environ, "XDEV_MAIL_ACTION": action, "XDEV_MAIL_DESCRIPTION": DESCRIPTION,
           "XDEV_MAIL_PYTHON": str(pythonw), "XDEV_MAIL_ARGS": subprocess.list2cmdline(args)}
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", TASK], env=env,
                            capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise ValueError(f"Task Scheduler could not complete {action} (exit {result.returncode})")
    return json.loads(result.stdout)


def _status(task, state_path, generation):
    record = _stored(state_path)
    matches = bool(task.get("launch_id") and task["launch_id"] == record.get("launch_id")
                   and generation == record.get("generation"))
    current = record if matches else {}
    fresh = 0 <= time.time() - current.get("updated_at", 0) <= FRESH_SECONDS
    running = bool(task.get("state") == "Running" and fresh and current.get("phase") == "running"
                   and current.get("process_birth") and _process_birth(current.get("pid")) == current["process_birth"])
    return {**task, "worker_running": running, "phase": current.get("phase", "unconfirmed"),
            "stage": current.get("stage"), "reason": current.get("reason"),
            "last_provider_event": current.get("last_provider_event", "unconfirmed"), "updated_at": current.get("updated_at"),
            "last_provider_reason": current.get("last_provider_reason"), "last_provider_at": current.get("last_provider_at"),
            "release": current.get("release"), "diagnostics": str(state_path)}


def manage(action, launch_id=""):
    if os.name != "nt":
        raise ValueError("On Linux use xdev msg listen --transport github --follow in your user service")
    root = Path(os.environ["LOCALAPPDATA"]) / "CrossDeviceCollab"
    installed = root / "integrations" / "github-mail-listener.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    state_path = root / "state" / SPOOL
    if action == "run":
        if Path(__file__).resolve() != installed.resolve() or not re.fullmatch(r"[0-9a-f]{32}", launch_id):
            raise ValueError("Start the installed listener")
        sys.exit(_receiver(root, installed, launch_id))
    source = Path(__file__).read_bytes()
    generation = hashlib.sha256(source).hexdigest()
    if action in {"status", "restart"} and installed.is_file():
        generation = hashlib.sha256(installed.read_bytes()).hexdigest()
    previous_birth = None
    if action == "restart":
        release = check_release(root)
        existing = _scheduler("status", pythonw, installed, "")
        launch_id = existing.get("launch_id") or ""
        if not installed.is_file() or not existing.get("registered") or not re.fullmatch(r"[0-9a-f]{32}", launch_id):
            raise ValueError("Install the receiver before restarting it")
        previous_birth = _stored(state_path).get("process_birth")
    if action == "install":
        release = check_release(root)
        if not pythonw.is_file():
            raise ValueError("Use the Windows CPython interpreter installed for xdev")
        old = _status(_scheduler("status", pythonw, installed, ""), state_path, generation)
        if old["worker_running"] and old["release"] == release and installed.is_file() and installed.read_bytes() == source:
            return old
        launch_id = uuid.uuid4().hex
        installed.parent.mkdir(parents=True, exist_ok=True)
        if not installed.exists() or installed.read_bytes() != source:
            with tempfile.NamedTemporaryFile(dir=installed.parent, delete=False) as tmp:
                tmp.write(source)
            staged = Path(tmp.name)
            try:
                staged.replace(installed)
            finally:
                staged.unlink(missing_ok=True)
    task = _scheduler(action, pythonw, installed, launch_id)
    if action in {"install", "restart"}:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            worker = _stored(state_path)
            fresh = action == "install" or worker.get("process_birth") != previous_birth
            if fresh and worker.get("launch_id") == launch_id and worker.get("phase") in {"running", "failed"}:
                break
            time.sleep(.25)
        task = _scheduler("status", pythonw, installed, launch_id)
    if action == "remove" and installed.exists() and installed.read_bytes() == Path(__file__).read_bytes():
        installed.unlink()
    result = _status(task, state_path, generation)
    if action == "restart":
        current = _stored(state_path)
        result["restarted"] = bool(result["worker_running"] and result["release"] == release
                                   and current.get("launch_id") == launch_id
                                   and current.get("process_birth") != previous_birth)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "status", "restart", "remove", "run"])
    parser.add_argument("--launch-id", default="", help=argparse.SUPPRESS)
    try:
        args = parser.parse_args()
        receipt = manage(args.action, args.launch_id)
        if receipt is not None:
            print(json.dumps(receipt))
            if args.action == "install" and not receipt["worker_running"]:
                sys.exit(1)
            if args.action == "restart" and not receipt["restarted"]:
                sys.exit(1)
    except (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired) as error:
        if sys.stderr is not None and not sys.stderr.closed:
            print(str(error), file=sys.stderr)
        sys.exit(1)
