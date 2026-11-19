"""Short commands whose output is returned on one request."""

import os
import subprocess
import tempfile

from src.runtime.transport import _cleanup_process


OUTPUT_LIMIT = 512 * 1024


def command_args(payload):
    argv = payload.get("argv")
    cwd = payload.get("cwd")
    distro = payload.get("wsl")
    if not isinstance(argv, list) or not argv or any(
        not isinstance(arg, str) or "\0" in arg for arg in argv
    ):
        raise ValueError("Specify an executable and argument list after --")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd must be a string")
    if distro is not None:
        if not isinstance(distro, str) or not distro or distro.startswith("-"):
            raise ValueError("Specify a valid WSL distribution name")
        args = ["wsl.exe", "--distribution", distro]
        if cwd:
            args += ["--cd", cwd]
        return args + ["--exec", *argv], None
    return argv, cwd


def _output(file):
    file.seek(0)
    raw = file.read(OUTPUT_LIMIT + 1)
    cropped = len(raw) > OUTPUT_LIMIT
    raw = raw[:OUTPUT_LIMIT]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace"), cropped
    if raw.startswith(b"w\0s\0l\0:\0"):
        return raw.decode("utf-16-le", "replace"), cropped
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("mbcs", "replace")
    return text, cropped


def run(payload):
    argv, cwd = command_args(payload)
    timeout = float(payload.get("timeout", 60))
    if not 0 < timeout <= 300:
        raise ValueError("Timeout must be within (0, 300] seconds. Use a job for longer commands.")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env={**os.environ, "WSL_UTF8": "1"} if payload.get("wsl") else None,
            )
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _cleanup_process(proc)
            return {"ok": False, "error_code": "COMMAND_TIMEOUT", "outcome": "unknown",
                    "error_message": "Command timed out. Verify any side effects before retrying."}
        out, trunc_out = _output(stdout)
        err, trunc_err = _output(stderr)
    return {"ok": exit_code == 0, "exit_code": exit_code,
            "stdout": out, "stderr": err, "output_truncated": trunc_out or trunc_err}
