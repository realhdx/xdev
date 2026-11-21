"""Short-lived local interest in the configured GitHub inbox, across processes."""

import math
import os
import re
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

from src.runtime import transport

MAX_WINDOW = 60
MARKER = re.compile(r"wait-([0-9]{1,20})-[a-f0-9]{32}")


def _directory(root):
    directory = root / "state" / "github-demand"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _active(directory):
    """Only live, bounded requests count. Reap abandoned markers after a crash."""
    now = time.time_ns()
    found = False
    for path in directory.iterdir():
        match = MARKER.fullmatch(path.name)
        if not match:
            continue
        remaining = int(match[1]) - now
        if 0 < remaining <= MAX_WINDOW * 1_000_000_000:
            found = True
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return found


@contextmanager
def request(root, seconds):
    """Signal once; remove this waiter's interest on success, timeout or cancellation."""
    mailbox = transport.config(root).get("github_mail")
    if not (
        isinstance(mailbox, dict)
        and mailbox.get("repository")
        and mailbox.get("repository_id")
        and mailbox.get("release_id")
        and mailbox.get("peers")
        and 0 < seconds <= MAX_WINDOW
    ):
        yield None
        return
    marker = None
    try:
        directory = _directory(root)
        _active(directory)
        end = time.time_ns() + math.ceil(seconds * 1_000_000_000)
        marker = directory / f"wait-{end}-{uuid4().hex}"
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        # Local inbox access stays available if the accelerator is unavailable.
        yield "unavailable"
        return
    try:
        yield "signaled"
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def wait(root, delay, minimum):
    """Interrupt an idle interval, while preserving the earliest permitted next poll."""
    if delay <= minimum or sys.platform not in ("win32", "linux"):
        time.sleep(delay)
        return
    from src.runtime.messages import _watch

    start = time.monotonic()
    deadline = start + delay
    earliest = start + minimum
    try:
        directory = _directory(root)
        # A request that arrives during watcher creation survives the initial scan.
        with _watch(directory) as changed:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    return
                if _active(directory):
                    if now >= earliest:
                        return
                    # Coalesce other requests during the minimum interval.
                    time.sleep(earliest - now)
                elif not changed(deadline - now):
                    return
    except (OSError, RuntimeError):
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
