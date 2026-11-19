"""Legacy inboxes and per-recipient progress survive registered device renaming."""

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.runtime import correspondence, letter_attachment, messages
from src.runtime.device_names import machine_name


class MailDeviceNames(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.configure({"device": "desktop"})
        self.a = {"device": "laptop", "client": "audit-chat", "session": "A"}
        self.b = {"device": "desktop", "client": "audit-chat", "session": "B"}
        self.group = {"id": "review-group", "members": [self.a, self.b]}
        self.envelope = {"id": "legacy-1", "from": "laptop", "to": "desktop", "sender_endpoint": self.a,
                         "recipient_identity": self.b, "text": "核验资料", "subject": "旧主题", "group": self.group}
        self.saved = self.send(self.envelope)
        self.snapshot = letter_attachment.make_attachment(self.saved["message"])
        self.new_a = {**self.a, "device": "田野机"}
        self.new_b = {**self.b, "device": "研究室"}

    def configure(self, config):
        (self.root / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def rename(self):
        self.configure({"device": "研究室", "identity_key": "desktop", "machine_name": machine_name(),
                        "aliases": ["desktop", "二号"], "peers": {"田野机": {
                            "identity_key": "laptop", "ssh": "field-ssh", "aliases": ["laptop", "移动车"]}}})

    def send(self, data):
        return messages.handle(self.root, "deliver", data)

    def test_queries_receipt_originals_and_replay_keep_sender_recipient_and_hash(self):
        self.rename()
        selectors = {"from": "移动车", "sender_endpoint": self.new_a, "recipient_endpoint": self.new_b}
        found = messages.handle(self.root, "wait", {**selectors, "timeout": 0})["message"]
        self.assertEqual(found, self.saved["message"])
        self.assertEqual(letter_attachment.make_attachment(found), self.snapshot)
        self.assertIsNone(messages.handle(self.root, "wait", {**selectors, "timeout": 0, "from": "未知"})["message"])
        def receipt(reader):
            return messages.handle(self.root, "receipt", {"id": "legacy-1", "reader_endpoint": reader})
        for neighbor in ({**self.new_b, "device": "未知"}, {**self.new_b, "session": "b"}, self.new_a):
            self.assertEqual(receipt(neighbor)["error_code"], "READER_SESSION_MISMATCH")
        read = receipt({**self.b, "device": "二号"})
        self.assertEqual(read["read_by_endpoint"]["device"], "二号")
        self.assertEqual(receipt(self.new_b)["read_by_endpoint"], read["read_by_endpoint"])
        equivalent = {**self.envelope, "from": "田野机", "to": "研究室", "sender_endpoint": self.new_a,
                      "recipient_identity": self.new_b, "group": {"id": "review-group", "members": [self.new_a,self.new_b]}}
        duplicate = self.send(equivalent)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["message"]["sender_endpoint"], self.a)
        self.assertEqual(letter_attachment.make_attachment(duplicate["message"]), self.snapshot)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.send({**equivalent, "subject": "不同主题"})
        with self.assertRaisesRegex(ValueError, "different membership"):
            changed = {**self.new_a, "session": "another"}
            self.send({**equivalent, "id": "next", "sender_endpoint": changed,
                       "group": {"id": "review-group", "members": [changed, self.new_b]}})
        follow = self.send({**equivalent, "id": "next", "letter_attachment": self.snapshot})
        self.assertEqual(follow["message"]["letter_attachment"]["sha256"], self.snapshot["sha256"])

    def test_group_retries_preserve_legacy_keys_and_fingerprint(self):
        old_group = correspondence.normalize_group(self.group)
        old_author = correspondence.address_key(self.a)
        old_plan = correspondence.stable_id("plan", old_group, "body")
        old_copy = correspondence.stable_id("copy", old_author, "batch-id", correspondence.address_key(self.b))
        correspondence.batch_state(self.root,self.a,"batch-id",old_plan,receipt={"id":old_copy,"ok":True})
        self.rename()
        new_group = {"id": old_group["id"], "members": [self.new_b, self.new_a]}
        projected = correspondence.plan_group(new_group, self.root)
        self.assertEqual(projected, old_group)
        new_plan = correspondence.stable_id("plan", projected, "body")
        new_copy = correspondence.stable_id("copy", correspondence.address_key(self.new_a,root=self.root),"batch-id",
                                           correspondence.address_key(self.new_b,root=self.root))
        self.assertEqual(new_copy, old_copy)
        self.assertIn(new_copy, correspondence.batch_state(self.root,self.new_a,"batch-id",new_plan))
        with self.assertRaisesRegex(ValueError, "different message"):
            correspondence.batch_state(self.root,self.new_a,"batch-id","different")
        with self.assertRaisesRegex(ValueError, "Duplicate discussion"):
            correspondence.normalize_group({"id":"dupe","members":[self.a,self.new_a]},root=self.root)

    def test_broker_uses_old_durable_cursor_and_commits_legacy_subject(self):
        self.rename()
        received = SimpleNamespace(data=json.dumps({**self.envelope, "id":"queued-before-upgrade"}).encode("utf-8"),
                                   ack_sync=AsyncMock(), term=AsyncMock())
        async def subscribe(topic, *, durable, stream):
            self.assertEqual(topic.removeprefix("aicollab.inbox."),durable.removeprefix("aicollab-"))
            async def fetch(*,batch,timeout):
                if durable=="aicollab-desktop":
                    return [received]
                raise TimeoutError()
            return SimpleNamespace(fetch=fetch)
        js=SimpleNamespace(pull_subscribe=AsyncMock(side_effect=subscribe))
        result=asyncio.run(messages._listen(self.root,js,"研究室",{"timeout":2},TimeoutError))
        self.assertEqual(result["results"][0]["id"],"queued-before-upgrade")
        received.ack_sync.assert_awaited_once()
        self.assertIn("aicollab.inbox.研究室",[c.args[0] for c in js.pull_subscribe.await_args_list])

    def test_saved_identity_survives_custom_rename_and_later_role_alias(self):
        initial = {"device": "Field-One", "identity_key": "field-one", "machine_name": machine_name(), "aliases": [],
                   "peers": {"Lab-Arc": {"identity_key": "lab-arc", "ssh": "via-lab"}}}
        self.configure(initial)
        sender = {**self.b, "device": "Field-One"}
        peer = {**self.a, "device": "Lab-Arc"}
        original = {"id": "alpha", "members": [sender, peer]}
        def receipt_keys(actor, group):
            plan = correspondence.plan_group(group, self.root)
            return (correspondence.address_key(actor, root=self.root), correspondence.stable_id("plan", plan))
        first_key, first_plan = receipt_keys(sender, original)
        copy = correspondence.stable_id("copy", first_key, "continue", correspondence.address_key(peer, root=self.root))
        correspondence.batch_state(self.root,sender,"continue",first_plan,receipt={"id":copy,"ok":True})
        for config in ({**initial, "aliases": ["laptop"]},
                       {"device":"移动工作站","identity_key":"field-one","aliases":["Field-One","laptop"],
                        "machine_name":machine_name(),"peers":{"台面":{"identity_key":"lab-arc","ssh":"via-lab",
                                                              "aliases":["Lab-Arc","desktop"]}}}):
            self.configure(config)
            new_sender = {**sender, "device": config["device"]}
            new_peer = {**peer, "device": next(iter(config["peers"]))}
            new_group = {"id":"alpha","members":[new_peer, new_sender]}
            key, plan = receipt_keys(new_sender, new_group)
            self.assertEqual((key,plan),(first_key,first_plan))
            self.assertIn(copy, correspondence.batch_state(self.root,new_sender,"continue",plan))

