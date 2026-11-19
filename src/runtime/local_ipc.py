"""Bounded rereads of the same local IPC response, never request resubmission."""

import errno
import math
import time


def diagnostics(error, *, stage=None):
    result = {"error_class": type(error).__name__}
    for source, target in (("winerror", "winerror"), ("errno", "errno"),
                           ("ipc_read_attempts", "read_attempts")):
        value = getattr(error, source, None)
        if type(value) is int:
            result[target] = value
    observed = getattr(error, "ipc_stage", stage)
    if isinstance(observed, str):
        result["stage"] = observed
    return result


def read_response(reader, *, deadline, stage="response_read", max_attempts=16, interval=0.02):
    """Retry only transient-compatible access errors inside an existing deadline.

    Permission failures may also be persistent ACL failures. A retry is not a
    diagnosis and never modifies permissions. Missing files, malformed content,
    failed authentication and wrong targets are left to the caller immediately.
    The operation must read one fixed response; it must not write/signal requests.
    """
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("Response read requires a finite monotonic deadline")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 128:
        raise ValueError("Response read attempts must be from 1 to 128")
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 < interval <= 0.1:
        raise ValueError("Response read interval must be within (0, 0.1] seconds")
    if not isinstance(stage, str) or not stage or len(stage) > 128:
        raise ValueError("Supply a bounded response read stage label")
    attempts = 0
    while True:
        attempts += 1
        try:
            return reader()
        except PermissionError as error:
            error.ipc_stage = stage
            error.ipc_read_attempts = attempts
            compatible = getattr(error, "winerror", None) in {5, 32, 33} or error.errno == errno.EACCES
            remaining = deadline - time.monotonic()
            if not compatible or attempts >= max_attempts or remaining <= 0:
                raise
            time.sleep(min(interval, remaining))
            if time.monotonic() >= deadline:
                raise
