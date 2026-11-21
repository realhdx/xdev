"""Silent, correlated mailbox operations over the existing paired GitHub carrier."""

from contextlib import closing
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import time

from src.runtime import messages, session_relay, transport


MAGIC = b"xdev-mc1\0"
DOMAIN = b"\0xdev.mail-control.v1"
MAX_BYTES = 24 * 1024
REQUESTS = {"preflight", "status", "recall", "replace", "correct"}
FRAME_ID = re.compile(r"mc-[ar]-[0-9a-f]{64}\Z")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def db_open(root):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "mail-control.sqlite3", timeout=10)
    db.execute("BEGIN IMMEDIATE")
    db.execute("""CREATE TABLE IF NOT EXISTS requests
               (request TEXT PRIMARY KEY, scope TEXT NOT NULL, peer TEXT NOT NULL, letter_id TEXT, created INTEGER NOT NULL,
                result TEXT, observed INTEGER, submission TEXT NOT NULL DEFAULT 'unconfirmed', expires INTEGER)""")
    if "expires" not in {r[1] for r in db.execute("PRAGMA table_info(requests)")}:
        db.execute("ALTER TABLE requests ADD COLUMN expires INTEGER")
    db.execute("CREATE INDEX IF NOT EXISTS control_scope ON requests(scope,created DESC)")
    db.execute("""CREATE TABLE IF NOT EXISTS handled
               (request TEXT PRIMARY KEY, result TEXT NOT NULL, peer TEXT NOT NULL, created INTEGER NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0, expires INTEGER)""")
    if "expires" not in {r[1] for r in db.execute("PRAGMA table_info(handled)")}:
        db.execute("ALTER TABLE handled ADD COLUMN expires INTEGER")
    db.execute("CREATE TABLE IF NOT EXISTS response_backoff (singleton INTEGER PRIMARY KEY, until_time INTEGER NOT NULL)")
    # The GitHub wire lifetime is bounded by one day. Keep receipts longer than any valid replay.
    cutoff = int(time.time()) - 30 * 86400
    with db:
        db.execute("DELETE FROM handled WHERE created<?", (cutoff,))
        db.execute("DELETE FROM requests WHERE created<?", (cutoff,))
    return db


def payload_for(directory, source, destination, data):
    if not isinstance(data, dict) or data.keys() - {
            "op", "id", "actor", "target", "replacement_id", "expected_version", "operation_id"}:
        raise ValueError("Invalid mailbox control fields")
    if data.get("op") not in REQUESTS:
        raise ValueError("Unknown mailbox control")
    if data["op"] == "preflight":
        if set(data) != {"op", "actor", "target"}:
            raise ValueError("An availability check only names the two chats")
    else:
        messages._string(data, "id")
    p = dict(data)
    for field, device in (("actor", source), ("target", destination)):
        endpoint = messages.normalize_participant(p.get(field))
        if not endpoint or not endpoint.get("session") or directory.identity_key(endpoint["device"]) != device:
            raise ValueError("Mailbox control must identify both original chats")
        p[field] = {**endpoint, "device": device}
    if (p["op"] in {"replace", "correct"}) != ("replacement_id" in p):
        raise ValueError("A revision requires its replacement letter ID")
    for key in ("operation_id", "replacement_id"):
        if key in p:
            messages._string(p, key)
    if "expected_version" in p and (type(p["expected_version"]) is not int or not 0 <= p["expected_version"] < 2**63):
        raise ValueError("Invalid lifecycle version")
    return p


def owned(root, data):
    path = root / "state" / "sent-mail.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sent", "sent_authors"} <= tables:
            return False
        row = db.execute("SELECT a.author,s.recipient FROM sent s JOIN sent_authors a USING(id) WHERE s.id=?",
                         (data["id"],)).fetchone()
    return bool(row and all(messages.matches_participant(json.loads(saved), data[key], root=root, exact=True)
                            for saved, key in zip(row, ("actor", "target"))))


def outbound(root, peer, payload):
    directory = transport.directory(root)
    dest = directory.identity_key(peer)
    data = payload_for(directory, directory.identity_key(directory.node), dest, payload)
    if data["op"] != "preflight" and not owned(root, data):
        raise ValueError("No original letter for this sender and recipient")
    return data, dest


def original_expiry(root, peer, rid):
    """Older request ledgers keep their original expiry in the carrier upload journal."""
    from src.runtime import github_mail
    try:
        return github_mail.retention_for(root, peer, rid).get("expires_at_unix")
    except (OSError, ValueError, sqlite3.Error, KeyError):
        return None


