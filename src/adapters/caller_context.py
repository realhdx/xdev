"""Host-declared MCP caller identity, isolated per call.

This is routing metadata from the connected local host, not cryptographic
identity or permission to execute anything. Never infer a chat from the shared
MCP process environment or its transport session ID.
"""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from uuid import UUID


_CALLER = ContextVar("xdev_mcp_caller", default=None)
_CODEX_META = "x-codex-turn-metadata"
_OPENCODE_META = "ai.opencode/sessionID"


def _empty(status="unknown", source="mcp_meta"):
    return {"status": status, "client": None, "session": None, "source": source}


def _chat_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        canonical = str(UUID(value))
    except ValueError:
        return None
    return canonical if canonical == value.lower() else None


def normalize(meta):
    """Recognize only proved per-call keys; malformed/conflicting IDs fail closed.

    Codex core supplies threadId and x-codex-turn-metadata.thread_id. Its
    sessionId is a runtime instance ID and is intentionally ignored.
    OpenCode V2 supplies ai.opencode/sessionID. DSH/V1 need a Host wrapper;
    this parser never guesses their current chat.
    """
    if meta is not None and not isinstance(meta, Mapping):
        dump = getattr(meta, "model_dump", None)
        if not callable(dump):
            return _empty()
        try:
            meta = dump(by_alias=True)
        except (TypeError, ValueError):
            return _empty()
    if not isinstance(meta, Mapping):
        return _empty()
    candidates = []
    if "threadId" in meta:
        identifier = _chat_uuid(meta["threadId"])
        if identifier is None:
            return _empty(source="invalid_mcp_meta")
        candidates.append(("codex", identifier))
    if _CODEX_META in meta:
        codex = meta[_CODEX_META]
        if isinstance(codex, str):
            if len(codex) > 16384:
                return _empty(source="invalid_mcp_meta")
            try:
                codex = json.loads(codex)
            except ValueError:
                return _empty(source="invalid_mcp_meta")
        if not isinstance(codex, Mapping):
            return _empty(source="invalid_mcp_meta")
        if "thread_id" in codex:
            identifier = _chat_uuid(codex["thread_id"])
            if identifier is None:
                return _empty(source="invalid_mcp_meta")
            candidates.append(("codex", identifier))
    if _OPENCODE_META in meta:
        identifier = meta[_OPENCODE_META]
        if not isinstance(identifier, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,100}", identifier):
            return _empty(source="invalid_mcp_meta")
        candidates.append(("opencode", identifier))
    unique = set(candidates)
    if len(unique) > 1:
        return _empty("conflict", "conflicting_mcp_meta")
    if not unique:
        return _empty()
    client, session = unique.pop()
    return {"status": "ready", "client": client, "session": session,
            "source": client + "_mcp_meta"}


@contextmanager
def bind(meta):
    """Bind one request and restore its caller even when execution raises."""
    token = _CALLER.set(normalize(meta))
    try:
        yield current()
    finally:
        _CALLER.reset(token)


def current():
    """Return a copy so downstream code cannot mutate another call's identity."""
    value = _CALLER.get()
    return dict(value) if value is not None else _empty(source="unbound")


@contextmanager
def bind_channel(client, session):
    """Bind the native channel's own validated SessionStart hook to one tool call."""
    if client not in {"claude-channel", "codebuddy-channel"} or not session or _chat_uuid(session) != session:
        raise ValueError("Expected an exact native channel chat")
    token = _CALLER.set({"status": "ready", "source": "official_channel_hook",
                         "client": client, "session": session})
    try:
        yield current()
    finally:
        _CALLER.reset(token)


@contextmanager
def bind_dsh(session):
    """One native DSH ToolRunContext invocation, never a Host-wide session."""
    if not isinstance(session, str) or not session.startswith("session-") or _chat_uuid(session[8:]) != session[8:]:
        raise ValueError("An exact native DSH chat is required")
    token = _CALLER.set({"status": "ready", "source": "dsh_host_tool", "client": "dsh", "session": session})
    try:
        yield current()
    finally:
        _CALLER.reset(token)
