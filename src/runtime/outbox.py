"""Local outbound intent used to authenticate which conversation a reply can wake."""

from contextlib import closing
import hashlib
import json
import sqlite3

from src.runtime import messages, transport


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def remember(root, envelope, *, delivery_report=False, automatic_report=False):
    """Register a locally initiated exchange before publication can race its reply."""
    directory = transport.directory(root)
    if not directory.same(envelope.get("from"), directory.node):
        raise ValueError("An outgoing letter must originate from this device")
    canonical = {**envelope, **messages.address_fields(envelope, directory.identity_key)}
    canonical.pop("sender_identity_source", None)
    expected = messages.normalize_participant(canonical.get("recipient_identity"))
    callback = canonical.get("reply_endpoint") or {}
    callback = (messages.normalize_participant(callback) if callback.get("client")
                and directory.same(callback.get("device"), directory.node) else None)
    checksum = hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()
    state = root / "state"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(state / "sent-mail.sqlite3", timeout=10)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS sent (
                      id TEXT PRIMARY KEY, digest TEXT NOT NULL, recipient TEXT, callback TEXT)""")
        fresh = db.execute("INSERT OR IGNORE INTO sent VALUES (?, ?, ?, ?)",
                           (envelope["id"], checksum, _json(expected), _json(callback))).rowcount
        previous = db.execute("SELECT digest FROM sent WHERE id=?", (envelope["id"],)).fetchone()[0]
        if previous != checksum:
            # Preview.3 included the optional carrier report in the letter identity.
            old_wire = _json({**canonical, "delivery_report_requested": True}).encode("utf-8")
            if previous != hashlib.sha256(old_wire).hexdigest():
                raise ValueError("This outgoing message ID already identifies a different letter")
            db.execute("UPDATE sent SET digest=? WHERE id=?", (checksum, envelope["id"]))
        db.execute("""CREATE TABLE IF NOT EXISTS report_requests (
                      id TEXT PRIMARY KEY, peer TEXT NOT NULL, author TEXT NOT NULL, report TEXT)""")
        db.execute("CREATE TABLE IF NOT EXISTS sent_authors (id TEXT PRIMARY KEY, author TEXT NOT NULL)")
        author = _json(messages.normalize_participant(canonical.get("sender_endpoint")))
        db.execute("INSERT OR IGNORE INTO sent_authors VALUES (?,?)", (envelope["id"], author))
        endpoint = canonical.get("reply_endpoint") or {}
        if (endpoint.get("channel") == "tool_result" and endpoint.get("request_id") == envelope["id"]
                and directory.same(endpoint.get("device"), directory.node)):
            db.execute("CREATE TABLE IF NOT EXISTS waiting_calls (id TEXT PRIMARY KEY)")
            db.execute("INSERT OR IGNORE INTO waiting_calls VALUES (?)", (envelope["id"],))
        if automatic_report:
            # An existing ID must keep the reporting mode of its original encrypted upload.
            delivery_report = bool(fresh or db.execute("SELECT 1 FROM report_requests WHERE id=?",
                                                       (envelope["id"],)).fetchone() or delivery_report)
        if delivery_report:
            db.execute("INSERT OR IGNORE INTO report_requests(id, peer, author) VALUES (?, ?, ?)",
                       (envelope["id"], canonical["to"], author))
        return delivery_report


def status(root, identifier, actor, *, as_device=False):
    unavailable = {"ok": False, "id": identifier, "status": "no_matching_report_request"}
    path = root / "state" / "sent-mail.sqlite3"
    if not path.is_file() or (not actor and not as_device):
        return unavailable
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_requests'").fetchone():
            return unavailable
        row = db.execute("SELECT author, report FROM report_requests WHERE id=?", (identifier,)).fetchone()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        local = None
        if {"sent_authors", "mail_route_claims"} <= tables:
            local = db.execute("SELECT a.author, c.carrier, c.receipt FROM sent_authors a "
                               "JOIN mail_route_claims c ON c.id=a.id WHERE a.id=?", (identifier,)).fetchone()
    route = {}
    if local and _can_view(root, actor, as_device, local[0]):
        receipt = json.loads(local[2]) if local[2] else {}
        route = {"transport": local[1], "submission_status": receipt.get("status", "submission_unconfirmed"),
                 "inbox_status": receipt.get("inbox_status", "unknown"), "session_accepted": receipt.get("session_accepted"),
                 "route_attempts": receipt.get("route_attempts", []), "model_status": "unverified"}
    if row is None:
        return {"ok": True, "id": identifier, "status": "outbound_status", **route} if route else unavailable
    if not _can_view(root, actor, as_device, row[0]):
        return unavailable
    result = {"ok": True, "id": identifier, "status": "awaiting_report", "session_accepted": None, **route}
    if row[1] is not None:
        report = json.loads(row[1])
        result.update(status="report_received", report=report, inbox_status=report["inbox_status"], model_status="unverified")
        if report["placement"] in {"current_turn", "next_turn", "original_session"}:
            result["session_accepted"] = True
        elif (route.get("transport") == "github" and report["inbox_status"] == "received"
              and report["session_status"] == "not_requested" and report["placement"] == "unknown"):
            result.update(delivery_scope="mailbox", model_woken=False,
                          available_to_addressed_chat=True, session_accepted=False)
    return result


def _can_view(root, actor, as_device, stored):
    author = json.loads(stored)
    if author is None or actor is None:
        return author is None and actor is None and as_device
    return messages.matches_participant(actor, author, exact=True, root=root)


def record_report(root, envelope):
    """Called only after the incoming peer's relay signature has been verified."""
    from src.runtime import session_relay
    if (envelope.get("kind") != "receipt" or envelope.get("sender_endpoint") is not None
            or not isinstance(envelope.get("reply_to"), str)):
        return False
    directory = transport.directory(root)
    identifier = envelope["reply_to"]
    if (envelope.get("id") != session_relay.report_id(directory.identity_key(directory.node), identifier)
            or not directory.same(envelope.get("to"), directory.node)):
        return False
    try:
        report = json.loads(envelope["text"])
    except (TypeError, ValueError, KeyError):
        return False
    if (not isinstance(report, dict)
            or set(report) != {"schema", "message_id", "inbox_status", "session_status", "placement"}
            or report["schema"] != session_relay.REPORT_SCHEMA or report["message_id"] != identifier
            or report["inbox_status"] != "received"
            or not session_relay.valid_progress(report["session_status"], report["placement"])):
        return False
    path = root / "state" / "sent-mail.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)) as db, db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_requests'").fetchone():
            return False
        row = db.execute("SELECT peer, author FROM report_requests WHERE id=?", (identifier,)).fetchone()
        if row is None or not directory.same(envelope.get("from"), row[0]):
            return False
        recipient = messages.normalize_participant(envelope.get("recipient_identity"))
        author = json.loads(row[1])
        if (recipient is not None or author is not None) and not messages.matches_participant(
                recipient, author, root=root, exact=True):
            return False
        db.execute("UPDATE report_requests SET report=? WHERE id=? AND report IS NULL", (_json(report), identifier))
    return True


