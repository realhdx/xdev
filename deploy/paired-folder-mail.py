"""An enrolled private folder represents one mail participant on this device.

The host writes immutable UTF-8 JSON requests atomically to out/ and read/.
Only the installed xdev message commands run. The state directory belongs to
the gateway and must be outside the host's shared folder.
"""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata


CLIENT = "paired-folder"
SLOTS = ("out", "read", "in", "results", "control")
INTENTS = ("inform", "invite", "request_reply", "request_action")
DELIVERY_EVIDENCE = {
    "linked_reply", "recipient_read", "recipient_withdrawal", "recipient_mailbox",
    "device_read", "session_accepted", "session_queued", "inbox_receipt", "carrier_report",
    "carrier_ack", "not_submitted", "delivery_rejected",
}
DELIVERY_STATES = {"withdrawn", "replied", "read", "accepted", "queued", "pending_wake",
                   "mailbox_received", "submitted", "failed", "unknown"}
DELIVERY_RETRIES = {"check_same_id", "never", "retry_same_id"}
MAX_BYTES = 32 * 1024
BATCH = 16
OP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,158}[A-Za-z0-9_-]\Z|[A-Za-z0-9]\Z")
RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def ident(value, pattern=MAIL):
    if (
        not isinstance(value, str)
        or not pattern.fullmatch(value)
        or RESERVED.match(value)
    ):
        raise ValueError("invalid_identifier")
    return value


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("use_absolute_path")
    for part in (*reversed(path.parents), path):
        if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("linked_path")
    return path


def read_request(path):
    safe_path(path)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_BYTES
    ):
        raise ValueError("invalid_file")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as source:
        if not os.path.samestat(before, os.fstat(source.fileno())):
            raise ValueError("file_changed")
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("message_too_large")

    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate_field")
            result[k] = v
        return result

    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)


def put(path, obj):
    safe_path(path.parent)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf8"
    ) as tmp:
        name = tmp.name
        json.dump(obj, tmp, ensure_ascii=False)
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run_cli(cfg, args):
    env = os.environ.copy()
    env.pop("CODEX_THREAD_ID", None)
    cmd = [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        str(safe_path(Path(cfg["root"]) / "launch.py")),
    ]
    result = subprocess.run(
        [*cmd, "msg", *args],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        env=env,
    )
    obj = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(obj, dict):
        raise ValueError("invalid_xdev_reply")
    return obj


def database(state):
    safe_path(state)
    return sqlite3.connect(state / "mail.sqlite", timeout=2)


def attach_inbox(db, cfg):
    """Share the local inbox write lock while materializing a body from an earlier query."""
    path = Path(cfg["root"]) / "state" / "messages.sqlite3"
    if not path.exists():
        return None
    db.execute("ATTACH DATABASE ? AS inbox", (str(safe_path(path)),))
    columns = {entry[1] for entry in db.execute("PRAGMA inbox.table_info(messages)")}
    return "withdrawn_at" in columns


def refresh_materialized_mail(db, cfg):
    """Redact previously materialized copies in this gateway's own inbound slot."""
    columns = {row[1] for row in db.execute("PRAGMA inbox.table_info(messages)")}
    archive = "m.read_at IS NOT NULL AND m.archive_digest IS NOT NULL" if "archive_digest" in columns else "0"
    rows = db.execute(f"""
        SELECT l.id,m.withdrawn_at,m.replacement_id FROM letters l
        JOIN inbox.messages m ON m.id=l.id WHERE l.exported=1 AND (m.withdrawn_at IS NOT NULL OR ({archive}))
        ORDER BY l.rowid LIMIT ?""", (BATCH,)).fetchall()
    for mid, at, replacement in rows:
        path = Path(cfg["folder"]) / "in" / (ident(mid) + ".json")
        with db:
            db.execute("BEGIN IMMEDIATE")
            still_removed = db.execute(
                f"SELECT 1 FROM inbox.messages m WHERE m.id=? AND (m.withdrawn_at IS NOT NULL OR ({archive}))", (mid,)
            ).fetchone()
            if still_removed:
                marker = ({"id": mid, "status": "recalled", "withdrawn_at": at, "replacement_id": replacement} if at
                          else {"id": mid, "status": "archived"})
                # This name was previously recorded against this enrollment and is gateway-owned.
                # Atomic replacement never writes through a planted file or directory link.
                put(path, marker)
                db.execute("UPDATE letters SET exported=0 WHERE id=?", (mid,))


