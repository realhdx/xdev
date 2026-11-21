"""Receiver-side mail lifecycle. All callers and races run on private temporary inboxes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.runtime import mail_lifecycle as lifecycle, messages, session_relay


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "config.json").write_text(json.dumps({"device":"beta","peers":{"alpha":{}}}))
        self.author = {"device":"alpha","client":"writer","session":"a"}
        self.reader = {"device":"beta","client":"reader","session":"b"}
        self.envelope = dict(id="first", envelope_version=2, text="secret draft", subject="private subject",
                             sender_endpoint=self.author, recipient_identity=self.reader,
                             communication_intent="request_action")
        self.envelope.update({"from":"alpha","to":"beta"})
        self.deliver()

    def deliver(self, **fields):
        return messages.handle(self.root, "deliver", {**self.envelope, **fields})

    def control(self, op, mid="first", **changes):
        actor = self.reader if op == "progress" else self.author
        data = {"op":op,"id":mid,"actor":actor, **changes}
        return lifecycle.apply(self.root, data, authenticated_peer="alpha" if op != "progress" else None,
                               expected_target=self.reader)

    def read(self, mid="first"):
        return messages._receipt(self.root, {"id":mid,"reader_endpoint":self.reader})

    def test_unread_recall_is_durable_and_never_returns_or_requeues_body(self):
        result = self.control("recall", operation_id="call1")
        self.assertEqual(result["status"], "recalled")
        self.assertIsNone(result["read_at"])
        self.assertEqual(self.control("recall", operation_id="call1"), result)
        self.assertEqual(self.control("status")["withdrawn_at"], result["withdrawn_at"])
        self.assertEqual(self.read()["status"], "recalled")
        self.assertEqual(messages.resolve_reply_route(self.root, "first")["status"], "recalled")
        for mode in ({"unread_only":True}, {"substantive_only":True}):
            self.assertEqual(messages._query(self.root, mode, 10)["messages"], [])
        self.assertEqual(messages._query(self.root, {}, 10, notices=True)["messages"], [])
        for view in (self.deliver(), messages._query(self.root, {}, 10), result, self.read()):
            self.assertNotIn("secret draft", json.dumps(view))
            self.assertNotIn("private subject", json.dumps(view))
        with self.assertRaisesRegex(ValueError, "different content"):
            self.deliver(text="incompatible")
        with messages._database(self.root) as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE messages SET read_at='now' WHERE id='first'")
        class Settings:
            root = self.root
        with patch.object(session_relay, "_notify_once", side_effect=AssertionError):
            event = session_relay._receive(Settings(), "alpha", self.envelope, {"id":"old-wake"})
        self.assertEqual(event["session_notification"]["status"], "recalled")

    def test_only_the_enrolled_author_or_recipient_can_inspect_or_change_this_letter(self):
        data = {"op":"recall","id":"first","actor":self.author}
        for peer, actor, target in ((None,self.author,self.reader),("beta",self.reader,self.reader),("",self.reader,self.reader),
                                   ("alpha",self.author,None),
                                   ("alpha",{**self.author,"session":"neighbor"},self.reader),
                                   ("alpha",self.author,{**self.reader,"session":"neighbor"})):
            with self.subTest(peer=peer, actor=actor, target=target):
                self.assertEqual(lifecycle.apply(self.root, {**data,"actor":actor},
                    authenticated_peer=peer, expected_target=target)["status"], "unavailable")
        with self.assertRaises(ValueError):
            lifecycle.apply(self.root, {**data,"authenticated_peer":"alpha"})
        self.assertEqual(self.control("status")["version"], 0)
        self.assertEqual(self.control("recall",mid="no-such-original")["status"],"unavailable")

    def test_same_device_and_wrong_native_profile(self):
        sender = {"device":"beta","client":"local-app","session":"sender"}
        reader = {"device":"beta","client":"codex","session":"00000000-0000-4000-9000-000000000078",
                  "codex_home":r"C:\private\myp"}
        self.deliver(id="same", **{"from":"beta","sender_endpoint":sender,"recipient_identity":reader})
        request = {"id":"same","op":"recall","actor":sender}
        self.assertEqual(lifecycle.apply(self.root, request, expected_target={**reader,"codex_home":r"C:\other"})["status"],
                         "unavailable")
        self.assertEqual(lifecycle.apply(self.root, {**request,"actor":{**sender,"session":"other"}})["status"],
                         "unavailable")
        self.assertEqual(lifecycle.apply(self.root, request, expected_target=reader)["status"], "recalled")

    def test_replace_preserves_both_addresses_and_successor(self):
        self.deliver(id="wrong-peer", recipient_identity={**self.reader,"session":"other"})
        self.assertEqual(self.control("replace",replacement_id="wrong-peer")["status"], "replacement_unavailable")
        self.assertEqual(self.control("replace",replacement_id="unknown")["status"], "replacement_unavailable")
        self.deliver(id="second",text="revised body")
        self.assertEqual(self.control("correct",replacement_id="second")["status"], "unread_use_replace")
        self.assertEqual(self.control("replace", replacement_id="second",expected_version=2)["status"], "version_conflict")
        updated = self.control("replace", replacement_id="second",expected_version=0)
        self.assertEqual((updated["status"],updated["replacement_id"]),("replaced","second"))
        self.assertEqual(self.read()["replacement_id"], "second")
        self.assertEqual(self.control("status","second")["revision_of"]["original"], "first")
        self.assertEqual(self.read("second")["message"]["text"], "revised body")

    def test_read_then_correct_is_not_a_recall(self):
        self.read()
        self.deliver(id="second", text="correction")
        self.assertEqual(self.control("recall")["status"], "already_read")
        self.assertEqual(self.control("replace",replacement_id="second")["status"], "already_read")
        update = self.control("correct",replacement_id="second",operation_id="ver2")
        self.assertEqual(update["status"],"corrected")
        self.assertIsNone(update["withdrawn_at"])
        self.assertEqual(self.read()["message"]["text"],"secret draft")
        self.assertEqual(self.control("status","second")["revision_of"]["operation"],"correct")
        self.assertEqual(self.control("correct",replacement_id="first",operation_id="ver2")["status"],"operation_conflict")

    def test_recipient_records_work_and_reply_independently(self):
        self.assertEqual(self.control("progress",work_state="pending")["status"],"read_required")
        self.read()
        self.assertEqual(self.control("status")["communication_intent"], "request_action")
        data = {"op":"progress","id":"first","actor":self.author,"work_state":"completed"}
        self.assertEqual(lifecycle.apply(self.root,data,authenticated_peer="alpha")["status"],"unavailable")
        report = self.control("progress",work_state="blocked",reason="waiting for review")
        self.assertTrue(report["work_at"])
        self.assertIsNone(report["reply_intent_at"])
        explicit = self.control("progress", reply_intent="no_reply")
        self.assertEqual(explicit["work_at"], report["work_at"])
        self.assertTrue(explicit["reply_intent_at"])
        self.assertEqual(explicit["reason"], "waiting for review")
        end = self.control("progress",work_state="completed")
        self.assertIsNone(end["reason"])
        self.assertEqual(end["reply_intent"],"no_reply")
        self.assertEqual(self.control("progress",work_state="in_progress")["status"],"completed_work")
        self.assertEqual(self.control("progress",work_state="completed")["version"], end["version"])

    def test_unlabeled_delivery_and_intent_conflict(self):
        unlabeled = dict(self.envelope)
        unlabeled.pop("communication_intent")
        self.assertEqual(messages._deliver(self.root,unlabeled)["status"],"duplicate")
        self.assertEqual(messages._deliver(self.root,{**unlabeled,"id":"neutral"})["message"]["communication_intent"],None)
        self.assertIsNone(self.deliver(id="neutral")["message"]["communication_intent"])
        with self.assertRaisesRegex(ValueError, "different communication intent"):
            self.deliver(communication_intent="inform")
        self.assertEqual(messages._query(self.root,{},1,notices=True)["messages"][0]["communication_intent"],
                         "request_action")
        with self.assertRaises(ValueError):
            self.deliver(id="cmd",communication_intent="execute")

    def test_read_races_recall_with_exactly_one_winner(self):
        for turn in range(6):
            mid = "race" + str(turn)
            self.deliver(id=mid)
            gate = threading.Barrier(2)
            def read():
                gate.wait()
                return self.read(mid)["status"]
            def recall():
                gate.wait()
                return self.control("recall", mid)["status"]
            with ThreadPoolExecutor(2) as pool:
                a, b = pool.submit(read), pool.submit(recall)
                self.assertIn((a.result(),b.result()), (("read","already_read"),("recalled","recalled")))
