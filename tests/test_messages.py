import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtime import messages


class MailboxTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="cross-device-message-test-")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.payload = {"id": "m-1", "from": "laptop", "to": "desktop", "text": "你好 桌面"}

    def call(self, action, **payload):
        return messages.handle(self.root, action, payload)

    def test_durable_deliver_deduplicate_and_reject_conflict(self):
        first = self.call("deliver", **self.payload)
        repeated = self.call("deliver", **{**self.payload, "reply_to": None})
        self.assertEqual(first["status"], "received")
        self.assertEqual(repeated["status"], "duplicate")
        self.assertEqual(first["message"], repeated["message"])
        with self.assertRaisesRegex(ValueError, "different content"):
            self.call("deliver", **{**self.payload, "text": "modified"})
        records = self.call("list")
        self.assertEqual(records["messages"], [first["message"]])
        self.assertEqual(records["cursor"], first["seq"])

    def test_reply_filters_cursor_and_explicit_receipt(self):
        first = self.call("deliver", **self.payload)
        reply = self.call("deliver", id="r-1", **{"from": "desktop", "to": "laptop",
                          "text": "收到", "reply_to": "m-1"})
        filtered = self.call("list", after=first["seq"], reply_to="m-1", **{"from": "desktop"})
        self.assertEqual(filtered["messages"], [reply["message"]])
        self.assertEqual(filtered["cursor"], reply["seq"])
        self.assertEqual(filtered["unread_ids"], ["r-1"])
        self.assertIsNone(filtered["messages"][0]["read_at"])
        receipt = self.call("receipt", id="r-1")
        self.assertEqual(self.call("receipt", id="r-1"), receipt)
        self.assertEqual(self.call("list", id="r-1")["messages"][0]["read_at"], receipt["read_at"])
        self.assertEqual(self.call("wait", id="missing", timeout=0),
                         {"status": "timeout", "message": None, "cursor": 0})

    def test_unread_filter_and_acknowledgement_hints_preserve_receipt_state(self):
        self.call("deliver", **self.payload)
        page = self.call("list", unread_only=True)
        self.assertEqual(page["unread_ids"], ["m-1"])
        self.assertIn("--reply-to", page["ack_first_protocol"])
        self.assertIsNone(page["messages"][0]["read_at"])
        waited = self.call("wait", id="m-1", unread_only=True, timeout=0)
        self.assertEqual(waited["unread_ids"], ["m-1"])
        self.call("receipt", id="m-1")
        self.assertEqual(self.call("list", unread_only=True), {"messages": [], "cursor": 0})
        self.assertNotIn("ack_first_protocol", self.call("list"))
        with self.assertRaisesRegex(ValueError, "unread_only must"):
            self.call("list", unread_only="yes")

    def test_cli_list_and_inbox_aliases_keep_filters(self):
        from src.cli.main import _parser

        for name in ("list", "inbox"):
            parsed = _parser().parse_args([
                "msg", name, "--host", "desktop", "--reply-to", "m-1", "--unread-only", "--limit", "5",
            ])
            self.assertEqual(parsed.host, "desktop")
            self.assertEqual(parsed.reply_to, "m-1")
            self.assertTrue(parsed.unread_only)
            self.assertEqual(parsed.limit, 5)

    def test_same_device_chats_filter_both_participants_before_limit_and_wait(self):
        a, b, c = ({"device": "desktop", "client": "example-web", "session": value}
                   for value in ("project.A", "project.B", "project.C"))
        def send(identifier, sender, recipient):
            return self.call("deliver", **{**self.payload, "id": identifier, "from": "desktop",
                             "sender_endpoint": sender, "recipient_identity": recipient})
        send("other", a, c)
        send("to-b", c, b)
        target = send("a-to-b", a, b)
        selection = {"sender_endpoint": a, "recipient_endpoint": b}
        self.assertEqual(self.call("list", limit=1, **selection)["messages"], [target["message"]])
        self.assertEqual(self.call("wait", timeout=0, **selection)["message"], target["message"])
        self.assertEqual(self.call("list", recipient_endpoint={**b, "session": "project.b"})["messages"], [])
        self.assertEqual(self.call("list", sender_endpoint=b)["messages"], [])

    def test_session_receipt_rejects_neighbor_and_preserves_first_reader(self):
        target = {"device": "desktop", "client": "example-web", "session": "b"}
        neighbor = {**target, "session": "c"}
        self.call("deliver", **self.payload, recipient_identity=target)
        self.assertEqual(self.call("receipt", id="m-1")["error_code"], "READER_SESSION_REQUIRED")
        self.assertEqual(self.call("receipt", id="m-1", reader_endpoint=neighbor)["error_code"],
                         "READER_SESSION_MISMATCH")
        self.assertIsNone(self.call("list", id="m-1")["messages"][0]["read_at"])
        receipt = self.call("receipt", id="m-1", reader_endpoint=target)
        self.assertEqual(receipt["read_by_endpoint"], target)
        self.assertEqual(receipt["read_scope"], "session")
        self.assertEqual(self.call("receipt", id="m-1", reader_endpoint=target), receipt)
        self.assertFalse(self.call("receipt", id="m-1", reader_endpoint=neighbor)["ok"])
        self.assertEqual(self.call("list", unread_only=True, recipient_endpoint=target)["messages"], [])
        self.call("deliver", **{**self.payload, "id": "legacy"})
        device_read = self.call("receipt", id="legacy")
        self.assertEqual(device_read["read_scope"], "device")
        self.assertEqual(self.call("receipt", id="legacy", reader_endpoint=target), device_read)

    def test_profile_scoped_recipient_requires_matching_profile_to_acknowledge(self):
        chat = {"device": "desktop", "client": "codex", "session": "00000000-0000-0000-0000-000000000001"}
        one = {**chat, "codex_home": "C:/one"}
        two = {**chat, "codex_home": "C:/two"}
        self.call("deliver", **self.payload, recipient_endpoint={**one, "delivery": "queue"}, envelope_version=2)
        self.assertEqual(self.call("list", recipient_endpoint=two)["messages"], [])
        self.assertEqual(self.call("list", recipient_endpoint=one)["messages"][0]["id"], "m-1")
        for reader in (chat, two):
            self.assertFalse(self.call("receipt", id="m-1", reader_endpoint=reader)["ok"])
        self.assertEqual(self.call("receipt", id="m-1", reader_endpoint=one)["read_by_endpoint"], one)

    def test_official_channel_and_host_share_identity_but_keep_distinct_wake_routes(self):
        for version in (1, 2):
            identifier = "channel-" + str(version)
            channel = {"device": "desktop", "client": "claude-channel",
                       "session": "00000000-0000-0000-0000-000000000001"}
            host = {**channel, "client": "claude"}
            item = self.call("deliver", **{**self.payload, "id": identifier, "envelope_version": version,
                                          "recipient_endpoint": channel})["message"]
            self.assertEqual(item["recipient_endpoint"], channel)
            self.assertEqual(item["recipient_identity"], host)
            self.assertEqual(self.call("list", id=identifier, recipient_endpoint=host)["messages"], [item])
            self.assertEqual(self.call("receipt", id=identifier, reader_endpoint=channel)["read_by_endpoint"], host)

    @unittest.skipUnless(sys.platform in ("win32", "linux"), "requires OS watcher")
    def test_wait_wakes_all_registered_listeners(self):
        wait_on_event = messages._watch
        ready = threading.Barrier(3, timeout=5)

        @contextmanager
        def watch(directory):
            with wait_on_event(directory) as waiter:
                # Check the empty inbox before signaling both waiters are truly blocked on an OS event.
                def signal_then_wait(seconds):
                    if not getattr(local, "ready", False):
                        local.ready = True
                        ready.wait()
                    return waiter(seconds)

                yield signal_then_wait

        local = threading.local()
        with patch.object(messages, "_watch", watch), ThreadPoolExecutor(2) as pool:
            listeners = [pool.submit(self.call, "wait", id="m-1", timeout=5) for _ in range(2)]
            ready.wait()
            self.assertFalse(any(listener.done() for listener in listeners))
            self.call("deliver", **self.payload)
            for listener in listeners:
                result = listener.result(timeout=2)
                self.assertEqual(result["status"], "received")
                self.assertEqual(result["message"]["text"], self.payload["text"])


if __name__ == "__main__":
    unittest.main()
