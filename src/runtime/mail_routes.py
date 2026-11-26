"""Choose one paired mail carrier, with a durable guard against ambiguous re-delivery."""

from contextlib import closing
import json
import sqlite3

from src.runtime import transport
from src.runtime.device_names import REPORT_CARRIERS, github_mailbox_config

_SECTIONS = {"relay": "session_relay", "sqs": "managed_queue"}


class NotSubmitted(RuntimeError):
    """A local transport has proved it did not call a mail submission operation."""


def candidates(root, peer, requested, *, delivery_report=False, legacy_mail=False):
    from src.runtime import messages
    directory = transport.directory(root)
    if directory.same(peer, directory.node):
        return (requested,)
    fixed = requested if requested != "auto" else directory.mail_transport(peer)
    if fixed != "auto":
        return (fixed,)
    config = transport.config(root)
    selected = directory.mail_routes(peer)
    configured = selected or ("direct", *REPORT_CARRIERS)
    routes = []
    for route in configured:
        if route in REPORT_CARRIERS:
            section = github_mailbox_config(config) if route == "github" else config.get(_SECTIONS[route], {})
            grants = section.get("peers", {}) if isinstance(section, dict) else {}
            if not isinstance(grants, dict):
                raise ValueError(f"Invalid paired {route} settings")
            match = next((value for name, value in grants.items() if directory.same(peer, name)), None)
            if not isinstance(match, dict) or not match.get("key_file") or (
                    route == "sqs" and not match.get("queue_url")):
                if selected:
                    raise ValueError(f"Automatic {route} route has no paired configuration for {peer}")
                continue
            if route == "relay" and match.get("encryption") != "required" and messages.legacy_broker_allowed(root, peer):
                if selected:
                    raise ValueError("Automatic relay selection requires encryption for this peer")
                continue
        if not delivery_report or route in REPORT_CARRIERS:
            routes.append(route)
    if selected is None and routes == ["direct"]:
        # Older inventories may depend on an SSH ProxyJump or mailbox-only NATS.
        if legacy_mail and config.get("broker") and messages.legacy_broker_allowed(root, peer):
            return ("nats", "ssh")
        return ("ssh",)
    if selected is None and "direct" in routes and transport.ssh_uses_proxy(root, peer):
        routes[routes.index("direct")] = "ssh"
    if not routes:
        raise ValueError("No paired route supports the requested mail delivery")
    return tuple(routes)


def _db(root):
    state = root / "state"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(state / "sent-mail.sqlite3", timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS mail_route_claims
                  (id TEXT PRIMARY KEY, carrier TEXT NOT NULL, receipt TEXT)""")
    return db


def _claim(root, identifier, route, *, retry_same_route=False):
    with closing(_db(root)) as db, db:
        cursor = db.execute("INSERT OR IGNORE INTO mail_route_claims VALUES (?, ?, NULL)", (identifier, route))
        if cursor.rowcount:
            return None
        carrier, saved = db.execute("SELECT carrier, receipt FROM mail_route_claims WHERE id=?",
                                    (identifier,)).fetchone()
        if saved and retry_same_route and carrier == route and carrier != "github":
            receipt = json.loads(saved)
            confirmed = receipt.get("ok") or (receipt.get("inbox_status") in {"received", "duplicate"}
                                               and receipt.get("session_status") != "unknown")
            if confirmed and receipt.get("session_status") != "requires_receiver_binding":
                # Existing clients can deliberately repeat a confirmed publication on its original
                # deduplicating carrier, e.g. to promote an item after the recipient becomes ready.
                db.execute("UPDATE mail_route_claims SET receipt=NULL WHERE id=?", (identifier,))
                return None
    if saved:
        return {**json.loads(saved), "route_reused": True}
    return {"ok": False, "id": identifier, "transport": carrier, "status": "submission_unconfirmed",
            "inbox_status": "unknown", "session_accepted": None, "route_reused": True,
            "next_action": "Check the existing message ID on its original route before any further action."}


def _release(root, identifier, route):
    with closing(_db(root)) as db, db:
        db.execute("DELETE FROM mail_route_claims WHERE id=? AND carrier=? AND receipt IS NULL", (identifier, route))


def _finish(root, identifier, route, result, *, reconciled=False):
    # Keep status only. Letter contents, native diagnostics and settings are not copied into the journal.
    fields = ("ok", "id", "host", "transport", "status", "inbox_status", "session_status", "session_accepted",
              "current_turn_accepted", "delivery_receipt", "delivery_report_requested", "error_code", "route_attempts")
    saved = json.dumps({key: result[key] for key in fields if key in result})
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT receipt FROM mail_route_claims WHERE id=? AND carrier=?", (identifier, route)).fetchone()
        if row and (row[0] is None or (reconciled and json.loads(row[0]).get("status") == "submission_unconfirmed")):
            db.execute("UPDATE mail_route_claims SET receipt=? WHERE id=? AND carrier=?", (saved, identifier, route))


def send(root, peer, identifier, routes, submit, *, reconcile=None):
    attempts = []
    for index, route in enumerate(routes):
        previous = _claim(root, identifier, route, retry_same_route=len(routes) == 1)
        if previous is not None:
            if (previous.get("status") == "submission_unconfirmed" and previous["transport"] == "github"
                    and "github" in routes and reconcile is not None):
                # Only a distinct read-only operation may check an uncertain upload. Neither a
                # missing asset nor a lookup failure authorizes a new POST or a different route.
                try:
                    checked = reconcile("github")
                except (OSError, RuntimeError, ValueError) as exc:
                    return {**previous, "lookup_status": "unconfirmed", "lookup_error": type(exc).__name__}
                if checked.get("ok") and checked.get("status") == "queue_acknowledged":
                    found = {**checked, "id": identifier, "transport": "github", "route_reused": True,
                             "route_attempts": [{"transport": "github", "status": "queue_acknowledged"}],
                             "lookup_status": "confirmed"}
                    _finish(root, identifier, "github", found, reconciled=True)
                    return found
                return {**previous, "lookup_status": "unconfirmed"}
            return previous
        try:
            if route == "direct" and index + 1 < len(routes):
                transport.check_direct_connection(root, peer)
            result = submit(route)
        except NotSubmitted as exc:
            _release(root, identifier, route)
            attempts.append({"transport": route, "status": "not_submitted", "reason": str(exc)})
            continue
        except ValueError as exc:
            result = {"ok": False, "id": identifier, "host": peer, "transport": route,
                      "status": "route_rejected", "inbox_status": "unconfirmed", "session_accepted": None,
                      "error_code": type(exc).__name__, "error_message": str(exc)}
        except (OSError, RuntimeError) as exc:
            result = {"ok": False, "id": identifier, "host": peer, "transport": route,
                      "status": "submission_unconfirmed", "inbox_status": "unknown", "session_accepted": None,
                      "error_code": type(exc).__name__, "error_message": str(exc)}
        result.setdefault("transport", route)
        result.setdefault("id", identifier)
        attempts.append({"transport": route, "status": result.get("status", "unconfirmed")})
        result["route_attempts"] = attempts
        _finish(root, identifier, route, result)
        return result
    return {"ok": False, "id": identifier, "host": peer, "status": "no_route_available",
            "inbox_status": "not_attempted", "session_accepted": False, "route_attempts": attempts}
