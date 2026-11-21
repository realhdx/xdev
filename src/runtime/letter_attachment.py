"""Portable, immutable snapshots of earlier xdev letters, without callback authority."""

from hashlib import sha256
import json
import re

from src.runtime.subjects import normalize_subject


SCHEMA = "xdev.letter-attachment.v1"
INTEGRITY = "sha256_copy_not_independently_signed"
MAX_BYTES = 256 * 1024
MAX_DEPTH = 8
MAX_TEXT = 65536


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _string(value, name, limit):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"Invalid attachment {name}")
    return value


def _participant(endpoint, *, stored):
    from src.runtime import messages
    if endpoint is None:
        return None
    if not isinstance(endpoint, dict):
        raise ValueError("Invalid attachment chat address")
    if not stored and set(endpoint) - {"device", "client", "session", "codex_home", "claude_home"}:
        raise ValueError("Unexpected attachment chat address fields")
    # An explicit host profile is part of the address for contacting this
    # original chat. Reader profiles, callbacks and mailbox paths are excluded.
    return messages.normalize_participant(endpoint)


def _author_route(author, candidate):
    from src.runtime import messages
    if not author or not isinstance(candidate, dict) or candidate.get("channel") == "tool_result":
        return None
    try:
        route = messages.normalize_reply_endpoint(candidate)
        addressee = messages.normalize_participant(route)
    except (TypeError, ValueError):
        return None
    if (messages.matches_participant(author, addressee, exact=True)
            and (route["client"] != author["client"] or "delivery" in route)):
        return route
    return None


def _original_fields(message, *, stored):
    original = {k: _string(message.get(k), k, 128) for k in ("id", "from", "to")}
    if message.get("subject") is not None:
        original["subject"] = normalize_subject(message["subject"])
    for field, device in (("sender_endpoint", "from"), ("recipient_identity", "to")):
        endpoint = _participant(message.get(field), stored=stored)
        if endpoint and endpoint["device"] != original[device]:
            raise ValueError(f"Attachment {field} conflicts with its device")
        original[field] = endpoint
    provenance = message.get("sender_identity_source", "unknown")
    allowed = ("cli_host", "mcp_metadata", "explicit") if original["sender_endpoint"] else ("unknown",)
    if provenance not in allowed:
        raise ValueError("Invalid recorded sender identity source")
    original["sender_identity_source"] = provenance
    candidate = message.get("reply_endpoint" if stored else "author_route")
    # Bindings added later are mailbox annotations. Replaying a snapshot after
    # a later bind must not acquire a different digest for the same original.
    if not stored or message.get("reply_endpoint_source") != "explicit_local_annotation":
        route = _author_route(original["sender_endpoint"], candidate)
        if route:
            original["author_route"] = route
    kind = message.get("kind", "message")
    reply_to = message.get("reply_to")
    if kind not in ("message", "reply", "receipt") or (kind in ("reply", "receipt") and reply_to is None):
        raise ValueError("Invalid attachment message kind or reply relationship")
    original.update(kind=kind, reply_to=_string(reply_to, "reply_to", 128) if reply_to is not None else None)
    return original


def make_attachment(stored_message, *, mode="forward", quote=None, include_full=True):
    """Capture the stored letter exactly. A digest authenticates neither the author nor their claims."""
    if not isinstance(stored_message, dict) or any(stored_message.get(k) for k in
            ("sender_endpoint_error", "recipient_identity_error", "letter_attachment_error")):
        raise ValueError("Cannot snapshot an invalid original message")
    if stored_message.get("withdrawn_at"):
        raise ValueError("The original was withdrawn and cannot be forwarded or quoted.")
    if stored_message.get("content_archived"):
        raise ValueError("Restore the archived original before quoting or forwarding its content.")
    if stored_message.get("letter_attachment_summary") is not None and stored_message.get("letter_attachment") is None:
        raise ValueError("Read the original message by ID before forwarding; an attachment summary omits its source letter")
    if mode not in ("forward", "quote") or type(include_full) is not bool:
        raise ValueError("Choose forward or quote, with an explicit full/excerpt selection")
    if mode == "forward" and (quote is not None or not include_full):
        raise ValueError("A forward includes the complete letter")
    original = _original_fields(stored_message, stored=True)
    text = _string(stored_message.get("text"), "original text", MAX_TEXT)
    raw = text.encode("utf-8")
    original.update(text_sha256=sha256(raw).hexdigest(), text_bytes=len(raw), text_chars=len(text))
    if include_full:
        original["text"] = text
    prior = stored_message.get("letter_attachment")
    if prior is not None:
        prior = validate_attachment(prior)
        if include_full:
            original["letter_attachment"] = prior
        else:
            original["attached_sha256"] = prior["sha256"]
    result = {"schema": SCHEMA, "integrity": INTEGRITY, "mode": mode,
              "scope": "full" if include_full else "partial", "original": original}
    if mode == "quote":
        excerpt = _string(quote, "quote", MAX_TEXT)
        offset = text.find(excerpt)
        if offset < 0:
            raise ValueError("The supplied quote is not a verbatim excerpt of the original letter")
        result.update(quote=excerpt, quote_start=offset)
    result["sha256"] = _digest(result)
    return validate_attachment(result)