def init(
    state,
    folder,
    root,
    local_session,
    peer_device,
    peer_client,
    peer_session,
    call=run_cli,
):
    state, folder, root = map(Path, (state, folder, root))
    for path in (state, folder):
        if not path.exists():
            safe_path(path.parent)
            path.mkdir(mode=0o700)
        safe_path(path)
    safe_path(root / "launch.py")
    if any(
        folder.is_relative_to(path) or path.is_relative_to(folder)
        for path in (state, root)
    ):
        raise ValueError("separate_private_state_install_and_shared_folder")
    cfg = dict(
        folder=str(folder),
        root=str(root),
        local_session=ident(local_session),
        peer_device=ident(peer_device),
        peer_client=ident(peer_client),
        peer_session=ident(peer_session),
    )
    cfg["local_device"] = ident(call(cfg, ["whoami"])["device"])
    with closing(database(state)) as db, db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, config TEXT, cursor INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS letters (id TEXT PRIMARY KEY, receipt INTEGER DEFAULT 0, exported INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ops (id TEXT PRIMARY KEY COLLATE NOCASE, digest TEXT, status TEXT, mid TEXT, retry_after INTEGER);
        """)
        old = db.execute("SELECT config FROM settings WHERE id=1").fetchone()
        if old and json.loads(old[0]) != cfg:
            raise ValueError("enrollment_conflict")
        if not old and any(folder.iterdir()):
            raise ValueError("dedicated_empty_folder_required")
        db.execute(
            "INSERT OR IGNORE INTO settings(id,config) VALUES(1,?)", (json.dumps(cfg),)
        )
        db.commit()  # A restart may finish creating slots for the same enrollment.
        for name in SLOTS:
            (folder / name).mkdir(mode=0o700, exist_ok=True)
            safe_path(folder / name)
    return {
        "enrolled": True,
        "local_client": CLIENT,
        **{k: v for k, v in cfg.items() if k not in ("folder", "root")},
    }


def pending(state, after=0):
    if type(after) is not int or not 0 <= after < 2**63:
        raise ValueError("invalid_cursor")
    with closing(database(Path(state))) as db:
        cfg = json.loads(db.execute("SELECT config FROM settings WHERE id=1").fetchone()[0])
        available = attach_inbox(db, cfg)
        eligible = (" AND EXISTS(SELECT 1 FROM inbox.messages m WHERE m.id=letters.id"
                    " AND m.withdrawn_at IS NULL AND m.read_at IS NULL)") if available else ""
        rows = db.execute(
            "SELECT rowid,id FROM letters WHERE exported=1 AND receipt=0 AND rowid>? " + eligible + " " +
            "ORDER BY rowid LIMIT 51",
            (after,),
        ).fetchall()
        return {
            "pending": [r[1] for r in rows[:50]],
            "has_more": len(rows) > 50,
            "next_cursor": rows[min(50, len(rows)) - 1][0] if rows else after,
        }


def address(row, field):
    endpoint = (
        row.get("sender_endpoint" if field == "from" else "recipient_identity") or {}
    )
    canonical = row.get("canonical_addresses") or {}
    return {
        "device": canonical.get(field, row.get(field)),
        "client": endpoint.get("client"),
        "session": endpoint.get("session"),
    }


def endpoints(cfg):
    return (
        dict(device=cfg["local_device"], client=CLIENT, session=cfg["local_session"]),
        dict(
            device=cfg["peer_device"],
            client=cfg["peer_client"],
            session=cfg["peer_session"],
        ),
    )


def claim_letters(db, cfg, call, counts, actual_inbox):
    local, peer = endpoints(cfg)
    cursor = db.execute("SELECT cursor FROM settings WHERE id=1").fetchone()[0]
    result = call(
        cfg,
        [
            "inbox",
            "--as-client",
            CLIENT,
            "--as-session",
            local["session"],
            "--from",
            peer["device"],
            "--from-client",
            peer["client"],
            "--from-session",
            peer["session"],
            "--after",
            str(cursor),
            "--limit",
            str(BATCH),
        ],
    )
    if not isinstance(result.get("messages"), list):
        raise ValueError("inbox_unavailable")
    if actual_inbox is None and call is run_cli:
        # The first inbox query may initialize the local installation. Rebind before using
        # its snapshot or any existing entries from an earlier installation.
        actual_inbox = attach_inbox(db, cfg)
        if actual_inbox is None:
            raise ValueError("local_inbox_unavailable")
    for m in result["messages"]:
        seq = m["seq"]
        if type(seq) is not int or seq <= cursor:
            raise ValueError("invalid_mail_cursor")
        if (
            m.get("kind") != "receipt"
            and not m.get("withdrawn_at")
            and not m.get("content_archived")
            and address(m, "from") == peer
            and address(m, "to") == local
        ):
            try:
                mid = ident(m["id"])
                body = m["text"]
                if not isinstance(body, str) or len(body.encode("utf8")) > MAX_BYTES:
                    raise ValueError("inbound_too_large")
                obj = dict(
                    id=mid,
                    subject=m.get("subject") or m.get("subject_preview"),
                    text=body,
                    reply_to=m.get("reply_to"),
                    sender=peer,
                    recipient=local,
                )
                if m.get("communication_intent"):
                    obj["communication_intent"] = m["communication_intent"]
            except ValueError:
                counts["rejected"] += 1
            else:
                # A filesystem error must leave the cursor before this letter.
                reader = m.get("read_by_endpoint") or {}
                has_read = bool(
                    m.get("read_at")
                    and reader.get("client") == CLIENT
                    and reader.get("session") == local["session"]
                )
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    # The CLI result is a snapshot. Recall/archive may have won since the query.
                    current = (not actual_inbox or db.execute(
                        "SELECT 1 FROM inbox.messages WHERE id=? AND withdrawn_at IS NULL AND text=?",
                        (mid, body)).fetchone())
                    if current:
                        put(Path(cfg["folder"]) / "in" / (mid + ".json"), obj)
                        db.execute(
                            "INSERT OR IGNORE INTO letters(id,exported,receipt) VALUES(?,1,?)",
                            (mid, has_read),
                        )
                        counts["exported"] += 1
        with db:
            db.execute("UPDATE settings SET cursor=? WHERE id=1", (seq,))
        cursor = seq
    return actual_inbox


def checked_request(slot, op, obj):
    if not isinstance(obj, dict) or obj.get("op_id") != op:
        raise ValueError("invalid_operation")
    if slot == "control":
        return check_control(obj)
    if slot == "read":
        if set(obj) != {"op_id", "message_id"}:
            raise ValueError("invalid_fields")
        ident(obj["message_id"])
    else:
        if not {"op_id","subject","text"} <= obj.keys() or obj.keys() - {"op_id","subject","text","reply_to","intent"}:
            raise ValueError("invalid_fields")
        if "intent" in obj and obj["intent"] not in INTENTS:
            raise ValueError("invalid_intent")
        if "reply_to" in obj:
            ident(obj["reply_to"])
        sub = obj["subject"]
        if (
            not isinstance(sub, str)
            or not sub.strip()
            or any(unicodedata.category(x)[0] == "C" for x in sub)
        ):
            raise ValueError("invalid_subject")
        if (
            sum(2 if unicodedata.east_asian_width(x) in {"F", "W"} else 1 for x in sub)
            > 96
        ):
            raise ValueError("invalid_subject")
        if (
            not isinstance(obj["text"], str)
            or not obj["text"].strip()
            or "\0" in obj["text"]
            or len(obj["text"].encode("utf-16-le")) > 16384
        ):
            raise ValueError("invalid_text")


CONTROL_FIELDS = {
    "prepare": ((), ()), "preflight": ((), ()),
    "availability": (("state",), ("ttl","reason")),
    "report": (("message_id",), ("work_state","reply_intent","reason","expected_version")),
    "status": (("message_id",), ()), "recall": (("message_id",), ("expected_version",)),
    "replace": (("message_id","replacement_id"), ("expected_version",)),
    "correct": (("message_id","replacement_id"), ("expected_version",)),
}
CONTROL_VALUES = {
    "state": ("available","busy","maintenance","quota_limited","unknown"),
    "work_state": ("pending","in_progress","blocked","completed"),
    "reply_intent": ("undecided","will_reply","no_reply"),
}


def check_control(obj):
    action = obj.get("action")
    if not isinstance(action,str) or action not in CONTROL_FIELDS:
        raise ValueError("invalid_control_action")
    required, optional = map(set, CONTROL_FIELDS[action])
    fields = set(obj) - {"op_id","action"}
    if not required <= fields or fields - required - optional:
        raise ValueError("invalid_control_fields")
    for key in ("message_id","replacement_id"):
        if key in fields:
            ident(obj[key])
    for key, allowed in CONTROL_VALUES.items():
        if key in fields and obj[key] not in allowed:
            raise ValueError("invalid_control_value")
    if action == "report" and not fields.intersection({"work_state","reply_intent"}):
        raise ValueError("missing_work_state_or_reply_intent")
    if "ttl" in fields and (type(obj["ttl"]) is not int or not 1 <= obj["ttl"] <= 86400):
        raise ValueError("invalid_ttl")
    if "expected_version" in fields and (type(obj["expected_version"]) is not int or not 0 <= obj["expected_version"] < 2**63):
        raise ValueError("invalid_version")
    if "reason" in fields:
        reason = obj["reason"]
        if (not isinstance(reason,str) or not reason.strip() or len(reason)>256 or
                any(unicodedata.category(ch).startswith("C") for ch in reason) or
                (action == "report" and obj.get("work_state") != "blocked")):
            raise ValueError("invalid_reason")


def observed_fields(value):
    # Host observations may also contain installation paths, internal callbacks or
    # dispatch metadata. The paired folder exposes only this correspondence evidence.
    scalars = {"ok","id","version","status","scope","state","source","reason","observed_at","expires_at","expired",
               "read_at","read_scope","received_at","withdrawn_at","replacement_id","work_state","work_at",
               "communication_intent","reply_intent","reply_intent_at","adapter_state","host_state","live_check",
               "observation_request","requested_at","response_observed_at","queried_at","reply_evidence",
               "session_accepted","inbox_status","transport","submission_status","original","operation","at"}
    nested = {"preflight","observation","declaration","recipient_observation","evidence","linked_reply","revision_of"}
    result = {}
    for key, item in value.items():
        if key in scalars and (item is None or type(item) in (str,bool,int)):
            result[key] = item[:512] if isinstance(item,str) else item
        elif key in nested and isinstance(item,dict):
            result[key] = observed_fields(item)
        elif key == "delivery" and isinstance(item, dict):
            summary = {field: item[field] for field, allowed in (("state", DELIVERY_STATES), ("retry", DELIVERY_RETRIES))
                       if isinstance(item.get(field), str) and item[field] in allowed}
            evidence = item.get("evidence")
            if isinstance(evidence, list):
                summary["evidence"] = list(dict.fromkeys(e for e in evidence if isinstance(e, str) and e in DELIVERY_EVIDENCE))
            result[key] = summary
    return result


def control(db, cfg, op, obj, call):
    action = obj["action"]
    base = {"op_id":op,"action":action}
    mid = obj.get("message_id")
    local, peer = endpoints(cfg)
    def outbound(identifier):
        attempts = db.execute("SELECT id FROM ops WHERE mid=? AND status IN "
                              "('mailbox_received','session_accepted','unknown')", (identifier,))
        return any(outgoing_id(local,peer,attempt[0]) == identifier for attempt in attempts)
    if mid:
        inbound = db.execute("SELECT 1 FROM letters WHERE id=?", (mid,)).fetchone()
        if action == "report":
            grant = inbound
        elif action == "status":
            grant = inbound or outbound(mid)
        else:
            grant = outbound(mid)
        if not grant or ("replacement_id" in obj and not outbound(obj["replacement_id"])):
            return {**base,"status":"rejected","reason":"not_in_enrolled_exchange"}
    digest = hashlib.sha256(json.dumps(["control",obj],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("INSERT OR IGNORE INTO ops(id,digest,status,mid,retry_after) VALUES(?,?,'control',?,0)",
                          (op,digest,mid)).rowcount:
            original, = db.execute("SELECT digest FROM ops WHERE id=?", (op,)).fetchone()
            if original != digest:
                return {**base,"status":"rejected","reason":"op_id_conflict"}
            saved = db.execute("SELECT result FROM control_results WHERE op=?", (op,)).fetchone()
            return json.loads(saved[0]) if saved else {**base,"status":"unknown"}
    if action in ("preflight","prepare"):
        target = local if action == "prepare" else peer
        args = ["preflight","--to",target["device"],"--client",target["client"],"--session",target["session"]]
    else:
        args = [action] + ([mid] if mid else [])
        if action in ("report","recall","replace","correct"):
            args += ["--operation-id", "folder-control-" + digest]
        for field in ("state","ttl","reason","work_state","reply_intent","expected_version","replacement_id"):
            if field in obj:
                flag = "--with" if field == "replacement_id" else "--" + field.replace("_","-")
                args.append(flag + "=" + str(obj[field]))
    args += ["--as-client", CLIENT,"--as-session",local["session"]]
    try:
        raw = call(cfg,args)
        fields = observed_fields(raw)
        status = fields.get("status") or ("observed" if fields.get("ok") else "unconfirmed")
        result = {**base,"status":status,"observation":fields}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        result = {**base,"status":"unknown"}
    with db:
        db.execute("INSERT INTO control_results VALUES(?,?)", (op,json.dumps(result)))
    return result


def reconcile(cfg, obj, mid, call):
    local, peer = endpoints(cfg)
    q = call(
        cfg,
        [
            "wait",
            "--host",
            peer["device"],
            "--timeout",
            "0",
            "--id",
            mid,
            "--to-client",
            peer["client"],
            "--to-session",
            peer["session"],
            "--from",
            local["device"],
            "--from-client",
            CLIENT,
            "--from-session",
            local["session"],
        ],
    )
    msg = q.get("message") or {}
    if (
        msg.get("id") == mid
        and msg.get("text") == obj["text"]
        and msg.get("reply_to") == obj.get("reply_to")
        and msg.get("subject") == " ".join(obj["subject"].split())
        and address(msg, "from") == local
        and address(msg, "to") == peer
    ):
        return "mailbox_received"
    return "unknown"


def outgoing_id(local, peer, op):
    return "folder-" + hashlib.sha256(json.dumps([local,peer,op], sort_keys=True).encode()).hexdigest()


def transact(db, cfg, slot, op, obj, call, actual_inbox):
    encoded = json.dumps(
        [slot, obj], ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    local, peer = endpoints(cfg)
    mid = outgoing_id(local,peer,op)
    if slot == "read":
        mid = obj["message_id"]
    row = db.execute(
        "SELECT digest,status,mid,retry_after FROM ops WHERE id=?", (op,)
    ).fetchone()
    now = int(time.time())
    if row and row[0] != digest:
        return {
            "op_id": op,
            "status": "rejected",
            "reason": "op_id_conflict",
            "message_id": row[2],
        }
    if row and (row[1] != "unknown" or row[3] > now):
        return {"op_id": op, "status": row[1], "message_id": row[2]}
    ref = obj.get("message_id") or obj.get("reply_to")
    # A previously attempted send may already have reached its peer. Its result can
    # only be reconciled by the deterministic outgoing ID, even if the source was recalled.
    reconciling = bool(row and slot == "out")
    if ref and actual_inbox and not reconciling:
        source = db.execute("SELECT withdrawn_at,read_at FROM inbox.messages WHERE id=?", (ref,)).fetchone()
        enrolled = db.execute("SELECT 1 FROM letters WHERE id=?", (ref,)).fetchone()
        if enrolled and source and source[0]:
            return {"op_id": op, "status": "recalled", "message_id": mid, "source_message_id": ref}
        if enrolled and source and slot == "out" and not source[1] and not row:
            return {"op_id": op, "status": "read_required", "message_id": mid}
    if (
        ref and not reconciling
        and not db.execute(
            "SELECT 1 FROM letters WHERE id=? AND exported=1", (ref,)
        ).fetchone()
    ):
        return {
            "op_id": op,
            "status": "rejected",
            "reason": "not_in_enrolled_inbox",
            "message_id": mid,
        }
    with db:
        db.execute(
            "INSERT OR IGNORE INTO ops VALUES(?,?,?,?,?)",
            (op, digest, "unknown", mid, now + 60),
        )
        db.execute("UPDATE ops SET retry_after=? WHERE id=?", (now + 60, op))
    status = "unknown"
    try:
        if slot == "read":
            # The xdev receipt command keeps the first reader and time immutable.
            q = call(
                cfg,
                ["read", mid, "--as-client", CLIENT, "--as-session", local["session"]],
            )
            reader = q.get("read_by_endpoint") or {}
            if q.get("id") == mid and q.get("status") == "recalled":
                status = "recalled"
            if (
                q.get("status") == "read"
                and q.get("id") == mid
                and reader.get("client") == CLIENT
                and reader.get("session") == local["session"]
            ):
                status = "read"
        elif row:
            status = reconcile(cfg, obj, mid, call)
        else:
            argv = [
                "send",
                "--transport",
                "direct",
                "--to",
                peer["device"],
                "--client",
                peer["client"],
                "--session",
                peer["session"],
                "--from-client",
                CLIENT,
                "--from-session",
                local["session"],
                "--return-client",
                CLIENT,
                "--return-session",
                local["session"],
                "--id",
                mid,
                "--subject=" + obj["subject"],
                "--text=" + obj["text"],
            ]
            if ref:
                argv.extend(["--kind", "reply", "--reply-to", ref])
            if "intent" in obj:
                argv.extend(["--intent", obj["intent"]])
            q = call(cfg, argv)
            if q.get("id") == mid and q.get("session_accepted") is True:
                status = "session_accepted"
            elif q.get("id") == mid and q.get("inbox_status") in {
                "received",
                "duplicate",
            }:
                status = "mailbox_received"
            elif q.get("inbox_status") == "not_attempted":
                status = "rejected"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Transmission may have succeeded. Only a lookup is safe now.
    with db:
        db.execute("UPDATE ops SET status=? WHERE id=?", (status, op))
        if status == "read":
            db.execute("UPDATE letters SET receipt=1 WHERE id=?", (mid,))
    return {"op_id": op, "status": status, "message_id": mid}


@contextmanager
def step_lock(state):
    # OS releases this independent SQLite writer lock if the process ends mid-call.
    with closing(sqlite3.connect(state / "worker.lock.sqlite", timeout=0)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        yield


def step(state, call=run_cli):
    state = safe_path(Path(state))
    with step_lock(state), closing(database(state)) as db:
        cfg = json.loads(
            db.execute("SELECT config FROM settings WHERE id=1").fetchone()[0]
        )
        folder = safe_path(Path(cfg["folder"]))
        # Upgrade an already enrolled dedicated folder without requiring another handshake.
        (folder / "control").mkdir(mode=0o700, exist_ok=True)
        db.execute("CREATE TABLE IF NOT EXISTS control_results(op TEXT PRIMARY KEY COLLATE NOCASE, result TEXT)")
        for name in SLOTS:
            safe_path(folder / name)
        counts = dict(exported=0, handled=0, rejected=0, unknown=0)
        actual_inbox = attach_inbox(db, cfg)
        if actual_inbox:
            refresh_materialized_mail(db, cfg)
        actual_inbox = claim_letters(db, cfg, call, counts, actual_inbox)
        callbacks = 0

        def bounded_call(settings, args):
            nonlocal callbacks
            callbacks += 1
            return call(settings, args)

        for slot in ("read", "out", "control"):
            for path in (folder / slot).glob("*.json"):
                if counts["handled"] >= BATCH or callbacks >= BATCH:
                    break
                try:
                    op = ident(path.stem, OP)
                    request = read_request(path)
                except (ValueError, OSError, RecursionError):
                    counts["rejected"] += 1
                    continue
                try:
                    checked_request(slot, op, request)
                    if slot == "control":
                        result = control(db, cfg, op, request, bounded_call)
                    else:
                        result = transact(db, cfg, slot, op, request, bounded_call, actual_inbox)
                except ValueError:
                    result = {
                        "op_id": op,
                        "status": "rejected",
                        "reason": "invalid_request",
                        "message_id": None,
                    }
                dest = folder / "results" / (op + ".json")
                try:
                    if dest.exists() and read_request(dest) == result:
                        continue
                except (ValueError, OSError, RecursionError):
                    pass  # Replace the directory entry, never follow a planted output link.
                put(dest, result)
                counts["handled"] += 1
                if result["status"] in ("unknown", "rejected"):
                    counts[result["status"]] += 1
        return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    reg = modes.add_parser("init")
    for flag in (
        "state",
        "folder",
        "xdev-root",
        "local-session",
        "peer-device",
        "peer-client",
        "peer-session",
    ):
        reg.add_argument("--" + flag, required=True)
    modes.add_parser("step").add_argument("--state", required=True)
    pending_mode = modes.add_parser("pending")
    pending_mode.add_argument("--state", required=True)
    pending_mode.add_argument("--after", type=int, default=0)
    args = parser.parse_args()
    if args.mode == "init":
        return init(
            args.state,
            args.folder,
            args.xdev_root,
            args.local_session,
            args.peer_device,
            args.peer_client,
            args.peer_session,
        )
    return step(args.state) if args.mode == "step" else pending(args.state, args.after)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=True))
    except (
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:180]},
                ensure_ascii=True,
            )
        )
        sys.exit(1)
