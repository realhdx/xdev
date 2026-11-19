"""Transport/storage tests with private temporary mailboxes and an in-memory broker client."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.runtime import letter_attachment, messages


A = {"device": "laptop", "client": "test-chat", "session": "A"}
B = {"device": "desktop", "client": "test-chat", "session": "B"}
C = {"device": "laptop", "client": "test-chat", "session": "C"}
GROUP = {"id": "discussion-17", "members": [A, B, C]}


class LetterTransportTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source, self.destination = folder / "desktop", folder / "laptop"
        first = messages.handle(self.source, "deliver", {"id": "A1", "from": "laptop", "to": "desktop",
                                "text": "第一行\r\n原件☃", "sender_endpoint": A, "recipient_identity": B})
        self.attachment = letter_attachment.make_attachment(first["message"])
        self.envelope = {"id": "B1", "from": "desktop", "to": "laptop", "sender_endpoint": B,
                         "recipient_identity": C, "envelope_version": 2, "text": "B 的意见",
                         "letter_attachment": self.attachment}

    def test_remote_recipient_reads_original_without_source_mailbox(self):
        delivered = messages.handle(self.destination, "deliver", self.envelope)
        self.assertEqual(delivered["status"], "received")
        self.assertEqual(messages.handle(self.destination, "deliver", self.envelope)["status"], "duplicate")
        received = messages.handle(self.destination, "receipt", {"id": "B1", "reader_endpoint": C})["message"]
        self.assertEqual(received["text"], "B 的意见")
        self.assertEqual(received["sender_endpoint"], B)
        self.assertEqual(received["letter_attachment"], self.attachment)
        self.assertEqual(received["letter_attachment"]["original"]["sender_endpoint"], A)
        self.assertIsNone(messages.handle(self.destination, "wait", {"id": "A1", "timeout": 0})["message"])

    def test_same_message_id_cannot_change_or_drop_its_attachment(self):
        messages.handle(self.destination, "deliver", self.envelope)
        other = deepcopy(self.envelope)
        original = messages.handle(self.source, "wait", {"id": "A1", "timeout": 0})["message"]
        other["letter_attachment"] = letter_attachment.make_attachment(original, mode="quote", quote="原件")
        for payload in (other, {**self.envelope, "letter_attachment": None}):
            with self.assertRaisesRegex(ValueError, "different content"):
                messages.handle(self.destination, "deliver", payload)
        entry = messages.handle(self.destination, "list", {})["messages"][0]
        self.assertNotIn("letter_attachment", entry)
        self.assertEqual(entry["letter_attachment_summary"], {"sha256": self.attachment["sha256"], "mode": "forward",
                        "scope": "full", "original_id": "A1"})
        self.assertNotIn("第一行", json.dumps(entry, ensure_ascii=False))

    def test_invalid_or_unsupported_extension_is_rejected_before_insertion(self):
        damaged = deepcopy(self.attachment)
        damaged["original"]["text"] = "edited"
        for payload in ({**self.envelope, "letter_attachment": damaged},
                        {"id": "old", "from": "desktop", "to": "laptop", "text": "v1", "envelope_version": 1,
                         "letter_attachment": self.attachment},
                        {**self.envelope, "surprise_extension": True}):
            with self.assertRaises(ValueError):
                messages.handle(self.destination, "deliver", payload)
        self.assertEqual(messages.handle(self.destination, "list", {})["messages"], [])

    def test_subject_is_immutable_and_literal_search_respects_mailbox_and_limit(self):
        token = r"%_\project"
        subject = "进展 " + token
        for identifier, recipient, title in (("neighbor", A, subject), ("wildcard", C, "进展 xxproject"),
                                              ("B1", C, subject)):
            messages.handle(self.destination, "deliver", {**self.envelope, "id": identifier,
                            "recipient_identity": recipient, "subject": title})
        query = {"recipient_endpoint": C, "subject": token}
        listed = messages.handle(self.destination, "list", {**query, "limit": 1})["messages"]
        self.assertEqual([(item["id"], item["subject"]) for item in listed], [("B1", subject)])
        self.assertEqual(messages.handle(self.destination, "wait", {**query, "timeout": 0})["message"]["id"], "B1")
        self.assertEqual(messages.resolve_reply_route(self.destination, "B1")["original_subject"], subject)
        self.assertEqual(messages.handle(self.destination, "receipt", {"id": "B1", "reader_endpoint": C})["message"]["subject"], subject)
        self.assertEqual(messages.handle(self.destination, "deliver", {**self.envelope, "subject": subject})["status"], "duplicate")
        for replacement in (None, "新题目"):
            with self.assertRaisesRegex(ValueError, "different content"):
                messages.handle(self.destination, "deliver", {**self.envelope, "subject": replacement})

    def test_old_mail_preview_is_display_only_and_invalid_subjects_are_rejected(self):
        old = {"id": "old", "from": "desktop", "to": "laptop", "text": "旧信正文。第二句"}
        prior = messages.handle(self.destination, "deliver", old)["message"]
        self.assertIsNone(prior["subject"])
        self.assertEqual(prior["subject_preview"], "旧信正文")
        self.assertEqual(messages.resolve_reply_route(self.destination, "old")["original_subject"], "旧信正文")
        self.assertEqual(messages.handle(self.destination, "list", {"subject": "旧信正文"})["messages"], [])
        for title in ("", "first\nsecond", "x" * 97):
            with self.assertRaises(ValueError):
                messages.handle(self.destination, "deliver", {**self.envelope, "subject": title})
        with self.assertRaisesRegex(ValueError, "require envelope version 2"):
            messages.handle(self.destination, "deliver", {**old, "subject": "题目"})

    def test_verified_auto_subject_retries_an_untitled_row_without_rewriting_history(self):
        parent = {**self.envelope, "id": "parent", "subject": "原讨论主题"}
        messages.handle(self.destination, "deliver", parent)
        untitled = {**self.envelope, "kind": "reply", "reply_to": "parent", "text": "旧回覆。细节"}
        messages.handle(self.destination, "deliver", untitled)
        for verified in ("旧回覆", "原讨论主题"):
            replay = messages.handle(self.destination, "deliver", {**untitled, "subject": verified})
            self.assertEqual(replay["status"], "duplicate")
            self.assertIsNone(replay["message"]["subject"])
        with self.assertRaisesRegex(ValueError, "different content"):
            messages.handle(self.destination, "deliver", {**untitled, "subject": "另起标题"})
        with self.assertRaisesRegex(ValueError, "different content"):
            messages.handle(self.destination, "deliver", {**untitled, "subject": "旧回覆", "text": "改过原信"})

    def test_database_with_legacy_and_pre_extension_v2_rows_upgrades_in_place(self):
        state = self.destination / "state"
        state.mkdir(parents=True)
        with closing(sqlite3.connect(state / "messages.sqlite3")) as db, db:
            db.execute("""CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                         sender TEXT, recipient TEXT, text TEXT, reply_to TEXT, received_at TEXT, read_at TEXT,
                         kind TEXT DEFAULT 'message', reply_endpoint TEXT, recipient_endpoint TEXT,
                         envelope_version INTEGER DEFAULT 1, sender_endpoint TEXT,
                         sender_identity_source TEXT DEFAULT 'unknown', recipient_identity TEXT, read_by_endpoint TEXT)""")
            for version in (1, 2):
                db.execute("""INSERT INTO messages(id,sender,recipient,text,received_at,envelope_version)
                              VALUES (?,?,?,?,?,?)""", ("old-" + str(version), "desktop", "laptop", "old", "prior", version))
        messages.handle(self.destination, "deliver", self.envelope)
        old1, old2, new = messages.handle(self.destination, "list", {})["messages"]
        self.assertEqual([(item["id"], item["text"], item["letter_attachment"], item["group"])
                          for item in (old1, old2)], [("old-1", "old", None, None), ("old-2", "old", None, None)])
        self.assertEqual(new["letter_attachment_summary"]["sha256"], self.attachment["sha256"])

    def test_damaged_stored_attachment_is_visible_and_not_marked_as_read(self):
        messages.handle(self.destination, "deliver", self.envelope)
        with closing(sqlite3.connect(self.destination / "state/messages.sqlite3")) as db, db:
            db.execute("UPDATE messages SET letter_attachment = ? WHERE id = ?", ('{"bad":true}', "B1"))
        stored = messages.handle(self.destination, "wait", {"id": "B1", "timeout": 0})["message"]
        self.assertIsNone(stored["letter_attachment"])
        self.assertEqual(stored["letter_attachment_error"], "invalid_stored_letter_attachment")
        with self.assertRaisesRegex(ValueError, "invalid stored attachment"):
            messages.handle(self.destination, "receipt", {"id": "B1", "reader_endpoint": C})
        self.assertIsNone(messages.handle(self.destination, "list", {})["messages"][0]["read_at"])

    def test_groups_filter_before_limit_and_have_distinct_per_recipient_read_state(self):
        group = {**self.envelope, "group": GROUP}
        messages.handle(self.destination, "deliver", {**self.envelope, "id": "unrelated"})
        messages.handle(self.destination, "deliver", {**group, "id": "for-a", "recipient_identity": A})
        messages.handle(self.destination, "deliver", group)
        messages.handle(self.destination, "receipt", {"id": "for-a", "reader_endpoint": A})
        found = messages.handle(self.destination, "list", {"group_id": GROUP["id"], "recipient_endpoint": C,
                               "limit": 1, "unread_only": True})["messages"]
        self.assertEqual([item["id"] for item in found], ["B1"])
        self.assertEqual(found[0]["group"]["id"], GROUP["id"])
        self.assertEqual(found[0]["letter_attachment_summary"]["original_id"], "A1")
        self.assertEqual(messages.resolve_reply_route(self.destination, "B1")["group"], found[0]["group"])
        self.assertEqual(messages.handle(self.destination, "list", {"group_id": "other"})["messages"], [])
        with self.assertRaisesRegex(ValueError, "different content"):
            messages.handle(self.destination, "deliver", {**group, "group": {**GROUP, "id": "other"}})
        with self.assertRaises(ValueError):
            messages.handle(self.destination, "deliver", {**group, "id": "outsider", "recipient_identity": {**C, "session": "D"}})

    def test_concurrent_first_observation_preserves_one_membership_and_entire_winning_letter(self):
        options = [GROUP, {**GROUP, "members": [B, C, {**A, "session": "D"}]}]
        def receive(index):
            try:
                messages.handle(self.destination, "deliver", {**self.envelope, "id": f"first-{index}", "group": options[index]})
            except ValueError as exc:
                self.assertIn("different membership", str(exc))
                return False
            return True
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(receive, (0, 1)))
        self.assertCountEqual(outcomes, [False, True])
        winner = outcomes.index(True)
        found = messages.handle(self.destination, "list", {"group_id": GROUP["id"]})["messages"]
        self.assertEqual([item["id"] for item in found], [f"first-{winner}"])
        later = messages.handle(self.destination, "deliver", {**self.envelope, "id": "later", "group": options[winner]})
        self.assertEqual(later["status"], "received")

    def test_broker_wire_commits_both_extensions_and_hashes_content_before_acknowledging(self):
        group = {**self.envelope, "group": GROUP, "subject": "小组意见"}
        js = Mock()
        js.stream_info = AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(subjects=["aicollab.inbox.*"])))
        js.publish = AsyncMock(return_value=SimpleNamespace(stream="AICOLLAB_MESSAGES", seq=1, duplicate=False))
        connection = SimpleNamespace(jetstream=lambda: js, close=AsyncMock())
        nats = SimpleNamespace(connect=AsyncMock(return_value=connection))
        modules = {"nats": nats, "nats.errors": SimpleNamespace(Error=OSError, TimeoutError=TimeoutError),
                   "nats.js": SimpleNamespace(), "nats.js.errors": SimpleNamespace(NotFoundError=LookupError)}
        with patch.dict(sys.modules, modules), patch.object(messages.transport, "node", return_value="desktop"), \
                patch.object(messages.transport, "config", return_value={"device": "desktop", "broker": {"url": "nats://localhost"}}):
            status = asyncio.run(messages._broker(self.source, "publish", group))
            asyncio.run(messages._broker(self.source, "publish", {**self.envelope, "letter_attachment": None}))
        self.assertEqual(status["recipient_inbox"], "unconfirmed")
        addressed, plain = js.publish.await_args_list
        encoded = addressed.args[1]
        self.assertNotEqual(addressed.kwargs["headers"], plain.kwargs["headers"])
        self.assertNotIn("letter_attachment", json.loads(plain.args[1]))
        self.assertNotIn("group", json.loads(plain.args[1]))
        self.assertNotIn("subject", json.loads(plain.args[1]))
        mixed_config = {"device": "desktop", "primary_name": "研究室", "legacy_clients": True,
                        "broker": {"url": "nats://localhost"},
                        "peers": {"移动站": {"ssh": "laptop", "aliases": ["laptop"]}}}
        with patch.dict(sys.modules, modules), patch.object(messages.transport, "config", return_value=mixed_config):
            names = messages.transport.directory(self.source)
            native = {**group, **messages.address_fields(group, names.resolve)}
            asyncio.run(messages._broker(self.source, "publish", native))
            topic, mixed_wire = js.publish.await_args.args
            self.assertEqual(topic, "aicollab.inbox.laptop")
            self.assertEqual(mixed_wire, encoded)
        delivery = SimpleNamespace(data=encoded, ack_sync=AsyncMock(), term=AsyncMock())
        js.pull_subscribe = AsyncMock(return_value=SimpleNamespace(fetch=AsyncMock(return_value=[delivery])))
        with patch.object(messages.transport, "config", return_value={"device": "laptop"}):
            received = asyncio.run(messages._listen(self.destination, js, "laptop", {"timeout": 1}, TimeoutError))
        self.assertEqual(received["results"][0]["status"], "received")
        delivery.ack_sync.assert_awaited_once()
        stored = messages.handle(self.destination, "wait", {"id": "B1", "timeout": 0})["message"]
        self.assertEqual(stored["letter_attachment"], self.attachment)
        self.assertEqual(stored["group"]["id"], GROUP["id"])
        self.assertEqual(stored["subject"], "小组意见")