def latest(root, peer, payload):
    data, dest = outbound(root, peer, payload)
    with closing(db_open(root)) as db:
        scope = digest([dest, data])
        row = db.execute("SELECT request,created,result,observed,submission,expires FROM requests WHERE scope=? "
                         "ORDER BY created DESC,rowid DESC LIMIT 1", (scope,)).fetchone()
        known = db.execute("SELECT result,observed,created FROM requests WHERE scope=? AND result IS NOT NULL "
                           "ORDER BY created DESC,rowid DESC LIMIT 1", (scope,)).fetchone()
    if not row:
        return {"ok": False, "status": "no_control_observation", "transport": "github"}
    result = {"ok": True, "transport": "github", "request_id": row[0], "requested_at": row[1],
              "status": "peer_reported" if row[2] is not None else row[4]}
    expires = row[5] if row[5] is not None else original_expiry(root, dest, row[0])
    if row[5] is None and expires is not None:
        with closing(db_open(root)) as db, db:
            db.execute("UPDATE requests SET expires=? WHERE request=? AND expires IS NULL", (expires,row[0]))
    elapsed = None if expires is None else time.time() >= expires
    result.update(expires_at_unix=expires, delivery_window_elapsed=elapsed)
    if elapsed and row[2] is None and row[4] != "not_submitted":
        result["status"] = "outcome_unknown"
    if known:
        result.update(evidence=json.loads(known[0]), response_observed_at=known[1], evidence_requested_at=known[2],
                      earlier_observation=row[2] is None)
    return result


