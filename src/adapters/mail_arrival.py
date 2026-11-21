"""Host-owned idle-chat notifications for authenticated local GitHub mail events.

A host opens one Subscription for a locally selected chat and runs it in its own
worker. The callback is bound to that chat in the host process. It receives
only a local inbox sequence and an idempotency key. No model needs to poll.
"""

from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import secrets
import sqlite3
import time

from src.adapters import sessions
from src.runtime import device_names, github_mail, messages, outbox, session_relay, transport


def _reader(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


class Subscription:
    """Opening opts in for future mail. Reopening resumes this exact subscription."""

    def __init__(self, root, *, peer, client, session, profile=None, max_age=900):
        if type(max_age) is not int or not 1 <= max_age <= 86400:
            raise ValueError("max_age must be 1 through 86400 seconds")
        self.root = Path(root)
        directory = transport.directory(self.root)
        self.peer = directory.identity_key(peer)
        address = {"device": directory.identity_key(directory.node), "client": client, "session": session}
        profile_field = sessions.descriptor(client).get("profile_field")
        if profile is not None:
            if not profile_field:
                raise ValueError("This host does not support a profile selector")
            address[profile_field] = profile
        self.target = messages.normalize_participant(address)
        if self.peer == self.target["device"] or not self._allowed():
            raise ValueError("Enable this peer and local mailbox address before subscribing")
        self.max_age = max_age
        self._closed = False
        identity_target = dict(self.target)
        if profile is not None:
            identity_target[profile_field] = messages.profile_key(profile)
        identity = json.dumps([self.peer, identity_target], sort_keys=True, separators=(",", ":"))
        self.key = hashlib.sha256(identity.encode()).hexdigest()
        self.inbox = self.root / "state" / "messages.sqlite3"
        self.path = self.root / "state" / "github-mail.sqlite3"
        with closing(github_mail._journal(self.root)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS host_subscriptions(
                           subscription TEXT PRIMARY KEY, generation TEXT NOT NULL,
                           cursor INTEGER NOT NULL, initial_seq INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS host_arrivals(
                           generation TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL,
                           due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                           PRIMARY KEY(generation,seq))""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS host_arrivals_ready ON host_arrivals(generation,due,seq) "
                "WHERE state='ready'"
            )
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT generation FROM host_subscriptions WHERE subscription=?", (self.key,)
            ).fetchone()
            if row is None:
                self.generation = secrets.token_hex(16)
                # An old inbox row may gain its local event later via crash recovery.
                # Both watermarks are needed to prevent enabling an old unread backlog.
                initial = 0
                if self.inbox.is_file():
                    with closing(_reader(self.inbox)) as inbox:
                        if inbox.execute("SELECT 1 FROM sqlite_master WHERE name='messages'").fetchone():
                            initial = inbox.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0]
                db.execute(
                    "INSERT INTO host_subscriptions SELECT ?,?,COALESCE(MAX(event_id),0),? FROM local_events",
                    (self.key, self.generation, initial),
                )
            else:
                self.generation = row[0]

    def _allowed(self):
        config = transport.config(self.root)
        mailbox = device_names.github_mailbox_config(config)
        directory = device_names.DeviceDirectory(config).check_machine()
        if not mailbox or directory.identity_key(directory.node) != self.target["device"]:
            return False
        for name, grant in mailbox["peers"].items():
            if directory.identity_key(name) == self.peer:
                policy = session_relay.receiver_policy(
                    self.root, grant.get("receivers", []), grant.get("receiver_apps", [])
                )
                return session_relay.receiver_allowed(self.root, policy, self.target)
        return False

    def disable(self):
        """Cancel future admissions. A callback already in progress may finish."""
        self._closed = True
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(
                "DELETE FROM host_subscriptions WHERE subscription=? AND generation=?",
                (self.key, self.generation),
            )
            db.execute("DELETE FROM host_arrivals WHERE generation=?", (self.generation,))

    def _eligible(self, seq):
        if not self.inbox.is_file():
            return False
        with closing(_reader(self.inbox)) as inbox:
            columns = {c[1] for c in inbox.execute("PRAGMA table_info(messages)")}
            active = " AND withdrawn_at IS NULL" if "withdrawn_at" in columns else ""
            row = inbox.execute(
                """SELECT kind,sender,recipient,received_at,recipient_identity,recipient_endpoint,
                                   sender_endpoint,reply_to FROM messages WHERE seq=? AND read_at IS NULL
                                   AND envelope_version=2 AND kind IN ('message','reply')""" + active,
                (seq,),
            ).fetchone()
        if not row:
            return False
        kind, sender, recipient, received, identity, endpoint, author, parent = row
        directory = transport.directory(self.root)
        if (
            not directory.same(sender, self.peer)
            or not directory.same(recipient, self.target["device"])
            or not identity
            or time.time() - datetime.fromisoformat(received).timestamp() > self.max_age
        ):
            return False
        identity = messages.normalize_participant(json.loads(identity))
        if not messages.matches_participant(identity, self.target, exact=True, root=self.root):
            return False
        endpoint = json.loads(endpoint) if endpoint else None
        if endpoint and (endpoint.get("channel") == "tool_result" or endpoint.get("delivery") == "queue"):
            return False
        if kind == "message":
            return parent is None
        if not endpoint or not author:
            return False
        # Ingress has validated the non-empty body. Do not load it into this
        # host callback path. Authenticate the original sender and return chat again.
        return outbox.permits_reply(
            self.root,
            {
                "kind": kind,
                "text": "present",
                "reply_to": parent,
                "sender_endpoint": json.loads(author),
                "recipient_endpoint": endpoint,
            },
        )

    def _scan(self, limit):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT cursor,initial_seq FROM host_subscriptions WHERE generation=? AND subscription=?",
                (self.generation, self.key),
            ).fetchone()
            if not row:
                self._closed = True
                return []
            cursor, initial = row
            events = db.execute(
                "SELECT event_id,inbox_seq,kind FROM local_events WHERE event_id>? ORDER BY event_id LIMIT ?",
                (cursor, limit),
            ).fetchall()
            for _, seq, kind in events:
                if kind == "session_mail_available" and seq > initial and self._eligible(seq):
                    db.execute(
                        "INSERT OR IGNORE INTO host_arrivals(generation,seq,state) VALUES (?,?,'ready')",
                        (self.generation, seq),
                    )
            if events:
                db.execute(
                    "UPDATE host_subscriptions SET cursor=? WHERE subscription=? AND generation=?",
                    (events[-1][0], self.key, self.generation),
                )
            return [
                r[0]
                for r in db.execute(
                    """SELECT seq FROM host_arrivals WHERE generation=? AND state='ready'
                                                AND due<=? ORDER BY due,seq LIMIT ?""",
                    (self.generation, time.time(), limit),
                )
            ]

    def poll(self, dispatch, *, limit=25):
        """Dispatch to a local callback. Return only locally generated handles and outcomes.

        Return 'accepted' only after the host accepts a wake for the original chat.
        'not_submitted' promises that no submission took place. Any other result or
        exception is ambiguous and will not trigger another model turn.
        """
        if type(limit) is not int or not 1 <= limit <= 1000 or not callable(dispatch):
            raise ValueError("Supply a local callback and a limit between 1 and 1000")
        if self._closed:
            return []
        # CLI tools may keep their outer routing snapshot for the lifetime of a
        # pipe. Refresh aliases and main-profile grants for every polling cycle.
        with transport.routing_snapshot(self.root):
            return self._poll(dispatch, limit)

    def _poll(self, dispatch, limit):
        if not self._allowed():
            self.disable()
            return []
        results = []
        for seq in self._scan(limit):
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                with db:
                    # Reserve before touching the host, including across processes.
                    claimed = db.execute(
                        """UPDATE host_arrivals SET state='unknown',attempts=attempts+1
                                            WHERE generation=? AND seq=? AND state='ready' AND due<=?
                                            RETURNING attempts""",
                        (self.generation, seq, time.time()),
                    ).fetchone()
                if not claimed:
                    continue
                token = hashlib.sha256(f"xdev-mail-arrival-v1/{self.key}/{seq}".encode("ascii")).hexdigest()
                enabled = db.execute(
                    "SELECT 1 FROM host_subscriptions WHERE subscription=? AND generation=?",
                    (self.key, self.generation),
                ).fetchone()
                if not enabled or not self._allowed() or not self._eligible(seq):
                    status = "suppressed"
                else:
                    try:
                        status = dispatch(seq, token)
                    except Exception:
                        status = "unknown"
                    if status not in ("accepted", "not_submitted", "unknown"):
                        status = "unknown"
                retry = status == "not_submitted"
                delay = min(300, 2 ** min(claimed[0], 9)) if retry else 0
                with db:
                    db.execute(
                        "UPDATE host_arrivals SET state=?,due=? WHERE generation=? AND seq=?",
                        ("ready" if retry else status, time.time() + delay, self.generation, seq),
                    )
                results.append({"inbox_seq": seq, "notice_id": token, "status": status})
        return results

    def run(self, dispatch, stop, *, interval=1.0):
        """Run in a host-owned worker until its threading.Event is set."""
        if (
            not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or not 0.1 <= interval <= 300
        ):
            raise ValueError("interval must be between 0.1 and 300 seconds")
        while not self._closed and not stop.is_set():
            try:
                self.poll(dispatch)
            except sqlite3.OperationalError as exc:
                # Another listener or a migration may briefly hold the mailbox.
                # The cursor and the pre-dispatch claim survive another poll.
                if getattr(exc, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
            stop.wait(interval)
