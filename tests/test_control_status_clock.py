"""Check signed status requests at the trust window boundaries of two paired clocks."""

import unittest
from unittest.mock import patch

import test_mail_contact as fixtures
from src.runtime import mail_control_ssh, messages


class ControlClockTests(unittest.TestCase):
    setUp = fixtures.ContactTests.setUp
    receiver_time = 2_000_000_000

    def signed(self, op, sender_clock_offset):
        command = {"op": op, "actor": self.author, "target": self.target}
        if op != "preflight":
            command["id"] = self.original["id"]
        with (patch("time.time", return_value=self.receiver_time + sender_clock_offset),
              patch("src.runtime.transport.peer_request", side_effect=lambda peer, frame, **kwargs: frame["payload"])):
            return mail_control_ssh.call(self.a, "beta", command)

    def receive(self, frame, elapsed=0):
        with patch("time.time", return_value=self.receiver_time + elapsed):
            return mail_control_ssh.receive(self.b, frame)

    def test_status_and_preflight_accept_both_clock_directions_up_to_expiration(self):
        messages.handle(self.b, "deliver", self.original)
        with patch("src.runtime.mail_contact.local_probe", return_value={"host_state": "idle"}):
            for op in ("status", "preflight"):
                for offset in (-90, -72, 0, 72, 90):
                    with self.subTest(op=op, sender_clock_offset=offset):
                        frame = self.signed(op, offset)
                        self.assertEqual(frame["expires"], self.receiver_time + offset + 90)
                        self.assertTrue(self.receive(frame)["ok"])

    def test_expired_and_out_of_window_read_requests_never_reach_the_mailbox(self):
        with (patch("src.runtime.mail_contact.local_probe") as probe,
              patch("src.runtime.mail_lifecycle.apply") as status):
            for op in ("status", "preflight"):
                for offset in (-91, 91):
                    with self.subTest(op=op, sender_clock_offset=offset):
                        frame = self.signed(op, offset)
                        with self.assertRaisesRegex(ValueError, "expiration"):
                            self.receive(frame)
            probe.assert_not_called()
            status.assert_not_called()

    def test_each_signed_nonce_is_spent_until_its_own_expiration_then_the_frame_is_expired(self):
        messages.handle(self.b, "deliver", self.original)
        frame = self.signed("status", 72)
        self.assertTrue(self.receive(frame)["ok"])
        for elapsed in (0, 121, 162):
            with self.subTest(elapsed=elapsed), self.assertRaisesRegex(ValueError, "already used"):
                self.receive(frame, elapsed)
        with self.assertRaisesRegex(ValueError, "expiration"):
            self.receive(frame, 163)

    def test_mutations_keep_the_shorter_window_and_a_read_signature_cannot_authorize_recall(self):
        with patch("src.runtime.mail_lifecycle.apply", return_value={"ok": True}) as mutation:
            for op in ("recall", "replace", "correct", "progress"):
                with self.subTest(op=op):
                    with self.assertRaisesRegex(ValueError, "expiration"):
                        self.receive(self.signed(op, 31))
                    with self.assertRaisesRegex(ValueError, "expiration"):
                        self.receive(self.signed(op, -91))
            signed_read = self.signed("status", 0)
            changed = {**signed_read, "command": {**signed_read["command"], "op": "recall"}}
            with self.assertRaisesRegex(ValueError, "signature"):
                self.receive(changed)
            mutation.assert_not_called()
            self.assertTrue(self.receive(self.signed("recall", 30))["ok"])
            mutation.assert_called_once()
