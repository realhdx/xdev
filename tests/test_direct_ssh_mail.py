"""Check direct SSH routing, constrained key identity and native admission."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.adapters import sessions
from src.runtime import messages, outbox, ssh_mail, transport


class DirectMailTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.a, self.b = self.root / "a", self.root / "b"
        self.a.mkdir()
        self.b.mkdir()
        for root, name, own_key, nickname, peer_key, client in [
                (self.a, "local-pc", "alpha", "my-server", "beta", "writer"),
                (self.b, "lab-node", "beta", "other-pc", "alpha", "editor")]:
            config = {"device": name, "aliases": [own_key], "identity_key": own_key, "peers": {
                nickname: {"aliases": [peer_key], "identity_key": peer_key, "ssh": "host.example",
                           "rpc": "linux-mailbox", "mail_transport": "direct",
                           "mail_receivers": [{"client": client, "session": "draft"}]},
                "third": {}}}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.author = {"device": "local-pc", "client": "writer", "session": "draft"}
        self.target = {"device": "my-server", "client": "editor", "session": "draft"}
        self.letter = {"envelope_version": 2, "id": "letter-1", "from": "local-pc", "to": "my-server",
                       "text": "Hello", "sender_endpoint": self.author, "recipient_identity": self.target,
                       "recipient_endpoint": self.target, "reply_endpoint": self.author}
        self.notice = {"id": "letter-1", "client": "editor", "session": "draft", "steer": True}

    def frame(self, service, action, payload):
        return {"expected_device": "beta", "service": service, "action": action, "payload": payload}

    def submit(self, service, action, payload, *, direct=True):
        def accept(argv, wire, timeout):
            self.launch = argv
            self.wire = json.loads(wire)
            return ssh_mail.receive(self.b, "other-pc", self.wire)
        with patch.object(transport, "_json_request", side_effect=accept):
            return transport.peer_request("my-server", self.frame(service, action, payload), 5, root=self.a, direct=direct)

    def test_direct_ssh_uses_fixed_command_and_maps_both_devices(self):
        sent = self.submit("msg", "deliver", self.letter)
        self.assertEqual(sent["message"]["text"], self.letter["text"])
        self.assertEqual(self.wire["payload"]["from"], "alpha")
        self.assertEqual(self.wire["payload"]["recipient_endpoint"]["device"], "beta")
        self.assertEqual(self.launch[-2:], ["host.example", transport.LINUX_MAIL_COMMAND])
        self.assertEqual(self.launch[0], "ssh.exe" if os.name == "nt" else "ssh")
        for option in ("ProxyJump=none", "ProxyCommand=none", "ControlPath=none", "ClearAllForwardings=yes",
                       "StrictHostKeyChecking=yes", "ForwardAgent=no"):
            self.assertIn(option, self.launch)
        duplicate = self.submit("msg", "deliver", self.letter, direct=False)
        self.assertEqual((sent["status"], duplicate["status"]), ("received", "duplicate"))
        self.assertNotIn("ProxyJump=none", self.launch)

    def test_only_paired_sender_and_local_device_can_write(self):
        for override in ({"from": "third"}, {"to": "third"}, {"to": "local"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.submit("msg", "deliver", {**self.letter, **override})
        self.assertIsNone(messages.handle(self.b, "wait", {"id": "letter-1", "timeout": 0})["message"])
        frame = self.frame("msg", "deliver", self.letter)
        for bad in (None, "third", "local"):
            with self.subTest(expected=bad), self.assertRaises(ValueError):
                ssh_mail.receive(self.b, "alpha", {**frame, "expected_device": bad})

    def test_mailbox_profile_rejects_history_commands_and_files(self):
        for service, action in (("msg", "list"), ("msg", "receipt"), ("system", "who"), ("exec", "run"), ("file", "read")):
            with self.subTest(service=service, action=action):
                with patch.object(transport, "_json_request") as launch, self.assertRaises(ValueError):
                    transport.peer_request("my-server", self.frame(service, action, {}), 3, root=self.a)
                launch.assert_not_called()
                with self.assertRaises(ValueError):
                    ssh_mail.receive(self.b, "alpha", self.frame(service, action, {}))

    def test_notification_requires_own_committed_letter_exact_route_and_local_allowlist(self):
        with patch.object(sessions, "notify") as native:
            with self.assertRaises(ValueError):
                self.submit("ai", "notify", self.notice)
            self.submit("msg", "deliver", self.letter)
            with self.assertRaises(ValueError):
                self.submit("ai", "notify", {**self.notice, "session": "wrong"})
            with self.assertRaises(ValueError):
                ssh_mail.receive(self.b, "third", self.frame("ai", "notify", self.notice))
            unauthorized = {**self.target, "session": "not-allowed"}
            self.submit("msg", "deliver", {**self.letter, "id": "second", "recipient_identity": unauthorized,
                                          "recipient_endpoint": unauthorized})
            response = self.submit("ai", "notify", {**self.notice, "id": "second", "session": "not-allowed"})
            self.assertEqual(response["status"], "requires_receiver_binding")
            native.assert_not_called()

    def test_paired_device_can_address_new_chats_in_once_selected_application(self):
        cfg = self.b / "config.json"
        values = json.loads(cfg.read_text(encoding="utf-8"))
        peer = values["peers"]["other-pc"]
        peer.pop("mail_receivers")
        peer["receiver_apps"] = ["editor"]
        cfg.write_text(json.dumps(values), encoding="utf-8")
        fresh = {**self.target, "session": "new-root"}
        self.submit("msg", "deliver", {**self.letter, "recipient_endpoint": fresh, "recipient_identity": fresh})
        admitted = {"ok": True, "status": "admitted", "host_placement": "current_turn",
                    "admission_evidence": "portable_host_ack"}
        with patch.object(sessions, "notify", return_value=admitted) as host:
            reply = self.submit("ai", "notify", {**self.notice, "session": "new-root"})
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(host.call_args.args[1]["session"], "new-root")

    def test_exact_host_ack_is_deduplicated_and_native_diagnostics_stay_local(self):
        self.submit("msg", "deliver", self.letter)
        evidence = {"ok": True, "status": "admitted", "host_placement": "current_turn",
                    "admission_evidence": "portable_host_ack", "private_detail": "local-only"}
        with patch.object(sessions, "notify", return_value=evidence) as native:
            first = self.submit("ai", "notify", self.notice)
            again = self.submit("ai", "notify", self.notice)
        native.assert_called_once()
        self.assertEqual(first, again)
        self.assertEqual(first["host_placement"], "current_turn")
        self.assertNotIn("private_detail", first)

    def test_receipt_or_unmatched_reply_cannot_wake(self):
        with patch.object(sessions, "notify", return_value={"ok": True, "status": "queued"}) as native:
            incoming = {**self.letter, "kind": "reply", "reply_to": "previous"}
            self.submit("msg", "deliver", incoming)
            self.assertEqual(self.submit("ai", "notify", self.notice)["status"], "unverified_reply_origin")
            self.submit("msg", "deliver", {**incoming, "kind": "receipt", "id": "receipt"})
            with self.assertRaises(ValueError):
                self.submit("ai", "notify", {**self.notice, "id": "receipt"})
            native.assert_not_called()
            prior = {"from": "beta", "to": "alpha", "id": "previous", "text": "review?",
                     "recipient_identity": {**self.author, "device": "alpha"},
                     "reply_endpoint": {**self.target, "device": "beta"}}
            outbox.remember(self.b, prior)
            self.assertEqual(self.submit("ai", "notify", self.notice)["status"], "queued")
            native.assert_called_once()
    def test_host_key_rejection_surfaces_a_verifiable_reason(self):
        diagnostic = "@@@@@@@@@@@@@@@@@\\n@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\\nHost key verification failed.\\n"
        argv = [sys.executable, "-c", "import sys; sys.stderr.write(" + repr(diagnostic) + "); sys.exit(255)"]
        with self.assertRaises(transport.JsonRequestError) as error:
            transport._json_request(argv, b"", 3)
        self.assertEqual(error.exception.code, "SSH_HOST_KEY_REJECTED")
        self.assertIn("fingerprint", str(error.exception))

    def test_queue_only_peers_may_omit_ssh_route(self):
        cfg = json.loads((self.a / "config.json").read_text())
        cfg["peers"]["queue-peer"] = {"mail_transport": "sqs"}
        (self.a / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        devices = transport.directory(self.a)
        self.assertEqual(devices.mail_transport("queue-peer"), "sqs")
        with self.assertRaisesRegex(ValueError, "No SSH route"):
            devices.ssh_target("queue-peer")
