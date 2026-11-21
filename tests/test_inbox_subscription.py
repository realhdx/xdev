"""A chat can wait once for its own durable inbox, with an explicit cursor."""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.adapters import caller_context, inprocess_cli
from src.runtime import messages


class InboxSubscription(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "config.json").write_text(
            json.dumps({"device": "work", "peers": {"peer": {"ssh": "peer-ssh"}}}), encoding="utf8"
        )
        self.room = {"device": "work", "client": "writer", "session": "room-a"}
        self.neighbor = {**self.room, "session": "room-b"}
        self.author = {"device": "peer", "client": "editor", "session": "draft"}

    def deliver(self, mid, recipient=None, **fields):
        recipient = recipient or self.room
        return messages.handle(
            self.root,
            "deliver",
            {
                "id": mid,
                "from": "peer",
                "to": "work",
                "text": "private body must stay in inbox",
                "subject": "Short subject",
                "kind": "message",
                "sender_endpoint": self.author,
                "sender_identity_source": "explicit",
                "recipient_identity": recipient,
                **fields,
            },
        )

    def cmd(self, *args):
        with caller_context.bind({"xdev/session": {"client": "writer", "session": "room-a"}}):
            return inprocess_cli.invoke(["msg", "inbox", *args], root=self.root)

    def test_unread_backlog_metadata_pagination_and_reconnect(self):
        read = self.deliver("old")
        messages.handle(self.root, "receipt", {"id": "old", "reader_endpoint": self.room})
        self.deliver("other", self.neighbor)
        self.deliver("receipt", kind="receipt", reply_to="old")
        self.deliver("note")
        last = self.deliver("answer", kind="reply", reply_to="old")
        initial = self.cmd("--wait", "0", "--limit", "1")
        self.assertTrue(initial["ok"], initial)
        self.assertEqual([m["id"] for m in initial["messages"]], ["note"])
        self.assertEqual(initial["messages"][0]["sender_endpoint"], self.author)
        self.assertEqual(initial["messages"][0]["recipient_identity"], self.room)
        self.assertEqual(initial["messages"][0]["subject"], "Short subject")
        self.assertNotIn("private body", str(initial))
        self.assertNotIn("unread_ids", initial)
        resumed = self.cmd("--wait", "0", "--after-seq", str(initial["cursor"]))
        self.assertEqual([m["id"] for m in resumed["messages"]], ["answer"])
        self.assertEqual(resumed["messages"][0]["reply_to"], "old")
        self.assertEqual(resumed["cursor"], last["seq"])
        replay = self.cmd("--wait", "0", "--after", "0")
        self.assertEqual([m["id"] for m in replay["messages"]], ["old", "note", "answer"])
        self.assertEqual(replay["messages"][0]["seq"], read["seq"])
        normal = self.cmd("--after", "0")
        self.assertIn("receipt", [m["id"] for m in normal["messages"]])
        self.assertTrue(
            any(m["text"] == "private body must stay in inbox" and m["read_at"] is None for m in normal["messages"])
        )

    def test_empty_cursor_is_scoped_and_read_backlog_is_not_replayed(self):
        item = self.deliver("mine")
        messages.handle(self.root, "receipt", {"id": "mine", "reader_endpoint": self.room})
        self.deliver("other", self.neighbor)
        empty = self.cmd("--wait", "0")
        self.assertEqual((empty["status"], empty["cursor"], empty["messages"]), ("timeout", item["seq"], []))
        self.assertEqual(self.cmd("--wait", "0", "--after-seq", str(empty["cursor"]))["messages"], [])
        self.assertEqual(
            self.cmd("--wait", "0", "--to-client", "writer", "--to-session", "room-b")["messages"][0]["id"], "other"
        )

    def test_requires_local_scoped_chat_and_a_bounded_wait(self):
        unbound = inprocess_cli.invoke(["msg", "inbox", "--wait", "0"], root=self.root)
        self.assertEqual(unbound["error_code"], "INBOX_SCOPE_REQUIRED")
        for options in (["--device-inbox"], ["--host", "peer"], ["--limit", "101"], ["--after-seq", "-1"]):
            self.assertFalse(self.cmd("--wait", "0", *options)["ok"])
        for t in ("61", "nan", "-1", "inf"):
            self.assertFalse(self.cmd("--wait", t)["ok"])
        with self.assertRaises(ValueError):
            messages.wait_inbox(self.root, {"recipient_endpoint": self.author, "timeout": 0})

    @unittest.skipUnless(sys.platform in ("win32", "linux"), "requires the system filesystem event watcher")
    def test_real_wait_ignores_duplicates_receipts_neighbors_and_wakes_on_target(self):
        committed = self.deliver("previous")
        ready = threading.Event()
        factory = messages._watch

        @contextmanager
        def observed(directory):
            with factory(directory) as changed:

                def wait(seconds):
                    ready.set()
                    return changed(seconds)

                yield wait

        with patch.object(messages, "_watch", observed), ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.cmd, "--after-seq", str(committed["seq"]), "--wait", "4")
            self.assertTrue(ready.wait(2))
            self.deliver("previous")
            self.deliver("receipt", kind="receipt", reply_to="previous")
            self.deliver("wrong", self.neighbor)
            self.assertFalse(pending.done())
            target = self.deliver("mine")
            found = pending.result(timeout=3)
        self.assertEqual([m["id"] for m in found["messages"]], ["mine"])
        self.assertEqual(found["cursor"], target["seq"])

    @unittest.skipUnless(sys.platform in ("win32", "linux"), "requires the system filesystem event watcher")
    def test_commit_during_registration_and_missing_pulse_on_timeout(self):
        original = messages._watch

        @contextmanager
        def deliver_at_registration(directory):
            with original(directory) as changed:
                self.deliver("during")
                yield changed

        with patch.object(messages, "_watch", deliver_at_registration):
            receive = self.cmd("--wait", "1")
        self.assertEqual(receive["messages"][0]["id"], "during")

        @contextmanager
        def no_pulse(_directory):
            def delayed(_seconds):
                with patch.object(messages, "_notify", side_effect=OSError("simulated dropped pulse")):
                    self.deliver("committed")
                return False

            yield delayed

        with patch.object(messages, "_watch", no_pulse):
            result = self.cmd("--wait", "1", "--after", str(receive["cursor"]))
        self.assertEqual(result["messages"][0]["id"], "committed")

    def test_reconnect_after_watcher_failure_uses_durable_cursor(self):
        first = self.deliver("first")
        with patch.object(messages, "_watch", side_effect=OSError("watch interrupted")):
            self.assertFalse(self.cmd("--after-seq", str(first["seq"]), "--wait", "1")["ok"])
        self.deliver("while-closed")
        self.assertEqual(
            self.cmd("--after-seq", str(first["seq"]), "--wait", "0")["messages"][0]["id"], "while-closed"
        )

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires the optional official MCP SDK")
    def test_official_mcp_call_returns_to_original_caller_without_another_model_turn(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def run(log):
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "src.cli.main", "mcp"],
                cwd=Path(__file__).resolve().parents[1],
                env={"XDEV_HOME": str(self.root)},
            )
            meta = {"xdev/session": {"client": "writer", "session": "room-a"}}
            async with stdio_client(params, errlog=log) as (reads, writes), ClientSession(reads, writes) as client:
                await client.initialize()
                tools = await client.list_tools()
                description = next(t.description for t in tools.tools if t.name == "xdev_msg")
                self.assertIn("--after-seq", description)
                pending = asyncio.create_task(
                    client.call_tool("xdev_msg", {"action": "inbox", "arguments": ["--wait", "8"]}, meta=meta)
                )
                await asyncio.sleep(0.15)
                self.deliver("other", self.neighbor)
                self.deliver("selected")
                got = await pending
                self.assertFalse(got.isError, got)
                result = got.structuredContent
                self.assertEqual([m["id"] for m in result["messages"]], ["selected"])
                self.assertNotIn("private body", str(result))
                again = await client.call_tool(
                    "xdev_msg",
                    {"action": "inbox", "arguments": ["--wait", "0", "--after-seq", str(result["cursor"])]},
                    meta=meta,
                )
                self.assertEqual(again.structuredContent["messages"], [])

        with open(os.devnull, "w") as log:
            asyncio.run(run(log))
