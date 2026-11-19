"""Client for the native bridge published to our own Codex executor.

No pipe discovery, app-server launch, provider credentials, or fabricated
executor identity. Submission callers must supply real per-call metadata.
The installed desktop bridge uses four-byte little-endian JSON frames.
"""
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import PureWindowsPath
import queue
import struct
import sys
import threading
import time
from uuid import UUID, uuid4

from src.adapters.claude import _process


MAX_FRAME = 8 * 1024 * 1024


class NativeBridgeError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def environment_available():
    return bool(os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"))


def _pipe_from_environment():
    value = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
    if not value.startswith("\\\\.\\pipe\\") or "\0" in value or len(value) > 1024:
        raise NativeBridgeError("CODEX_NATIVE_ENV_UNAVAILABLE")
    return value


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.WriteFile.argtypes = kernel.ReadFile.argtypes
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    return kernel


def _host_identity(kernel, handle):
    pid = wintypes.DWORD()
    if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        raise NativeBridgeError("CODEX_NATIVE_SERVER_UNVERIFIED")
    identity = _process(pid.value)
    image = PureWindowsPath(identity["image"]) if identity else None
    # The real desktop app, rather than another MCP or CLI child, owns this pipe.
    if (not identity or not identity["same_user"] or image.name.lower() not in {"chatgpt.exe", "codex.exe"}
            or image.parent.name.lower() != "app"
            or image.parents[2] != PureWindowsPath(os.environ.get("ProgramFiles", "C:/Program Files")) / "WindowsApps"
            or not image.parent.parent.name.lower().startswith("openai.codex_")):
        raise NativeBridgeError("CODEX_NATIVE_SERVER_UNVERIFIED")
    return pid.value, identity["start"]


def _exchange_native(pipe, request, timeout, expected=None, describe_only=False):
    if sys.platform != "win32":
        raise NativeBridgeError("CODEX_NATIVE_WINDOWS_REQUIRED")
    kernel = _kernel()
    deadline = time.monotonic() + timeout
    wire = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    if len(wire) > MAX_FRAME:
        raise ValueError("Codex native request exceeds 8 MiB")
    if not kernel.WaitNamedPipeW(pipe, max(1, int(timeout * 1000))):
        raise NativeBridgeError("CODEX_NATIVE_PIPE_UNAVAILABLE")
    handle = kernel.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise NativeBridgeError("CODEX_NATIVE_PIPE_UNAVAILABLE")
    try:
        identity = _host_identity(kernel, handle)
        if expected is not None and identity != (expected["host_pid"], expected["host_started_at"]):
            raise NativeBridgeError("CODEX_NATIVE_REGISTERED_HOST_CHANGED")
        if describe_only:
            return {"pipe_path": pipe, "host_pid": identity[0], "host_started_at": identity[1]}
        def transfer(raw=None, size=None):
            output = bytearray()
            offset = 0
            while offset < (len(raw) if raw is not None else size):
                if time.monotonic() >= deadline:
                    raise NativeBridgeError("CODEX_NATIVE_TIMEOUT_UNKNOWN")
                count = min(65536, (len(raw) if raw is not None else size) - offset)
                buffer = ctypes.create_string_buffer(raw[offset:offset + count] if raw is not None else count)
                done = wintypes.DWORD()
                operation = kernel.WriteFile if raw is not None else kernel.ReadFile
                if not operation(handle, buffer, count, ctypes.byref(done), None) or not done.value:
                    raise NativeBridgeError("CODEX_NATIVE_IO_UNVERIFIED")
                if raw is None:
                    output.extend(buffer.raw[:done.value])
                offset += done.value
            return bytes(output)
        transfer(struct.pack("<I", len(wire)) + wire)
        length = struct.unpack("<I", transfer(size=4))[0]
        if not 0 < length <= MAX_FRAME:
            raise NativeBridgeError("CODEX_NATIVE_RESPONSE_INVALID")
        try:
            response = json.loads(transfer(size=length).decode("utf-8"))
        except (UnicodeError, ValueError):
            raise NativeBridgeError("CODEX_NATIVE_RESPONSE_INVALID") from None
        if _host_identity(kernel, handle) != identity:
            raise NativeBridgeError("CODEX_NATIVE_SERVER_CHANGED")
        return response
    finally:
        kernel.CloseHandle(handle)


def _bounded(action, timeout):
    timeout = float(timeout)
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Codex native timeout must be within (0, 30] seconds")
    completed = queue.Queue(maxsize=1)
    def work():
        try:
            value = action()
        except Exception as exc:
            completed.put(exc)
        else:
            completed.put(value)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        result = completed.get(timeout=timeout)
    except queue.Empty:
        # Cancel only this helper's synchronous Windows I/O, not any host turn.
        if sys.platform == "win32" and worker.native_id:
            kernel = _kernel()
            thread = kernel.OpenThread(1, False, worker.native_id)
            if thread:
                try:
                    kernel.CancelSynchronousIo(thread)
                finally:
                    kernel.CloseHandle(thread)
        raise NativeBridgeError("CODEX_NATIVE_TIMEOUT_UNKNOWN") from None
    if isinstance(result, Exception):
        if isinstance(result, NativeBridgeError):
            raise result
        raise NativeBridgeError("CODEX_NATIVE_REQUEST_UNVERIFIED") from None
    return result


def _registration(value):
    if (not isinstance(value, dict) or type(value.get("host_pid")) is not int or value["host_pid"] <= 0
            or not isinstance(value.get("host_started_at"), str) or not value["host_started_at"].isdigit()
            or not isinstance(value.get("pipe_path"), str)):
        raise NativeBridgeError("CODEX_NATIVE_REGISTRATION_INVALID")
    pipe = value["pipe_path"]
    # Apply the same local-only pipe rule without changing the process env.
    if not pipe.startswith("\\\\.\\pipe\\") or "\0" in pipe or len(pipe) > 1024:
        raise NativeBridgeError("CODEX_NATIVE_REGISTRATION_INVALID")
    return {key: value[key] for key in ("pipe_path", "host_pid", "host_started_at")}


def describe_host(timeout=3):
    """Verify only the native pipe our own host published; keep the result private."""
    pipe = _pipe_from_environment()
    return _bounded(lambda: _exchange_native(pipe, {}, float(timeout), describe_only=True), timeout)


def _request(method, params, timeout=8, *, registration=None, request_id=None):
    pipe = _pipe_from_environment() if registration is None else _registration(registration)["pipe_path"]
    request = {"id": request_id or str(uuid4()), "jsonrpc": "2.0", "method": method, "params": params}
    if registration is None:
        result = _bounded(lambda: _exchange_native(pipe, request, float(timeout)), timeout)
    else:
        registered = _registration(registration)
        result = _bounded(lambda: _exchange_native(pipe, request, float(timeout), registered), timeout)
    if not isinstance(result, dict) or result.get("id") != request["id"] or result.get("jsonrpc") != "2.0":
        raise NativeBridgeError("CODEX_NATIVE_RESPONSE_INVALID")
    if "error" in result:
        raise NativeBridgeError("CODEX_NATIVE_REQUEST_REJECTED")
    if "result" not in result:
        raise NativeBridgeError("CODEX_NATIVE_RESPONSE_INVALID")
    return result["result"]


def list_tools(timeout=8):
    result = _request("tools/list", {"threadStartKind": "all"}, timeout)
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise NativeBridgeError("CODEX_NATIVE_CATALOG_INVALID")
    return result["tools"]



def executor_from_mcp_meta(meta):
    """Extract real host-supplied executor fields without inventing a turn/call."""
    from src.adapters.caller_context import normalize
    identity = normalize(meta)
    if identity["status"] != "ready" or identity["client"] != "codex":
        raise NativeBridgeError("CODEX_NATIVE_EXECUTOR_META_REQUIRED")
    if not isinstance(meta, dict):
        meta = meta.model_dump(by_alias=True)
    nested = meta.get("x-codex-turn-metadata", {})
    if isinstance(nested, str):
        nested = json.loads(nested)
    if not isinstance(nested, dict):
        raise NativeBridgeError("CODEX_NATIVE_EXECUTOR_META_REQUIRED")
    executor = {"threadId": meta.get("threadId", nested.get("thread_id")),
                "turnId": nested.get("turn_id"), "callId": meta.get("callId")}
    if any(not isinstance(value, str) or not value.strip() for value in executor.values()):
        raise NativeBridgeError("CODEX_NATIVE_EXECUTOR_META_REQUIRED")
    return executor

def call_tool(tool, arguments, executor_meta, timeout=8):
    """Use actual caller metadata supplied by the host; never synthesize IDs."""
    if tool not in {"read_thread", "send_message_to_thread"} or not isinstance(arguments, dict):
        raise ValueError("Unsupported Codex native bridge tool")
    if not isinstance(executor_meta, dict) or any(
        not isinstance(executor_meta.get(key), str) or not executor_meta[key].strip()
        for key in ("threadId", "turnId", "callId")
    ):
        raise NativeBridgeError("CODEX_NATIVE_EXECUTOR_META_REQUIRED")
    params = {key: executor_meta[key] for key in ("threadId", "turnId", "callId")}
    params.update(namespace="codex_app", callerSource="codex", tool=tool, arguments=arguments)
    if "hostId" in executor_meta:
        params["hostId"] = executor_meta["hostId"]
    result = _request("tools/call", params, timeout)
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise NativeBridgeError("CODEX_NATIVE_TOOL_ACK_UNVERIFIED")
    return result


def call_registered(registration, tool, arguments, *, source_thread, timeout=8):
    """Make a new MCP-transport call for an explicitly registered real Host chat.

    The official client (server.mjs:34616-34636) creates these opaque MCP IDs
    when native model-turn metadata is absent. They are new request correlation
    IDs, never stored/replayed model executor metadata or claimed active turns.
    The owner must bind source_thread to its verified registration and ledger.
    """
    registered = _registration(registration)
    if tool not in {"read_thread", "send_message_to_thread"} or not isinstance(arguments, dict):
        raise ValueError("Unsupported Codex native bridge tool")
    try:
        canonical = str(UUID(source_thread))
    except (TypeError, ValueError, AttributeError):
        raise NativeBridgeError("CODEX_NATIVE_SOURCE_THREAD_REQUIRED") from None
    if canonical != source_thread.lower():
        raise NativeBridgeError("CODEX_NATIVE_SOURCE_THREAD_REQUIRED")
    request_id = str(uuid4())
    params = {"threadId": source_thread, "turnId": "mcp-turn-" + request_id,
              "callId": "mcp-call-" + str(uuid4()), "namespace": "codex_app",
              "callerSource": "codex", "tool": tool, "arguments": arguments}
    result = _request("tools/call", params, timeout, registration=registered, request_id=request_id)
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise NativeBridgeError("CODEX_NATIVE_TOOL_ACK_UNVERIFIED")
    return result
