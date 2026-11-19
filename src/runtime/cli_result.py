"""Per-call CLI result collection without touching process-wide stdout."""

from contextlib import contextmanager
from contextvars import ContextVar


_COLLECTING = ContextVar("xdev_cli_result_collecting", default=False)


def collecting():
    return _COLLECTING.get()


@contextmanager
def capture():
    """Tell cooperative emitters to return their dictionaries in this context."""
    token = _COLLECTING.set(True)
    try:
        yield
    finally:
        _COLLECTING.reset(token)
