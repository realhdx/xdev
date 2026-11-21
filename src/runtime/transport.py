"""Run a bounded request through the system OpenSSH client and a paired alias."""

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time

from src.runtime.device_names import DeviceDirectory


_ROUTING = ContextVar("xdev_call_routing", default=None)


@contextmanager
def routing_snapshot(root):
    """Keep one address mapping for a request, including parallel group deliveries."""
    token = _ROUTING.set((root, {}))
    try:
        yield
    finally:
        _ROUTING.reset(token)


SSH_OPTIONS = [
    "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
    "-o", "UpdateHostKeys=no", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=5",
    "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
]
DIRECT_OPTIONS = [
    "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
    "-o", "ControlPath=none", "-o", "ControlMaster=no",
    "-o", "ClearAllForwardings=yes", "-o", "ForwardX11=no",
    "-o", "PermitLocalCommand=no",
]
LINUX_MAIL_COMMAND = 'exec "$HOME/.local/bin/xdev-mail-rpc"'
REMOTE_COMMAND = r"""
$ProgressPreference = 'SilentlyContinue'
$entry = Join-Path $env:LOCALAPPDATA 'CrossDeviceCollab\xdev.cmd'
if (!(Test-Path -LiteralPath $entry -PathType Leaf)) {
  [Console]::Error.WriteLine('xdev is not installed for the target Windows user.')
  exit 127
}
& $entry _rpc
exit $LASTEXITCODE
"""


def home():
    override = os.environ.get("XDEV_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "CrossDeviceCollab"
    state = os.environ.get("XDG_STATE_HOME")
    return (Path(state) if state and Path(state).is_absolute() else Path.home() / ".local/state") / "xdev"


def config(root):
    path = root / "config.json"
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def directory(root):
    snapshot = _ROUTING.get()
    if snapshot is not None and snapshot[0] == root:
        cache = snapshot[1]
        if "directory" not in cache:
            cache.setdefault("directory", DeviceDirectory(config(root)).check_machine())
        return cache["directory"]
    return DeviceDirectory(config(root)).check_machine()


def node(root):
    return directory(root).node


def resolve(root, selector):
    return directory(root).resolve(selector)


def ssh_target(root, selector):
    return directory(root).ssh_target(selector)


def route(root, selector):
    devices = directory(root or home())
    return devices.resolve(selector), devices.ssh_target(selector)


def _ssh_options(root, selector, *, direct):
    from src.runtime.mail_routes import NotSubmitted
    args = ["ssh.exe" if os.name == "nt" else "ssh", "-G", *SSH_OPTIONS,
            *(DIRECT_OPTIONS if direct else ()), ssh_target(root, selector)]
    try:
        expanded = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=6)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise NotSubmitted("The local SSH client was unavailable before submission") from exc
    if expanded.returncode:
        raise ValueError("OpenSSH rejected the paired host configuration")
    return dict(line.split(" ", 1) for line in expanded.stdout.splitlines() if " " in line)


def ssh_uses_proxy(root, selector):
    """Preserve a legacy per-peer jump host when automatically choosing new carriers."""
    from src.runtime.mail_routes import NotSubmitted
    try:
        options = _ssh_options(root, selector, direct=False)
    except NotSubmitted:
        return False
    return any(options.get(k, "none").lower() != "none" for k in ("proxyjump", "proxycommand"))


def check_direct_connection(root, selector):
    """Test the configured direct socket before a failover-capable send writes any mail."""
    from src.runtime.mail_routes import NotSubmitted
    options = _ssh_options(root, selector, direct=True)
    hostname = options.get("hostname")
    port = int(options.get("port", 22))
    if not hostname or not 0 < port < 65536:
        raise ValueError("OpenSSH returned an invalid address for the paired host")
    try:
        with socket.create_connection((hostname, port), timeout=4):
            pass
    except OSError as exc:
        raise NotSubmitted("The paired SSH address could not be reached before submission") from exc


RESPONSE_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
CLEANUP_GRACE = 0.1


def _cleanup_process(proc):
    """Stop our child without waiting indefinitely for its inherited handles."""
    def stop():
        try:
            if proc.returncode is None:
                proc.kill()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=CLEANUP_GRACE)
        except (OSError, subprocess.TimeoutExpired):
            pass
        # A pipe close can itself wait on a concurrent blocked reader/writer.
        # Keep all such cleanup in this daemon, outside the request deadline.
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    worker = threading.Thread(target=stop, daemon=True)
    worker.start()
    worker.join(CLEANUP_GRACE)


