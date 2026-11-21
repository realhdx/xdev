"""Exercise the same resident socket and mailbox dispatch path used by a host plugin."""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.adapters import portable, resident, sessions
from src.runtime import messages


@unittest.skipUnless(find_spec("cryptography"), "resident identity provisioning requires cryptography")
class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.identity = self.root / "receiver-identity"
        resident.provision(self.identity)
        self.target = {"device": "mercury", "client": "editor", "session": "original-7"}
        self.notice = {**self.target, "protocol": portable.PROTOCOL, "message_id": "mail1",
                       "notice_id": "notice-1", "mode": "send_now"}
        self.calls = []

    def original(self, session, notice):
        if session != self.target["session"]:
            return None
        self.calls.append(notice["notice_id"])
        return {"session": session, "placement": "current_turn"}

    def test_authentication_and_exact_host_confirmation(self):
        with resident.listen(self.identity, "editor", self.original) as route:
            self.assertEqual(resident.exchange(route, self.notice)["placement"], "current_turn")
            answer = resident.exchange(route, {**self.notice, "session": "absent"})
            self.assertEqual(answer["status"], "requires_activation")
            other = self.root / "unrelated-app"
            resident.provision(other)
            import ssl
            with self.assertRaises(ssl.SSLError):
                resident.exchange({**route, "identity_directory": str(other)}, self.notice)
        self.assertEqual(self.calls, ["notice-1"])
        self.assertEqual(resident.exchange(route, self.notice)["status"], "unavailable_before_submit")

    def test_cross_chat_ack_is_unknown(self):
        def wrong(session, notice):
            return {"session": "foreign", "placement": "current_turn"}
        with resident.listen(self.identity, "editor", wrong) as route:
            self.assertEqual(resident.exchange(route, self.notice)["status"], "unknown")

    def test_two_simultaneous_normal_sends_call_resident_once(self):
        with resident.listen(self.identity, "editor", self.original) as route:
            (self.root / "config.json").write_text(json.dumps({
                "device": "mercury", "session_hosts": {"editor": {"resident": route}}}))
            messages.handle(self.root, "deliver", {"id": "mail1", "from": "mercury", "to": "mercury",
                "text": "check", "recipient_identity": self.target, "recipient_endpoint": self.target})
            with ThreadPoolExecutor(2) as workers:
                results = list(workers.map(lambda _: sessions.submit(self.root, "mail1", "original-7", "editor"), range(2)))
            self.assertIn("admitted", [r["status"] for r in results])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(sessions.submit(self.root, "mail1", "original-7", "editor")["status"], "already_admitted")

    def test_lost_ack_never_activates_an_alternate_receiver(self):
        def lost(session, notice):
            self.calls.append(notice["notice_id"])
            raise RuntimeError("connection lost after host work")
        with resident.listen(self.identity, "editor", lost) as route:
            import sys
            (self.root / "config.json").write_text(json.dumps({"device": "mercury", "session_hosts": {
                "editor": {"resident": route, "argv": [sys.executable, "-V"]}}}))
            messages.handle(self.root, "deliver", {"id": "mail1", "from": "mercury", "to": "mercury",
                "text": "check", "recipient_identity": self.target, "recipient_endpoint": self.target})
            with patch.object(portable, "_exchange", side_effect=AssertionError("unexpected fallback")):
                for i in range(2):
                    self.assertEqual(sessions.submit(self.root, "mail1", "original-7", "editor")["status"], "unknown")
            self.assertEqual(len(self.calls), 1)
