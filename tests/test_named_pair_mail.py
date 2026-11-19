"""Addressed chat routes through the real dispatcher before and after adopting custom names."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.adapters import caller_context, inprocess_cli, sessions
from src.cli import main
from src.runtime import transport


class NamedPairMessages(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.left, self.right = (self.base / name for name in ("left", "right"))
        self.left.mkdir()
        self.right.mkdir()
        self.a, self.b = "ses_study_a", "ses_lab_b"
        self.names = {"laptop": self.left, "desktop": self.right, "书房": self.left, "实验室": self.right}
        for name, root in (("laptop", self.left), ("desktop", self.right)):
            (root / "config.json").write_text(json.dumps({"device": name}), encoding="utf-8")
        def peer(host, wire, timeout, *, root):
            destination = self.names[host]
            self.assertTrue(transport.directory(destination).same(wire["expected_device"], transport.node(destination)))
            return main._request(destination, wire["service"], wire["action"], wire["payload"])
        self.enterContext(patch.object(transport, "peer_request", side_effect=peer))
        self.enterContext(patch.object(sessions, "notify", return_value={
            "ok": True, "status": "admitted", "delivery_mode": "steer", "admission": {"accepted": True}}))

    def adopt_names(self):
        for here, old, name, peer_old, peer_name in (
                (self.left, "laptop", "书房", "desktop", "实验室"),
                (self.right, "desktop", "实验室", "laptop", "书房")):
            cfg = {"device": name, "identity_key": old, "aliases": [old],
                   "peers": {peer_name: {"identity_key": peer_old, "ssh": peer_old, "aliases": [peer_old]}}}
            (here / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def call(self, root, session, *args):
        with caller_context.bind({"ai.opencode/sessionID": session}):
            reply = inprocess_cli.invoke(["msg", *args], root)
        self.assertTrue(reply.get("ok"), reply)
        return reply

    def test_old_letter_new_reply_then_forward_and_reply_to_original_author(self):
        self.call(self.left, self.a, "send", "--to", "desktop", "--client", "opencode", "--session", self.b,
                  "--id", "before-renaming", "--subject", "原题", "--text", "原件十七行")
        self.adopt_names()
        self.assertEqual(self.call(self.right, self.b, "inbox", "--subject", "原题")["messages"][0]["from"], "laptop")
        self.assertEqual(self.call(self.right, self.b, "read", "before-renaming")["message"]["to"], "desktop")
        self.call(self.right, self.b, "reply", "before-renaming", "--text", "旧信已查")
        returned = self.call(self.left, self.a, "inbox", "--reply-to", "before-renaming")["messages"][0]
        self.assertEqual((returned["from"], returned["to"]), ("实验室", "书房"))
        third = "ses_third_reader"
        fwd = self.call(self.right, self.b, "forward", "before-renaming", "--to", "laptop", "--client", "opencode",
                        "--session", third, "--text", "附原件")
        copy = self.call(self.left, third, "read", fwd["id"])["message"]["letter_attachment"]["original"]
        self.assertEqual((copy["from"], copy["to"], copy["text"]), ("laptop", "desktop", "原件十七行"))
        self.call(self.left, third, "reply", fwd["id"], "--audience", "original-author", "--text", "已阅原件")
        self.assertEqual(self.call(self.left, self.a, "inbox", "--reply-to", fwd["id"])["messages"][0]["text"], "已阅原件")

    def test_rpc_rejects_misbound_remote_command_before_it_can_create_a_file(self):
        self.adopt_names()
        probe = self.base / "wrong-machine.txt"
        request = {"service": "exec", "action": "run", "expected_device": "书房",
                   "payload": {"argv": [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(probe)]}}
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "src.cli.main", "_rpc"],
                                input=json.dumps(request).encode("utf-8"), capture_output=True, timeout=10,
                                env=dict(os.environ, XDEV_HOME=str(self.right)))
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertFalse(probe.exists())

