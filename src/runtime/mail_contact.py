"""First-party chat presence and the sender's own body-free correspondence observations."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from src.adapters import sessions
from src.runtime import messages, outbox, transport


INTENTS = ("inform", "invite", "request_reply", "request_action")
STATES = ("available", "busy", "maintenance", "quota_limited", "unknown")
SCHEMA = """
CREATE TABLE IF NOT EXISTS presence(address TEXT PRIMARY KEY, state TEXT, note TEXT, observed TEXT, expires TEXT);
CREATE TABLE IF NOT EXISTS observations(address TEXT PRIMARY KEY, evidence TEXT);
CREATE TABLE IF NOT EXISTS dispatches(id TEXT PRIMARY KEY, author TEXT, recipient TEXT, peer TEXT,
  intent TEXT, issued TEXT, submitted TEXT, recorded TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS letter_evidence(id TEXT, author TEXT, recipient TEXT, received TEXT,
  read_at TEXT, withdrawn TEXT, reply_json TEXT, PRIMARY KEY(id,author,recipient));
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def database(root):
    path = Path(root) / "state"
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    db = sqlite3.connect(path / "mail-contact.sqlite3", timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def identity(root, endpoint):
    if not endpoint:
        return None
    value = messages.normalize_participant(endpoint)
    return {**value, "device": transport.directory(root).identity_key(value["device"])}


def address_key(root, endpoint):
    value = identity(root, endpoint)
    if value:
        value = {k: messages.profile_key(v) if k in {"codex_home", "claude_home"} else v for k,v in value.items()}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def report_presence(root, actor, state, ttl, reason=None):
    if not actor or not transport.directory(root).same(actor["device"], transport.node(root)):
        raise ValueError("Availability requires the reporting local chat.")
    if state not in STATES or not 1 <= ttl <= 86400 or (reason is not None and len(reason) > 256):
        raise ValueError("Use a known state, 1–86400 seconds and a reason of at most 256 characters.")
    stamp = datetime.now(timezone.utc)
    entered = stamp.isoformat(timespec="milliseconds")
    expires = (stamp + timedelta(seconds=ttl)).isoformat(timespec="milliseconds")
    with closing(database(root)) as db, db:
        db.execute("INSERT OR REPLACE INTO presence VALUES(?,?,?,?,?)",
                   (address_key(root, actor), state, reason, entered, expires))
    return {"ok": True, "status": "declared", "state": state, "source": "recipient_declaration",
            "observed_at": entered, "expires_at": expires}


def local_probe(root, target):
    if not transport.directory(root).same(target["device"], transport.node(root)):
        raise ValueError("Inspect a chat on its own host.")
    result = {"source": "local_host", "observed_at": now(), "host_state": "unknown",
              "adapter_state": "unverified", "model_calls": 0}
    result["host_xdev_version"] = json.loads((Path(__file__).parents[1] / "product.json").read_text(
        encoding="utf-8"))["version"]
    try:
        if target.get("codex_home") or target.get("claude_home"):
            # A profile-agnostic host check cannot verify the selected profile.
            result["adapter_state"] = "profile_probe_unavailable"
        else:
            observation = sessions.discover(root, target["client"], target["session"])
            result["adapter_state"] = observation.get("status", "unknown")
            if observation.get("active") is True and type(observation.get("running")) is bool:
                result["host_state"] = "busy" if observation["running"] else "idle"
    except (OSError, RuntimeError, ValueError):
        result["adapter_state"] = "probe_unavailable"
    with closing(database(root)) as db:
        row = db.execute("SELECT * FROM presence WHERE address=?", (address_key(root,target),)).fetchone()
    if row:
        result["declaration"] = {"state": row["state"], "source": "recipient_declaration",
                                 "reason": row["note"], "observed_at": row["observed"], "expires_at": row["expires"]}
    return result


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def cached(root, target, *, actor=None, refresh=None, earliest_sample=None):
    if target is None:
        return {"status": "device_mailbox", "host_state": "unknown", "model_calls": 0}
    key = json.dumps([address_key(root, actor), address_key(root, target)])
    with closing(database(root)) as db, db:
        if isinstance(refresh, dict) and refresh.get("source") == "local_host" and _timestamp(refresh.get("observed_at")):
            db.execute("BEGIN IMMEDIATE")
            fields = ("source", "observed_at", "host_state", "adapter_state", "host_xdev_version")
            latest = {field: refresh[field] for field in fields if isinstance(refresh.get(field), str)}
            bound = _timestamp(earliest_sample)
            if bound:
                latest["sample_window_start"] = bound.isoformat()
            declaration = refresh.get("declaration")
            if isinstance(declaration, dict) and declaration.get("state") in STATES:
                fields = ("state", "source", "reason", "observed_at", "expires_at")
                latest["declaration"] = {k: declaration[k] for k in fields if isinstance(declaration.get(k), str)}
            previous = db.execute("SELECT evidence FROM observations WHERE address=?", (key,)).fetchone()
            old_bound = _timestamp(json.loads(previous[0]).get("sample_window_start")) if previous else None
            if not old_bound or (bound and bound >= old_bound):
                db.execute("INSERT OR REPLACE INTO observations VALUES(?,?)", (key, json.dumps(latest)))
        row = db.execute("SELECT evidence FROM observations WHERE address=?", (key,)).fetchone()
    result = json.loads(row[0]) if row else {}
    reference = datetime.now(timezone.utc)
    source_stamp = _timestamp(result.get("observed_at"))
    window_start = _timestamp(result.get("sample_window_start"))
    stamp = window_start or source_stamp
    age = (reference - stamp).total_seconds() if stamp else None
    reported = result.get("declaration")
    if reported:
        expires = _timestamp(reported.get("expires_at"))
        if window_start and source_stamp and expires:
            # Both timestamps came from the peer's clock. Compare the remaining validity
            # to a local upper bound on transit plus cache age, even across clock skew.
            reported["expired"] = age < 0 or age >= (expires - source_stamp).total_seconds()
        else:
            reported["expired"] = expires is None or expires <= reference
    stale = age is None or not 0 <= age <= 60
    return {**result, "status": "observed" if row and refresh else "cached" if row else "unknown",
            "host_state": result["host_state"] if not stale and result.get("host_state") in {"idle", "busy"} else "unknown",
            "last_host_state": result.get("host_state", "unknown"), "host_observation_stale": stale,
            "observation_age_seconds": None if age is None else round(age, 1),
            "age_basis": "local_request_upper_bound" if window_start else "source_clock",
            "model_calls": 0}


def before_send(root, envelope, intent):
    with closing(database(root)) as db, db:
        row = (envelope["id"], address_key(root,envelope.get("sender_endpoint")),
               address_key(root,envelope.get("recipient_identity")),
               transport.directory(root).identity_key(envelope["to"]), intent, now(), envelope.get("reply_to"))
        db.execute("""INSERT OR IGNORE INTO dispatches(id,author,recipient,peer,intent,issued,parent)
                      VALUES(?,?,?,?,?,?,?)""", row)
        saved = db.execute("SELECT author,recipient,peer,intent,parent FROM dispatches WHERE id=?", (row[0],)).fetchone()
        if tuple(saved) != (*row[1:5], row[6]):
            raise ValueError("The message ID already belongs to another exchange or intent.")


def after_send(root, identifier, result):
    fields = ("transport", "status", "inbox_status", "session_status", "session_accepted",
              "current_turn_accepted", "return_channel", "error_code")
    evidence = {k: result[k] for k in fields if k in result}
    if result.get("delivery_receipt", {}).get("scope"):
        evidence["delivery_receipt"] = {"scope": result["delivery_receipt"]["scope"]}
    with closing(database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT submitted FROM dispatches WHERE id=?", (identifier,)).fetchone()
        if previous and previous["submitted"]:
            saved = json.loads(previous["submitted"])
            # Failed retries cannot erase an earlier acknowledgement for this exact ID.
            if (saved.get("inbox_status") in {"received", "duplicate"} and
                    evidence.get("inbox_status") not in {"received", "duplicate"}):
                return
            if saved.get("session_accepted") is True and evidence.get("session_accepted") is not True:
                return
            if saved.get("status") in {"broker_acknowledged", "queue_acknowledged"} and not result.get("ok"):
                return
        db.execute("UPDATE dispatches SET submitted=?,recorded=? WHERE id=?", (json.dumps(evidence), now(), identifier))


def dispatch(root, identifier, actor, *, as_device=False):
    with closing(database(root)) as db:
        row = db.execute("SELECT * FROM dispatches WHERE id=?", (identifier,)).fetchone()
    if row and outbox._can_view(root, actor, as_device, row["author"]):
        return {"recipient": json.loads(row["recipient"]), "peer": row["peer"], "intent": row["intent"],
                "issued_at": row["issued"], "submission": json.loads(row["submitted"] or "{}"),
                "submission_observed_at": row["recorded"], "parent": row["parent"]}
    # Older sends recorded exact actors and targets but not their local times or intent.
    path = root / "state" / "sent-mail.sqlite3"
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"sent_authors", "sent"} <= tables:
                row = db.execute("SELECT a.author,s.recipient FROM sent_authors a JOIN sent s ON a.id=s.id "
                                 "WHERE a.id=?", (identifier,)).fetchone()
                if row and outbox._can_view(root, actor, as_device, row[0]):
                    target = json.loads(row[1])
                    return {"recipient": target, "peer": target.get("device") if target else None,
                            "intent": None, "issued_at": None, "submission": {}, "submission_observed_at": None}
    return None


def related_reply(root, identifier, actor, target):
    path = root / "state" / "messages.sqlite3"
    if not path.is_file() or not all((actor, target)):
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        if not {"sender_endpoint", "recipient_identity", "received_at"} <= cols:
            return None
        present = " AND withdrawn_at IS NULL" if "withdrawn_at" in cols else ""
        for mid, recv, sender, destination in db.execute(
            "SELECT id,received_at,sender_endpoint,recipient_identity FROM messages WHERE reply_to=? AND kind='reply'"
            + present + " ORDER BY seq DESC", (identifier,)
        ):
            if (messages.matches_participant(json.loads(sender or "null"), target, root=root, exact=True)
                    and messages.matches_participant(json.loads(destination or "null"), actor, root=root, exact=True)):
                return {"id": mid, "reply_to": identifier, "received_at": recv, "source": "local_addressed_inbox"}
    return None


def confirmed_delivery(root, identifier, actor, target, observation, linked):
    """Retain immutable, body-free facts about this exact exchange across failed status probes."""
    if not actor or not target:
        return observation, linked
    key = (identifier, address_key(root, actor), address_key(root, target))
    observed = observation or {}
    verified = (observed.get("ok") is True and observed.get("id") == identifier
                and observed.get("scope") == "recipient_mailbox")
    received = observed.get("received_at") if verified else None
    read = observed.get("read_at") if verified and observed.get("read_scope") == "session" else None
    withdrawn = observed.get("withdrawn_at") if verified else None
    reply = json.dumps(linked) if linked and linked.get("reply_to") == identifier else None
    with closing(database(root)) as db, db:
        if any((received, read, withdrawn, reply)):
            db.execute("""INSERT INTO letter_evidence VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id,author,recipient) DO UPDATE SET
                received=coalesce(received,excluded.received),read_at=coalesce(read_at,excluded.read_at),
                withdrawn=coalesce(withdrawn,excluded.withdrawn),reply_json=coalesce(reply_json,excluded.reply_json)""",
                       (*key, received, read, withdrawn, reply))
        row = db.execute("SELECT * FROM letter_evidence WHERE id=? AND author=? AND recipient=?", key).fetchone()
    if not row:
        return observation, linked
    evidence = dict(observed) if verified else {}
    evidence.update(id=identifier, ok=True, scope="recipient_mailbox", received_at=row["received"],
                    withdrawn_at=row["withdrawn"])
    if row["read_at"]:
        evidence.update(read_at=row["read_at"], read_scope="session")
    return evidence, linked or json.loads(row["reply_json"] or "null")
