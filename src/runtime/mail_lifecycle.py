"""Recipient-owned observations and changes to an already addressed, immutable mail identity."""
import hashlib
import json
import unicodedata

from src.runtime import messages, transport


OPS = {"status", "recall", "replace", "correct", "progress"}
WORK = {"pending", "in_progress", "blocked", "completed"}
INTENT = {"undecided", "will_reply", "no_reply"}


def removed_digest(row):
    payload = [row[field] for field in ("text", "letter_attachment", "subject")]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mail_observations(
               id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0, work TEXT, work_at TEXT,
               intent TEXT, intent_at TEXT, reason TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mail_revisions(
               original TEXT NOT NULL, revision TEXT PRIMARY KEY, operation TEXT NOT NULL, at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS mail_lifecycle_ops(
               id TEXT NOT NULL, opid TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
               PRIMARY KEY(id,opid))""")


def _snapshot(db, row, root, receiver):
    work = db.execute("SELECT * FROM mail_observations WHERE id=?", (row["id"],)).fetchone()
    links = db.execute("SELECT revision,operation,at FROM mail_revisions WHERE original=? ORDER BY rowid",
                       (row["id"],)).fetchall()
    parent = db.execute("SELECT original,operation,at FROM mail_revisions WHERE revision=?", (row["id"],)).fetchone()
    read_scope = None
    if row["read_at"]:
        if row["read_by_endpoint"] is None:
            read_scope = "device"
        else:
            # Legacy or damaged reader records must not certify a different chat.
            try:
                reader = messages.normalize_participant(json.loads(row["read_by_endpoint"]))
            except (TypeError, ValueError):
                reader = None
            if messages.matches_participant(reader, receiver, exact=True, root=root):
                read_scope = "session"
    return {"ok": True, "status": "observed", "id": row["id"], "scope": "recipient_mailbox",
            "read_scope": read_scope,
            "received_at": row["received_at"], "read_at": row["read_at"], "withdrawn_at": row["withdrawn_at"],
            "replacement_id": row["replacement_id"], "version": work["version"] if work else 0,
            "communication_intent": row["communication_intent"],
            "work_state": work["work"] if work else None, "work_at": work["work_at"] if work else None,
            "reply_intent": work["intent"] if work else None, "reply_intent_at": work["intent_at"] if work else None,
            "reason": work["reason"] if work else None, "revisions": [dict(r) for r in links],
            "revision_of": dict(parent) if parent else None}


def _participants(row, root):
    if row is None or row["envelope_version"] < 2 or row["kind"] not in ("message", "reply"):
        return None, None
    # Addresses in the envelope are authoritative. Later local routing annotations
    # cannot confer a right to recall or report on a historical letter.
    def participant(field):
        raw = row[field]
        return messages.normalize_participant(json.loads(raw)) if raw else None
    return participant("sender_endpoint"), participant("recipient_identity")


def _params(payload):
    op = payload.get("op")
    extras = {"status": set(), "recall": set(), "replace": {"replacement_id"}, "correct": {"replacement_id"},
              "progress": {"work_state", "reply_intent", "reason"}}
    if not isinstance(op, str) or op not in OPS or set(payload) - ({"id","op","actor","operation_id","expected_version"} | extras[op]):
        raise ValueError("Unknown lifecycle operation or field")
    ident = messages._string(payload, "id")
    if op in {"replace","correct"}:
        messages._string(payload, "replacement_id")
    if payload.get("operation_id") is not None:
        messages._string(payload, "operation_id")
    if payload.get("expected_version") is not None:
        messages._integer(payload, "expected_version", 0, 0, 2**63-1)
    if op == "progress":
        if not ({"reply_intent", "work_state"} & payload.keys()):
            raise ValueError("Supply a work state or reply intent")
        if "work_state" in payload and (not isinstance(payload["work_state"], str) or payload["work_state"] not in WORK):
            raise ValueError("Unknown work state")
        if "reply_intent" in payload and (not isinstance(payload["reply_intent"], str) or payload["reply_intent"] not in INTENT):
            raise ValueError("Unknown reply intent")
        if "reason" in payload:
            reason = messages._string(payload, "reason", max_chars=256)
            if payload.get("work_state") != "blocked" or any(unicodedata.category(ch)[0] == "C" for ch in reason):
                raise ValueError("Supply a short reason only when reporting blocked work")
    return op, ident


def apply(root, payload, *, authenticated_peer=None, expected_target=None):
    """A remote carrier supplies authenticated_peer from its own verified context."""
    op, ident = _params(payload)
    actor = messages.normalize_participant(payload.get("actor"))
    devices = transport.directory(root)
    denied = {"ok": False, "id": ident, "status": "unavailable"}
    origin = devices.node
    if authenticated_peer is not None:
        try:
            origin = devices.resolve(authenticated_peer)
        except messages.UnknownDeviceError:
            return denied
        if origin == devices.node or expected_target is None:
            return denied
    if not actor or not devices.same(actor["device"], origin):
        return denied
    with messages._database(root) as db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        row = messages._stored_message(db, ident)
        author, receiver = _participants(row, root)
        if not author or not receiver or not devices.same(receiver["device"], devices.node):
            return denied
        if expected_target is not None and not messages.matches_participant(
                receiver, messages.normalize_participant(expected_target), exact=True, root=root):
            return denied
        is_author = messages.matches_participant(actor, author, exact=True, root=root)
        is_receiver = messages.matches_participant(actor, receiver, exact=True, root=root)
        allowed = {"status": is_author or is_receiver, "progress": is_receiver}
        if not allowed.get(op, is_author):
            return denied
        current = _snapshot(db, row, root, receiver)
        if op == "status":
            return current
        opid = payload.get("operation_id")
        if opid:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            old = db.execute("SELECT digest,result FROM mail_lifecycle_ops WHERE id=? AND opid=?", (ident, opid)).fetchone()
            if old:
                if old["digest"] != digest:
                    return {"ok": False, "id": ident, "status": "operation_conflict"}
                return json.loads(old["result"])
        if payload.get("expected_version") is not None and current["version"] != payload["expected_version"]:
            return {"ok": False, "id": ident, "status": "version_conflict", "version": current["version"]}
        result = _change(db, root, row, current, payload, author, receiver)
        if opid and result.get("ok"):
            db.execute("INSERT INTO mail_lifecycle_ops VALUES(?,?,?,?)", (ident, opid, digest, json.dumps(result)))
    if result.get("ok") and op in {"recall", "replace"}:
        try:
            messages._notify(root)
        except OSError:
            pass
    return result


def _change(db, root, row, current, request, author, receiver):
    op, ident, now = request["op"], row["id"], messages._timestamp()
    new = request.get("replacement_id")
    def no(status):
        return {**current, "ok": False, "status": status}
    if row["withdrawn_at"]:
        if (op == "recall" and not row["replacement_id"]) or (op == "replace" and row["replacement_id"] == new):
            return {**current, "status": "replaced" if new else "recalled"}
        return no("already_recalled")
    if op in ("recall", "replace") and row["read_at"]:
        return no("already_read")
    if op == "progress" and not row["read_at"]:
        return no("read_required")
    if op in ("replace", "correct"):
        if op == "correct" and not row["read_at"]:
            return no("unread_use_replace")
        candidate = messages._stored_message(db, new)
        a, b = _participants(candidate, root)
        if (not candidate or candidate["seq"] <= row["seq"] or candidate["withdrawn_at"]
                or not messages.matches_participant(a, author, exact=True, root=root)
                or not messages.matches_participant(b, receiver, exact=True, root=root)):
            return no("replacement_unavailable")
        if row["group_json"] != candidate["group_json"]:
            from src.runtime.correspondence import plan_group
            def group(message):
                encoded = message["group_json"]
                return plan_group(json.loads(encoded), root) if encoded else None
            if group(row) != group(candidate):
                return no("replacement_unavailable")
        link = db.execute("SELECT original,operation FROM mail_revisions WHERE revision=?", (new,)).fetchone()
        if link:
            if link["original"] == ident and link["operation"] == op:
                return {**current, "status": "corrected" if op=="correct" else "replaced"}
            return no("revision_conflict")
        db.execute("INSERT INTO mail_revisions VALUES(?,?,?,?)", (ident,new,op,now))
    if op == "progress":
        state = request.get("work_state", current["work_state"])
        intent = request.get("reply_intent", current["reply_intent"])
        reason = request.get("reason", current["reason"]) if state=="blocked" else None
        if current["work_state"] == "completed" and state != "completed":
            return no("completed_work")
        if (state,intent,reason) == (current["work_state"],current["reply_intent"],current["reason"]):
            return {**current, "status": "reported"}
        db.execute("INSERT OR IGNORE INTO mail_observations(id) VALUES(?)", (ident,))
        db.execute("UPDATE mail_observations SET work=?,work_at=?,reason=?,intent=?,intent_at=? WHERE id=?",
                   (state, now if (state,reason)!=(current["work_state"],current["reason"]) else current["work_at"],
                    reason, intent, now if intent!=current["reply_intent"] else current["reply_intent_at"], ident))
    if op in ("recall", "replace"):
        db.execute("""UPDATE messages SET withdrawn_at=?,withdrawn_digest=?,replacement_id=?,text='',
                       letter_attachment=NULL,subject='[recalled]' WHERE id=? AND read_at IS NULL""",
                   (now,removed_digest(row),new,ident))
    db.execute("INSERT INTO mail_observations(id,version) VALUES(?,1) ON CONFLICT(id) DO UPDATE SET version=version+1",
               (ident,))
    return {**_snapshot(db, messages._stored_message(db, ident), root, receiver),
            "status": {"recall":"recalled","replace":"replaced","correct":"corrected","progress":"reported"}[op]}
