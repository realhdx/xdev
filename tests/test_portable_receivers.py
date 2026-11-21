"""Exercise a host-supplied receiver through the same path as an addressed wake."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.adapters import portable, sessions
from src.adapters.delivery_contract import acceptance
from src.runtime import messages


HOST = """
import json, pathlib, sys, time
notice = json.loads(sys.stdin.buffer.read().decode('utf-8'))
log, response = pathlib.Path(sys.argv[1]), sys.argv[2]
with log.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(notice) + '\\n')
ack = {k: notice[k] for k in ('protocol', 'client', 'session', 'message_id', 'notice_id')}
ack.update(status='accepted', placement='next_turn' if notice['mode'] == 'queue' else 'current_turn')
if response == 'mismatch':
    ack['session'] += 'X'
elif response == 'offline':
    ack['status'] = 'requires_activation'
elif response == 'late':
    time.sleep(15)
print(json.dumps(ack))
"""


class PortableReceivers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "host.ndjson"
        self.script = self.root / "host.py"
        self.script.write_text(HOST, encoding="utf-8")
        self.sender = {"device": "lab-peer", "client": "other-app", "session": "id#other"}
        self.target = {"device": "work-pc", "client": "sample-host", "session": "项目/Run:42"}
        self.configure()

    def configure(self, response="ok"):
        argv = [sys.executable, str(self.script), str(self.log), response]
        bindings = {self.target["client"]: {self.target["session"]: {"argv": argv}}}
        (self.root / "config.json").write_text(json.dumps({
            "device": self.target["device"], "session_receivers": bindings,
            "peers": {"lab-peer": {"ssh": "lab-peer"}}}), encoding="utf-8")

    def deliver(self, identifier, *, target=None, **fields):
        endpoint = target or self.target
        messages.handle(self.root, "deliver", {"id": identifier, "text": "full private content",
                        "subject": "A short subject", "from": self.sender["device"], "to": endpoint["device"],
                        "sender_endpoint": self.sender, "recipient_identity": endpoint,
                        "recipient_endpoint": endpoint, **fields})

    def notify(self, identifier, *, target=None, **options):
        endpoint = target or self.target
        return sessions.submit(self.root, identifier, endpoint["session"], endpoint["client"], **options)

    def test_notice_is_short_ack_is_scoped_and_read_is_separate(self):
        self.deliver("one")
        sent = self.notify("one", steer=True)
        notice = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(set(notice), {"protocol", "device", "client", "session", "message_id",
                                      "notice_id", "mode", "sender", "subject"})
        self.assertEqual((notice["sender"], notice["subject"], notice["mode"]),
                         (self.sender, "A short subject", "send_now"))
        self.assertEqual(acceptance(sent, "send_now", sessions.descriptor("sample-host"))["scope"], "current_turn")
        self.assertIsNone(messages.handle(self.root, "wait", {"id": "one", "timeout": 0})["message"]["read_at"])
        read = messages.handle(self.root, "receipt", {"id": "one", "reader_endpoint": self.target})
        self.assertEqual(read["read_by_endpoint"], self.target)
        self.assertTrue(self.notify("one")["duplicate"])
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)

    def test_host_integration_handles_new_chats_without_changing_xdev_configuration(self):
        from uuid import uuid4
        config_path = self.root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        argv = config.pop("session_receivers")[self.target["client"]][self.target["session"]]["argv"]
        config["session_hosts"] = {self.target["client"]: {"argv": argv}}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        baseline = config_path.read_bytes()
        chat = {**self.target, "session": str(uuid4())}
        self.assertTrue(sessions.receiver_bound(self.root, chat["client"], chat["session"]))
        self.assertEqual(sessions.capabilities(self.root)["portable_receiver_apps"], [chat["client"]])
        self.deliver("first-contact", target=chat)
        self.assertEqual(self.notify("first-contact", target=chat)["status"], "admitted")
        self.assertTrue(self.notify("first-contact", target=chat)["duplicate"])
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8"))["session"], chat["session"])
        self.assertEqual(config_path.read_bytes(), baseline)

    def test_queue_and_config_discovery(self):
        caps = sessions.capabilities(self.root)
        self.assertEqual(caps["portable_receivers"], {"sample-host": ["项目/Run:42"]})
        self.assertNotIn(str(self.script), json.dumps(caps))
        self.deliver("queued")
        result = self.notify("queued", force_queue=True)
        self.assertEqual(acceptance(result, "queue", sessions.descriptor("sample-host"))["scope"], "next_turn")
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8"))["mode"], "queue")

    def test_address_is_opaque_and_executable_is_local_configuration(self):
        client = self.target["client"]
        self.assertEqual(sessions.canonical_session(client, self.target["session"]), self.target["session"])
        for invalid in ("", " \t", "first\nsecond", "s" * 513):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError):
                    sessions.canonical_session(client, invalid)
        for invalid in ("Sample", "../../sample", "a" * 65):
            with self.assertRaises(ValueError):
                sessions.client_name(invalid)
        self.assertTrue(sessions.receiver_bound(self.root, client, self.target["session"]))
        self.assertFalse(sessions.receiver_bound(self.root, client, self.target["session"].swapcase()))
        settings = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        settings["session_receivers"][client][self.target["session"]]["argv"] = ["relative-launcher"]
        (self.root / "config.json").write_text(json.dumps(settings), encoding="utf-8")
        with self.assertRaises(ValueError):
            sessions.receiver_bound(self.root, client, self.target["session"])

    def test_exact_binding_intent_and_inert_notices(self):
        adjacent = {**self.target, "session": "项目/Run:43"}
        self.deliver("wrong", target=adjacent)
        self.assertEqual(self.notify("wrong")["status"], "target_mismatch")
        self.assertEqual(self.notify("wrong", target=adjacent)["status"], "requires_receiver_binding")
        self.assertEqual(self.notify("missing")["status"], "target_mismatch")
        self.deliver("receipt", kind="receipt", reply_to="wrong")
        self.assertEqual(self.notify("receipt")["status"], "receipt_suppressed")
        messages.handle(self.root, "deliver", {"id": "legacy", "text": "txt", "from": "lab-peer", "to": "work-pc"})
        self.assertEqual(self.notify("legacy")["status"], "target_mismatch")
        with self.assertRaises(ValueError):
            sessions.notify(self.root, {"id": "wrong", "client": "sample-host", "session": "项目/Run:42",
                                        "argv": [sys.executable]})
        self.assertFalse(self.log.exists())

    def test_ambiguous_ack_and_timeout_cannot_repeat(self):
        for case in ("mismatch", "late"):
            with self.subTest(case=case):
                self.configure(case)
                self.deliver(case)
                with patch.object(portable, "TIMEOUT", 1):
                    self.assertEqual(self.notify(case)["status"], "unknown")
                self.configure()
                retry = self.notify(case)
                self.assertTrue(retry["duplicate"])
                self.assertEqual(retry["status"], "unknown")
        records = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(r)["message_id"] for r in records], ["mismatch", "late"])

    def test_explicit_offline_allows_later_activation(self):
        self.configure("offline")
        self.deliver("later")
        self.assertEqual(self.notify("later")["status"], "requires_activation")
        self.configure()
        self.assertEqual(self.notify("later")["status"], "admitted")

    def test_concurrent_notifications_invoke_host_only_once(self):
        self.deliver("racing")
        entered = threading.Event()
        release = threading.Event()
        exchange = portable._exchange

        def held(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return exchange(*args)

        with patch.object(portable, "_exchange", side_effect=held) as helper, ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.notify, "racing")
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.notify, "racing").result(timeout=3)
                self.assertEqual((second["status"], second["duplicate"]), ("unknown", True))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)["status"], "admitted")
            self.assertEqual(helper.call_count, 1)
        self.assertEqual(self.notify("racing")["status"], "already_admitted")