def submit(root, peer, payload):
    from src.runtime import github_mail

    data, dest = outbound(root, peer, payload)
    src = data["actor"]["device"]
    now = int(time.time())
    scope = digest([dest, data])
    with closing(db_open(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT request,created,result,expires FROM requests WHERE scope=? ORDER BY created DESC,rowid DESC LIMIT 1",
                         (scope,)).fetchone()
        window = 120 if data["op"] == "preflight" else 60
        if old and (data["op"] not in {"preflight", "status"} or now - old[1] < window or "operation_id" in data):
            rid = old[0]
            epoch = old[1]
            finished = old[2] is not None
            expires = old[3] if old[3] is not None else original_expiry(root, dest, rid)
        else:
            epoch = now
            rid = "mc-r-" + digest([src, dest, data, epoch])
            expires = epoch + github_mail.Settings(root).ttl
            db.execute("INSERT INTO requests(request,scope,peer,letter_id,created,expires) VALUES(?,?,?,?,?,?)",
                       (rid, scope, dest, data.get("id"), epoch, expires))
            finished = False
    if finished or (expires is not None and now >= expires):
        return latest(root, dest, data)
    frame = {"from": src, "to": dest, "id": rid, "data": data, "epoch": epoch}
    # The upload journal reconciles this exact ID after an uncertain response, without a second POST.
    try:
        github_mail._send(github_mail.Api(root), github_mail.Settings(root), {"_control": frame, "to": dest, "id": rid}, None)
    except github_mail.NotSubmitted:
        with closing(db_open(root)) as db, db:
            db.execute("UPDATE requests SET submission='not_submitted' WHERE request=?", (rid,))
        raise
    finally:
        # The upload journal is authoritative if registration crossed a clock boundary,
        # including when the response was lost or a previously pending call was resumed.
        actual = original_expiry(root, dest, rid)
        if actual is not None and actual != expires:
            with closing(db_open(root)) as db, db:
                db.execute("UPDATE requests SET expires=? WHERE request=?", (actual,rid))
    with closing(db_open(root)) as db, db:
        db.execute("UPDATE requests SET submission='requested' WHERE request=?", (rid,))
    return latest(root, dest, data)


def pack(settings, box, frame, stamp):
    target = settings.devices.identity_key(frame["to"])
    raw = encoded(frame).encode("utf-8")
    if len(raw) > MAX_BYTES or frame["from"] != settings.local:
        raise ValueError("Invalid outgoing control frame")
    return MAGIC + session_relay.seal_mail(settings.key(target), settings.local, target, raw,
                                           context=box.context + stamp + DOMAIN)


def unpack(settings, source, box, stamp, wire):
    if len(wire) > MAX_BYTES + 256:
        raise ValueError("Oversize mailbox control")
    frame = json.loads(session_relay.open_sealed_mail(settings.key(source), source, settings.local,
                       wire[len(MAGIC):], context=box.context + stamp + DOMAIN))
    if (not isinstance(frame, dict) or frame.get("from") != source or frame.get("to") != settings.local
            or not isinstance(frame.get("id"), str) or not FRAME_ID.fullmatch(frame["id"])):
        raise ValueError("Unmatched mailbox control frame")
    return frame


def fields_are_scalars(obj, *, strings=(), times=(), integers=(), flags=(), enums=None):
    for key in strings:
        value = obj.get(key)
        if value is not None and (not isinstance(value,str) or len(value) > 256 or "\0" in value):
            raise ValueError("Invalid peer text field")
    for key in times:
        value = obj.get(key)
        if value is not None and (not isinstance(value,str) or len(value)>64 or datetime.fromisoformat(value).tzinfo is None):
            raise ValueError("Invalid peer timestamp")
    for key in integers:
        if key in obj and (type(obj[key]) is not int or not 0 <= obj[key] < 2**63):
            raise ValueError("Invalid peer counter")
    for key in flags:
        if key in obj and type(obj[key]) is not bool:
            raise ValueError("Invalid peer flag")
    for key, allowed in (enums or {}).items():
        if obj.get(key) is not None and (not isinstance(obj[key],str) or obj[key] not in allowed):
            raise ValueError("Unknown peer observation value")


def checked_result(result, letter_id):
    """The peer can report correspondence facts and a stated reason, never an instruction or letter body."""
    common = {"ok", "status"}
    if (not isinstance(result,dict) or type(result.get("ok")) is not bool
            or len(encoded(result).encode("utf-8")) > MAX_BYTES):
        raise ValueError("Invalid peer observation")
    if "status" in result and (not isinstance(result["status"],str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",result["status"])):
        raise ValueError("Invalid peer status")
    if letter_id is None:
        if result.keys() - (common | {"observation"}):
            raise ValueError("Invalid availability response")
        observation = result.get("observation", {})
        if not isinstance(observation, dict) or observation.keys() - {
                "host_state", "adapter_state", "source", "observed_at", "model_calls", "declaration", "host_xdev_version"}:
            raise ValueError("Invalid availability observation")
        if result["ok"] and not observation:
            raise ValueError("Missing availability observation")
        fields_are_scalars(observation,strings=("host_state","adapter_state","source","host_xdev_version"),
                          times=("observed_at",),integers=("model_calls",))
        declaration = observation.get("declaration", {})
        if not isinstance(declaration, dict) or declaration.keys() - {"state","source","reason","observed_at","expires_at"}:
            raise ValueError("Invalid availability declaration")
        fields_are_scalars(declaration,strings=("state","source","reason"), times=("observed_at","expires_at"))
    else:
        fields = {"id", "scope", "received_at", "read_at", "read_scope", "withdrawn_at", "replacement_id", "version",
                  "work_state", "work_at", "communication_intent", "reply_intent", "reply_intent_at",
                  "reason", "revisions", "revision_of", "more_revisions"}
        if result.keys() - (common | fields) or result.get("id") != letter_id or "status" not in result:
            raise ValueError("Unmatched lifecycle response")
        from src.runtime import mail_lifecycle
        fields_are_scalars(result,strings=("reason","replacement_id"),integers=("version",),flags=("more_revisions",),
            times=("received_at","read_at","withdrawn_at","work_at","reply_intent_at"),
            enums={"scope":{"recipient_mailbox"}, "work_state":mail_lifecycle.WORK,"reply_intent":mail_lifecycle.INTENT,
                   "read_scope":{"session","device"},"communication_intent":{"inform","invite","request_reply","request_action"}})
        links = result.get("revisions", [])
        if not isinstance(links,list) or len(links)>20 or any(not isinstance(link,dict) or set(link) != {"revision","operation","at"} for link in links):
            raise ValueError("Invalid correction references")
        parent = result.get("revision_of")
        if parent is not None and (not isinstance(parent,dict) or set(parent) != {"original","operation","at"}):
            raise ValueError("Invalid correction parent")
        for link in [*links, *([parent] if parent else [])]:
            messages._string(link, "original" if "original" in link else "revision")
            if not isinstance(link["operation"],str) or link["operation"] not in ("replace","correct") or link["at"] is None:
                raise ValueError("Invalid correction reference")
            fields_are_scalars(link,times=("at",))
    return result


def accept(settings, source, frame):
    from src.runtime import mail_lifecycle

    identifier = frame["id"]
    if identifier.startswith("mc-a-"):
        if set(frame) != {"from", "to", "id", "request", "result"} or not isinstance(frame["result"], dict):
            raise ValueError("Invalid mailbox control result")
        rid = frame["request"]
        if not isinstance(rid, str) or rid != "mc-r-" + identifier[5:]:
            raise ValueError("Unmatched mailbox control result ID")
        with closing(db_open(settings.root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # The request hash binds the peer. Persist that peer explicitly to reject other paired devices.
            row = db.execute("SELECT result,letter_id FROM requests WHERE request=? AND peer=?", (rid, source)).fetchone()
            if not row:
                raise ValueError("Unsolicited mailbox control result")
            result = encoded(checked_result(frame["result"], row[1]))
            if row[0] is not None and row[0] != result:
                raise ValueError("Changed mailbox control result")
            db.execute("UPDATE requests SET result=?,observed=? WHERE request=? AND result IS NULL",
                       (result, int(time.time()), rid))
        return {"status": "control_result_received"}
    if set(frame) != {"from", "to", "id", "data", "epoch"} or type(frame["epoch"]) is not int:
        raise ValueError("Invalid mailbox control request")
    data = payload_for(settings.devices, source, settings.local, frame["data"])
    if identifier != "mc-r-" + digest([source, settings.local, data, frame["epoch"]]):
        raise ValueError("Changed mailbox control request")
    with closing(db_open(settings.root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        found = db.execute("SELECT result FROM handled WHERE request=?", (identifier,)).fetchone()
        if found:
            result = json.loads(found[0])
        else:
            target = data.pop("target")
            result = {"ok": False, "status": "unavailable"}
            if "id" in data:
                result["id"] = data["id"]
            if settings.allows(source, target) and data["op"] == "preflight":
                from src.runtime import mail_contact
                result = {"ok": True, "observation": mail_contact.local_probe(settings.root, target)}
            elif settings.allows(source, target):
                data.setdefault("operation_id", identifier)
                result = mail_lifecycle.apply(settings.root, data, authenticated_peer=source, expected_target=target)
            if len(result.get("revisions", [])) > 20:
                result = {**result, "revisions": result["revisions"][-20:], "more_revisions": True}
            if len(encoded(checked_result(result, data.get("id"))).encode("utf-8")) > MAX_BYTES:
                raise ValueError("Oversize lifecycle result")
            now = int(time.time())
            db.execute("INSERT INTO handled(request,result,peer,created,expires) VALUES(?,?,?,?,?)",
                       (identifier, encoded(result), source, now, now+settings.ttl))
    return {"status": "control_processed"}


def flush(api, settings, limit):
    """Send previously committed outcomes even after their inbound request was deleted."""
    from src.runtime import github_mail

    if not (settings.root / "state" / "mail-control.sqlite3").exists():
        return
    now = int(time.time())
    with closing(db_open(settings.root)) as db:
        paused = db.execute("SELECT until_time FROM response_backoff WHERE singleton=1").fetchone()
        if paused and paused[0] > now:
            return
        due = db.execute("SELECT request,result,peer,expires,created FROM handled WHERE sent=0 AND due<=? "
                         "AND (expires IS NULL OR expires>?) ORDER BY due,created,rowid LIMIT ?",
                         (now, now, min(limit,10))).fetchall()
        for rid, saved, peer, expires, created in due:
            aid = "mc-a-" + rid[5:]
            if peer not in settings.boxes:
                with db:
                    db.execute("UPDATE handled SET due=? WHERE request=?",(now+3600,rid))
                continue
            if expires is None:
                expires = original_expiry(settings.root, peer, aid) or created + settings.ttl
                with db:
                    db.execute("UPDATE handled SET expires=? WHERE request=?", (expires,rid))
                if now >= expires:
                    continue
            answer = {"from": settings.local, "to": peer, "id": aid, "request": rid, "result": json.loads(saved)}
            try:
                github_mail._send(api, settings, {"_control": answer, "to": peer, "id": aid}, None)
            except (github_mail.NotSubmitted, github_mail.Unavailable) as exc:
                # A lost POST acknowledgment may remain unknowable even after the other side
                # consumed that response. Its upload journal permits reconciliation only.
                throttle = max(getattr(exc,"retry_after",0), getattr(exc.__cause__,"retry_after",0))
                with db:
                    db.execute("UPDATE handled SET due=? WHERE request=?", (now+max(60,throttle), rid))
                    if throttle:
                        db.execute("""INSERT INTO response_backoff(singleton,until_time) VALUES(1,?)
                                   ON CONFLICT(singleton) DO UPDATE SET until_time=MAX(until_time,excluded.until_time)""",
                                   (now+throttle,))
                if throttle:
                    break
            else:
                with db:
                    db.execute("UPDATE handled SET sent=1 WHERE request=?", (rid,))
