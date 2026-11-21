"""Offline regression of the host-owned idle notification boundary."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.adapters.mail_arrival import Subscription
from src.runtime import github_mail, messages, outbox


class MailArrival(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key, token = self.root / "pair.key", self.root / "token"
        key.write_text("9f" * 32)
        token.write_text("synthetic")
        self.config = {
            "device": "beta",
            "peers": {"alpha": {"mail_transport": "github"}},
            "github_mailbox": {
                "repository": "example/mail",
                "repository_id": 51,
                "release_id": 2,
                "token_file": str(token),
                "peers": {"alpha": {"receiver_apps": ["reviewer", "codex"], "key_file": str(key)}},
            },
        }
        self.save()
        self.peer = {"device": "alpha", "client": "reviewer", "session": "author"}
        self.target = {"device": "beta", "client": "reviewer", "session": "review-a"}
        self.dispatch = Mock(return_value="accepted")

    def save(self):
        (self.root / "config.json").write_text(json.dumps(self.config))

    def sub(self, **kwargs):
        return Subscription(
            self.root, **{"peer": "alpha", "client": "reviewer", "session": "review-a", **kwargs}
        )

    def envelope(self, identifier, **extra):
        return {
            "id": identifier,
            "envelope_version": 2,
            "kind": "message",
            "from": "alpha",
            "to": "beta",
            "sender_endpoint": self.peer,
            "reply_endpoint": self.peer,
            "recipient_identity": self.target,
            "recipient_endpoint": self.target,
            "text": "untrusted body",
            "subject": "untrusted subject",
            **extra,
        }

    def accept(self, identifier, **extra):
        return github_mail._accept(
            github_mail.Settings(self.root), "alpha", self.envelope(identifier, **extra), None, False
        )["seq"]

    def test_idle_background_dispatch_only_local_fields(self):
        sub = self.sub()
        stop = threading.Event()
        calls = []

        def dispatch(seq, token):
            calls.append((seq, token))
            stop.set()
            return "accepted"

        with ThreadPoolExecutor() as pool:
            worker = pool.submit(sub.run, dispatch, stop, interval=0.1)
            try:
                seq = self.accept("contains words\nwith instructions --flag")
                worker.result(timeout=5)
            finally:
                stop.set()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], seq)
        self.assertRegex(calls[0][1], r"^[0-9a-f]{64}$")
        self.assertEqual(self.sub().poll(self.dispatch), [])
        self.dispatch.assert_not_called()

    def test_install_excludes_existing_mail_even_if_event_is_recovered_after_registration(self):
        self.accept("complete")
        messages.handle(self.root, "deliver", self.envelope("partial"))
        sub = self.sub()
        self.accept("partial")
        self.accept("complete")
        self.assertEqual(sub.poll(self.dispatch), [])
        self.dispatch.assert_not_called()

    def test_peer_and_exact_chat_read_and_age_filter(self):
        sub = self.sub()
        neighbor = {**self.target, "session": "adjacent"}
        self.accept("neighbor", recipient_endpoint=neighbor, recipient_identity=neighbor)
        self.accept("taken")
        messages._receipt(self.root, {"id": "taken", "reader_endpoint": self.target})
        self.accept("stale")
        with closing(sqlite3.connect(sub.inbox)) as inbox, inbox:
            old = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
            inbox.execute("UPDATE messages SET received_at=? WHERE id='stale'", (old,))
        q = {**self.target, "delivery": "queue"}
        self.accept("wait-until-next", recipient_endpoint=q)
        good = self.accept("mine")
        self.assertEqual([r["inbox_seq"] for r in self.sub().poll(self.dispatch)], [good])

    def test_receipt_and_waiting_tool_return_are_passive_normal_correlated_reply_wakes(self):
        sub = self.sub()
        sent = {
            "envelope_version": 2,
            "id": "waiting",
            "from": "beta",
            "to": "alpha",
            "kind": "message",
            "recipient_identity": self.peer,
            "recipient_endpoint": self.peer,
            "sender_endpoint": self.target,
            "text": "example",
            "reply_endpoint": {"device": "beta", "channel": "tool_result", "request_id": "waiting"},
        }
        outbox.remember(self.root, sent, delivery_report=True)
        data = {
            "schema": github_mail.mail.REPORT_SCHEMA,
            "message_id": "waiting",
            "inbox_status": "received",
            "session_status": "not_requested",
            "placement": "unknown",
        }
        receipt = {
            "envelope_version": 2,
            "id": github_mail.mail.report_id("beta", "waiting"),
            "kind": "receipt",
            "from": "alpha",
            "to": "beta",
            "reply_to": "waiting",
            "recipient_identity": self.target,
            "text": json.dumps(data),
        }
        github_mail._accept(github_mail.Settings(self.root), "alpha", receipt, None, False)
        self.accept("tool-result", kind="reply", reply_to="waiting", recipient_endpoint=None)
        with self.assertRaisesRegex(ValueError, "Unauthorized"):
            self.accept("unsolicited", kind="reply", reply_to="nobody")
        outbox.remember(self.root, {**sent, "id": "ordinary", "reply_endpoint": self.target})
        good = self.accept("real-reply", kind="reply", reply_to="ordinary")
        self.assertEqual([r["inbox_seq"] for r in sub.poll(self.dispatch)], [good])
        self.dispatch.assert_called_once()

    def test_unrelated_peer_cannot_notify_target_chat(self):
        grant = self.config["github_mailbox"]["peers"]["alpha"]
        otherkey = self.root / "third.key"
        otherkey.write_text("af" * 32)
        self.config["github_mailbox"]["peers"]["gamma"] = {**grant, "key_file": str(otherkey)}
        self.config["peers"]["gamma"] = {"mail_transport": "github"}
        self.save()
        sub = self.sub()
        origin = {**self.peer, "device": "gamma"}
        letter = self.envelope(
            "other-peer", **{"from": "gamma", "sender_endpoint": origin, "reply_endpoint": origin}
        )
        github_mail._accept(github_mail.Settings(self.root), "gamma", letter, None, False)
        self.assertEqual(sub.poll(self.dispatch), [])

    def test_exact_profile_independent_from_default_peer_grant(self):
        chat = "2bc8bb29-f68b-41d9-8f52-8ec2b1633843"
        default = {"device": "beta", "client": "codex", "session": chat}
        custom = {**default, "codex_home": str(self.root / "profile")}
        self.config["github_mailbox"]["peers"]["alpha"]["receivers"] = [
            {"client": "codex", "session": chat, "codex_home": custom["codex_home"]}
        ]
        self.save()
        first = self.sub(client="codex", session=chat)
        other = self.sub(client="codex", session=chat, profile=custom["codex_home"])
        a = self.accept("default", recipient_identity=default, recipient_endpoint=default)
        b = self.accept("custom", recipient_identity=custom, recipient_endpoint=custom)
        self.assertEqual([r["inbox_seq"] for r in first.poll(self.dispatch)], [a])
        self.assertEqual([r["inbox_seq"] for r in other.poll(self.dispatch)], [b])

    def test_duplicates_and_parallel_consumers_submit_once(self):
        one, two = self.sub(), self.sub()
        seq = self.accept("again")
        self.accept("again")
        barrier = threading.Barrier(2)

        def check(sub):
            barrier.wait(timeout=5)
            return sub.poll(self.dispatch)

        with ThreadPoolExecutor() as pool:
            rows = list(pool.map(check, (one, two)))
        self.assertEqual([r["inbox_seq"] for group in rows for r in group], [seq])
        self.dispatch.assert_called_once()

    def test_known_not_submitted_is_retried_unknown_is_not(self):
        sub = self.sub()
        self.accept("test")
        self.dispatch.side_effect = ["not_submitted", RuntimeError("ack lost")]
        now = datetime.now(timezone.utc).timestamp()
        with patch("src.adapters.mail_arrival.time.time", return_value=now):
            first = sub.poll(self.dispatch)[0]
            self.assertEqual(first["status"], "not_submitted")
            self.assertEqual(self.sub().poll(self.dispatch), [])
        with patch("src.adapters.mail_arrival.time.time", return_value=now + 5):
            second = self.sub().poll(self.dispatch)[0]
            self.assertEqual(second["status"], "unknown")
            self.assertEqual(second["notice_id"], first["notice_id"])
            self.assertEqual(self.sub().poll(self.dispatch), [])
        self.assertEqual(self.dispatch.call_count, 2)

    def test_busy_local_mailbox_does_not_terminate_the_host_subscriber(self):
        sub = self.sub()
        stop = threading.Event()
        busy = sqlite3.OperationalError("database is busy")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        poll = sub.poll
        self.accept("busy")

        def admitted(seq, token):
            stop.set()
            return "accepted"

        def twice(dispatch):
            if not stop.is_set() and guarded.call_count == 1:
                raise busy
            return poll(dispatch)

        with patch.object(sub, "poll", side_effect=twice) as guarded:
            sub.run(admitted, stop, interval=0.1)
        self.assertEqual(guarded.call_count, 2)

    def test_crash_after_local_claim_is_unknown_on_reopen(self):
        sub = self.sub()
        self.accept("id")
        self.dispatch.side_effect = SystemExit()
        with self.assertRaises(SystemExit):
            sub.poll(self.dispatch)
        self.assertEqual(self.sub().poll(self.dispatch), [])
        self.dispatch.assert_called_once()

    def test_failed_host_does_not_delay_new_mail_for_same_or_neighbor_chat(self):
        sub = self.sub()
        other = self.sub(session="review-b")
        neighbor = {**self.target, "session": "review-b"}
        failed = self.accept("not-launched")
        self.dispatch.return_value = "not_submitted"
        self.assertEqual(sub.poll(self.dispatch, limit=1)[0]["inbox_seq"], failed)
        self.dispatch.return_value = "accepted"
        fresh = self.accept("unrelated-fresh")
        theirs = self.accept("their-fresh", recipient_endpoint=neighbor, recipient_identity=neighbor)
        self.assertEqual(sub.poll(self.dispatch, limit=1)[0]["inbox_seq"], fresh)
        self.assertEqual([r["inbox_seq"] for r in other.poll(self.dispatch)], [theirs])

    def test_reopen_recovers_pending_mail_and_excludes_read_or_stale_before_retry(self):
        self.sub()
        recent = self.accept("arrived-when-host-offline")
        self.dispatch.return_value = "not_submitted"
        self.assertEqual(self.sub().poll(self.dispatch)[0]["inbox_seq"], recent)
        messages._receipt(self.root, {"id": "arrived-when-host-offline", "reader_endpoint": self.target})
        with patch(
            "src.adapters.mail_arrival.time.time", return_value=datetime.now(timezone.utc).timestamp() + 10
        ):
            self.assertEqual(self.sub().poll(self.dispatch)[0]["status"], "suppressed")
        self.dispatch.assert_called_once()

    def test_revocation_and_new_opt_in_exclude_old_mail(self):
        old = self.sub()
        self.accept("before")
        self.config["github_mailbox"]["peers"]["alpha"]["receiver_apps"] = []
        self.save()
        self.assertEqual(old.poll(self.dispatch), [])
        with self.assertRaises(ValueError):
            self.sub()
        self.config["github_mailbox"]["peers"]["alpha"]["receiver_apps"] = ["reviewer"]
        self.save()
        new = self.sub()
        fresh = self.accept("after")
        self.assertEqual(old.poll(self.dispatch), [])
        self.assertEqual([r["inbox_seq"] for r in new.poll(self.dispatch)], [fresh])
        new.disable()
        self.assertEqual(new.poll(self.dispatch), [])