def permits_reply(root, envelope):
    if (envelope.get("kind") != "reply" or not envelope.get("reply_to")
            or not isinstance(envelope.get("text"), str) or not envelope["text"].strip()):
        return False
    path = root / "state" / "sent-mail.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        row = db.execute("SELECT recipient, callback FROM sent WHERE id=?", (envelope["reply_to"],)).fetchone()
    if row is None:
        return False
    sender = messages.normalize_participant(envelope.get("sender_endpoint"))
    destination = messages.normalize_participant(envelope.get("recipient_endpoint"))
    return (messages.matches_participant(sender, json.loads(row[0]), root=root, exact=True)
            and messages.matches_participant(destination, json.loads(row[1]), root=root, exact=True))


def permits_tool_reply(root, envelope):
    """Allow an authenticated peer to return a result to the exact waiting caller, without a wake."""
    if (envelope.get("kind") != "reply" or not envelope.get("reply_to") or envelope.get("recipient_endpoint")
            or not isinstance(envelope.get("text"), str) or not envelope["text"].strip()):
        return False
    path = root / "state" / "sent-mail.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='waiting_calls'").fetchone():
            return False
        row = db.execute("SELECT s.recipient, a.author FROM waiting_calls w "
                         "JOIN sent s ON s.id=w.id JOIN sent_authors a ON a.id=w.id WHERE w.id=?",
                         (envelope["reply_to"],)).fetchone()
    if not row:
        return False
    sender = messages.normalize_participant(envelope.get("sender_endpoint"))
    recipient = messages.normalize_participant(envelope.get("recipient_identity"))
    return (sender is not None and recipient is not None
            and messages.matches_participant(sender, json.loads(row[0]), root=root, exact=True)
            and messages.matches_participant(recipient, json.loads(row[1]), root=root, exact=True))