class JsonRequestError(RuntimeError):
    """The single response could not be verified; remote side effects are unknown."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json_request(argv, wire, timeout):
    """Exchange one finite request/result, never a transfer lease or lock session."""
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Remote timeout must be a positive finite number")
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    frames = queue.Queue(maxsize=1)
    diagnostics = bytearray()
    diagnostic_lock = threading.Lock()
    diagnostic_done = threading.Event()

    def write_request():
        try:
            proc.stdin.write(wire)
            proc.stdin.close()  # _rpc reads one bounded request through EOF.
        except (OSError, ValueError):
            pass

    def read_response():
        try:
            # _rpc emits exactly one UTF-8 JSON line. A complete valid response
            # is the operation's result even if SSH/PowerShell never exits.
            frame = proc.stdout.readline(RESPONSE_LIMIT + 1)
        except (OSError, ValueError):
            frame = b""
        frames.put(frame)

    def read_diagnostics():
        try:
            while True:
                raw = proc.stderr.read(4096)
                if not raw:
                    return
                with diagnostic_lock:
                    diagnostics.extend(raw[:max(0, STDERR_LIMIT - len(diagnostics))])
        except (OSError, ValueError):
            pass
        finally:
            diagnostic_done.set()

    for action in (write_request, read_response, read_diagnostics):
        threading.Thread(target=action, daemon=True).start()
    try:
        try:
            frame = frames.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise JsonRequestError("SSH_TIMEOUT", "Request timed out; check remote status before retrying.")
        reply = None
        if len(frame) <= RESPONSE_LIMIT and frame.endswith(b"\n"):
            try:
                reply = json.loads(frame.decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                pass
        if isinstance(reply, dict) and type(reply.get("ok")) is bool:
            return reply
        if not frame:
            diagnostic_done.wait(min(0.1, max(0, deadline - time.monotonic())))
        with diagnostic_lock:
            lines = bytes(diagnostics).decode("utf-8", "replace").splitlines()
        if not frame and any(line.strip() == "Host key verification failed." or
                             "REMOTE HOST IDENTIFICATION HAS CHANGED!" in line for line in lines):
            raise JsonRequestError("SSH_HOST_KEY_REJECTED", "SSH could not verify the host key. "
                                   "Confirm the remote host fingerprint independently before updating the trusted key.")
        detail = next((line[:300] for line in lines if line and not line.startswith("#< CLIXML")), "")
        raise JsonRequestError("SSH_TRANSPORT_ERROR", detail or
                               "Remote protocol did not return one valid bounded JSON response line.")
    finally:
        _cleanup_process(proc)


def peer_request(target, message, timeout, *, root=None, direct=False):
    devices = directory(root or home())
    ssh = devices.ssh_target(target)
    if ssh is None:
        raise ValueError(f"Expected a remote device: {target}")
    args = ["ssh.exe" if os.name == "nt" else "ssh", *SSH_OPTIONS]
    if direct:
        args.extend(DIRECT_OPTIONS)
    args.append(ssh)
    if devices.rpc_profile(target) == "linux-mailbox":
        from src.runtime import messages, ssh_mail
        if (message.get("service"), message.get("action")) not in ssh_mail.OPERATIONS:
            raise ValueError("The mailbox SSH endpoint accepts delivery and addressed notifications only")
        message = {**message, "expected_device": devices.identity_key(target)}
        if message["service"] == "msg":
            envelope = message["payload"]
            message["payload"] = {**envelope, **messages.address_fields(envelope, devices.identity_key)}
        args.append(LINUX_MAIL_COMMAND)
    else:
        encoded = base64.b64encode(REMOTE_COMMAND.encode("utf-16-le")).decode("ascii")
        args.extend(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
    wire = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(wire) > 1_000_000:
        raise ValueError("Request exceeds 1 MB")
    try:
        return _json_request(args, wire, timeout)
    except JsonRequestError as exc:
        return {"ok": False, "error_code": exc.code, "outcome": "unknown", "error_message": str(exc)}