def validate_attachment(attachment):
    """Verify structure, limits and digests without a source mailbox. Return a detached copy."""
    return _validate(attachment, 1)


def _validate(value, depth):
    if depth > MAX_DEPTH:
        raise ValueError(f"Attachment chain exceeds {MAX_DEPTH} levels")
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("integrity") != INTEGRITY:
        raise ValueError("Invalid letter attachment schema or integrity declaration")
    mode, scope = value.get("mode"), value.get("scope")
    quoting = mode == "quote"
    if mode not in ("quote", "forward") or scope not in ("partial", "full") or (not quoting and scope != "full"):
        raise ValueError("Invalid attachment mode or scope")
    required = {"schema", "integrity", "mode", "scope", "original", "sha256"}
    if quoting:
        required.update(("quote", "quote_start"))
    if set(value) != required or not _is_digest(value["sha256"]):
        raise ValueError("Invalid letter attachment fields or digest")
    orig = value["original"]
    if not isinstance(orig, dict):
        raise ValueError("Original letter must be an object")
    normalized = _original_fields(orig, stored=False)
    size, chars, digest = orig.get("text_bytes"), orig.get("text_chars"), orig.get("text_sha256")
    if (type(chars) is not int or not 1 <= chars <= MAX_TEXT or type(size) is not int
            or not chars <= size <= 4 * chars or not _is_digest(digest)):
        raise ValueError("Invalid original letter length or text digest")
    normalized.update(text_bytes=size, text_chars=chars, text_sha256=digest)
    if scope == "full":
        text = _string(orig.get("text"), "text", MAX_TEXT)
        raw = text.encode("utf-8")
        if len(text) != chars or len(raw) != size or sha256(raw).hexdigest() != digest:
            raise ValueError("Original letter text does not match its fingerprint")
        normalized["text"] = text
        if "letter_attachment" in orig:
            normalized["letter_attachment"] = _validate(orig["letter_attachment"], depth + 1)
    elif "attached_sha256" in orig:
        if not _is_digest(orig["attached_sha256"]):
            raise ValueError("Invalid omitted attachment fingerprint")
        normalized["attached_sha256"] = orig["attached_sha256"]
    if orig != normalized:
        raise ValueError("Unexpected, missing or noncanonical original letter fields")
    result = {"schema": SCHEMA, "integrity": INTEGRITY, "mode": mode, "scope": scope, "original": normalized}
    if quoting:
        excerpt = _string(value["quote"], "quote", MAX_TEXT)
        start = value["quote_start"]
        if (type(start) is not int or not 0 <= start <= chars - len(excerpt) or len(excerpt.encode()) > size
                or (scope == "full" and normalized["text"][start:start + len(excerpt)] != excerpt)):
            raise ValueError("Invalid verbatim excerpt or offset")
        result.update(quote=excerpt, quote_start=start)
    if _digest(result) != value["sha256"]:
        raise ValueError("Letter attachment digest mismatch")
    result["sha256"] = value["sha256"]
    if len(_canonical(result)) > MAX_BYTES:
        raise ValueError(f"Letter attachment exceeds {MAX_BYTES} UTF-8 bytes; the full letter was not truncated")
    return result
