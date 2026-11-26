"""Cross-route outcomes for an individual letter and distinct copies in a discussion."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.adapters import caller_context, inprocess_cli
from src.runtime import mail_contact
from src.runtime.delivery_status import summarize


LETTER = "test-letter"
NATIVE_MISS = {
    "id": LETTER, "ok": False, "status": "session_requires_receiver_binding",
    "error_code": "SESSION_RECEIVER_NOT_REGISTERED", "transport": "direct",
    "inbox_status": "received", "session_status": "requires_receiver_binding",
    "session_accepted": False,
}


class DeliveryStatusTests(unittest.TestCase):
    def test_native_failure_then_addressed_read_then_correlated_reply(self):
        pending = summarize(NATIVE_MISS)
        self.assertEqual(pending["state"], "pending_wake")
        self.assertNotEqual(pending["retry"], "retry_same_id")
        self.assertTrue(pending["evidence"])

        observed = {"ok": True, "id": LETTER, "scope": "recipient_mailbox", "status": "observed",
                    "received_at": "2026-01-01T00:00:00Z", "read_at": "2026-01-01T00:00:05Z",
                    "read_scope": "session"}
        self.assertEqual(summarize(NATIVE_MISS, observation=observed)["state"], "read")
        self.assertNotEqual(summarize(NATIVE_MISS, observation={**observed, "read_scope": "device"})["state"], "read")
        response = {"id": "answer", "reply_to": LETTER, "received_at": "2026-01-01T00:00:07Z",
                    "source": "local_addressed_inbox"}
        answered = summarize(NATIVE_MISS, observation=observed, linked_reply=response)
        self.assertEqual((answered["state"], answered["retry"]), ("replied", "never"))
        unavailable = {"ok": False, "id": LETTER, "status": "unconfirmed"}
        self.assertEqual(summarize(NATIVE_MISS, observation=unavailable, linked_reply=response)["state"], "replied")

    def test_relay_acknowledgement_and_host_admission_are_separate(self):
        submitted = {"id": LETTER, "ok": True, "status": "queue_acknowledged", "transport": "github"}
        self.assertEqual(summarize(submitted)["state"], "submitted")
        report = {"schema": "xdev.delivery-report.v1", "message_id": LETTER, "inbox_status": "received",
                  "session_status": "queued", "placement": "next_turn"}
        queued = summarize(submitted, report=report)
        self.assertEqual(queued["state"], "queued")
        accepted = summarize(submitted, report={**report, "session_status": "admitted", "placement": "current_turn"})
        self.assertEqual(accepted["state"], "accepted")
        self.assertNotEqual(accepted["retry"], "retry_same_id")

    def test_only_definitively_unsent_letter_is_safe_to_submit_again(self):
        uncertain = {"id": LETTER, "ok": False, "status": "submission_unconfirmed",
                     "inbox_status": "unknown", "session_accepted": None}
        verdict = summarize(uncertain)
        self.assertEqual((verdict["state"], verdict["retry"]), ("unknown", "check_same_id"))
        unsent = {"id": LETTER, "ok": False, "status": "no_route_available", "inbox_status": "not_attempted",
                  "route_attempts": [{"transport": "direct", "status": "not_submitted"}]}
        verdict = summarize(unsent)
        self.assertEqual((verdict["state"], verdict["retry"]), ("failed", "retry_same_id"))

    def test_a_withdrawn_letter_stays_withdrawn_despite_confirmed_arrival(self):
        observed = {"id": LETTER, "ok": True, "scope": "recipient_mailbox", "status": "observed",
                    "received_at": "2026-01-01T00:00:00Z", "withdrawn_at": "2026-01-01T00:01:00Z"}
        verdict = summarize(NATIVE_MISS, observation=observed)
        self.assertEqual((verdict["state"], verdict["retry"]), ("withdrawn", "never"))

    def test_parallel_letters_do_not_share_the_other_letters_read_reply_or_host_report(self):
        other = "another-letter"
        unrelated = {"ok": True, "scope": "recipient_mailbox", "id": other, "status": "observed",
                     "read_scope": "session", "read_at": "2026-01-01T00:00:05Z"}
        unrelated_reply = {"id": "other-answer", "reply_to": other, "source": "local_addressed_inbox"}
        unrelated_report = {"schema": "xdev.delivery-report.v1", "message_id": other, "inbox_status": "received",
                            "placement": "current_turn", "session_status": "admitted"}
        verdict = summarize(NATIVE_MISS, report=unrelated_report, observation=unrelated, linked_reply=unrelated_reply)
        self.assertEqual(verdict, summarize(NATIVE_MISS))

    def test_a_later_offline_probe_preserves_only_this_recipient_and_senders_confirmed_read(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "config.json").write_text(json.dumps({"device": "station"}), encoding="utf-8")
        sender, recipient, unrelated = [
            {"device": "station", "client": "dsh", "session": "session-00000000-0000-4000-8000-00000000000" + digit}
            for digit in "123"
        ]
        observed = {"id": LETTER, "ok": True, "scope": "recipient_mailbox", "read_scope": "session",
                    "read_at": "2026-01-01T00:00:05Z"}
        mail_contact.confirmed_delivery(root, LETTER, sender, recipient, observed, None)
        offline = {"id": LETTER, "ok": False, "status": "unconfirmed"}
        for author, target, expected in [(sender, recipient, "read"), (sender, unrelated, "pending_wake"),
                                         (unrelated, recipient, "pending_wake")]:
            with self.subTest(author=author, target=target):
                cached, reply = mail_contact.confirmed_delivery(root, LETTER, author, target, offline, None)
                self.assertEqual(summarize(NATIVE_MISS, observation=cached, linked_reply=reply)["state"], expected)


class DiscussionDeliveryTests(unittest.TestCase):
    def test_reusing_group_refreshes_a_read_copy_without_notifying_either_recipient_again(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "config.json").write_text(json.dumps({"device": "station"}), encoding="utf-8")
        sender, first, second = ["session-00000000-0000-4000-8000-00000000000" + digit for digit in "123"]
        command = ["msg", "send", "--to", "station", "--client", "dsh", "--session", first,
                   "--also", "station", "dsh", second, "--id", "group-test", "--text", "Please check."]
        with (patch("src.adapters.sessions.discover", return_value={"status": "not_available"}),
              patch("src.adapters.sessions.notify", return_value={
                  "ok": False, "status": "requires_receiver_binding",
                  "error_code": "SESSION_RECEIVER_NOT_REGISTERED"}) as notify):
            with caller_context.bind_dsh(sender):
                submitted = inprocess_cli.invoke(command, root=root)
            self.assertTrue(submitted["ok"], submitted)
            self.assertEqual(submitted["status"], "group_submitted")
            item = next(item for item in submitted["deliveries"] if item["session"] == first)
            with caller_context.bind_dsh(first):
                self.assertTrue(inprocess_cli.invoke(["msg", "read", item["id"]], root=root)["ok"])
            with caller_context.bind_dsh(sender):
                current = inprocess_cli.invoke(command, root=root)
            self.assertEqual(notify.call_count, 2)
        by_recipient = {item["session"]: item for item in current["deliveries"]}
        self.assertEqual(by_recipient[first]["status"], "read")
        self.assertEqual(by_recipient[first]["delivery"]["state"], "read")
        self.assertEqual(by_recipient[second]["delivery"]["state"], "pending_wake")

    def test_group_displays_the_outcome_of_each_copy_even_when_reusing_the_batch(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "config.json").write_text(json.dumps({"device": "station"}), encoding="utf-8")
        first = "00000000-0000-4000-8000-000000000001"
        second = "00000000-0000-4000-8000-000000000002"
        argv = ["msg", "send", "--to", "station", "--client", "codex", "--session", first,
                "--also", "station", "codex", second, "--id", "group-test", "--text", "test"]

        def send_one(_root, args, **kwargs):
            state = "accepted" if args.session == first else "pending_wake"
            return {"id": args.id, "ok": True, "status": "received", "host": args.to, "client": args.client,
                    "session": args.session, "delivery": {"state": state, "retry": "check_same_id", "evidence": []}}

        with (caller_context.bind_dsh("session-00000000-0000-4000-8000-000000000003"),
              patch("src.cli.main._send", side_effect=send_one) as send):
            original = inprocess_cli.invoke(argv, root=root)
            again = inprocess_cli.invoke(argv, root=root)
        self.assertTrue(original["ok"], original)
        self.assertEqual(original["status"], "group_submitted")
        self.assertEqual(send.call_count, 2)
        items = {item["session"]: item for item in original["deliveries"]}
        self.assertNotEqual(items[first]["id"], items[second]["id"])
        self.assertEqual(items[first]["delivery"]["state"], "accepted")
        self.assertEqual(items[second]["delivery"]["state"], "pending_wake")
        for saved in again["deliveries"]:
            self.assertEqual(saved["delivery"], items[saved["session"]]["delivery"])
            self.assertTrue(saved["previously_accepted"])


class DeliveryThroughCliTests(unittest.TestCase):
    def test_stale_gateway_mailbox_snapshot_does_not_hide_a_newer_addressed_read_on_another_route(self):
        from src.cli import contact
        command = {"op": "status", "id": LETTER, "target": {"device": "station"}}
        received = {"ok": True, "id": LETTER, "scope": "recipient_mailbox", "status": "observed",
                    "received_at": "2026-01-01T00:00:00Z"}
        read = {**received, "read_scope": "session", "read_at": "2026-01-01T00:00:05Z"}
        routes = {"github": {"status": "peer_reported", "evidence": received}, "ssh": read}
        with (patch("src.runtime.mail_routes.candidates", return_value=["github", "ssh"]),
              patch.object(contact, "remote_or_local", side_effect=lambda root, route, cmd: routes[route])):
            observed, attempts = contact.status_checks(None, "github", command, automatic=True)
        self.assertEqual([item["transport"] for item in attempts], ["github", "ssh"])
        self.assertEqual(summarize(NATIVE_MISS, observation=observed.get("evidence") or observed)["state"], "read")

    def test_missing_receiver_then_read_then_original_reply_via_the_public_commands(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "config.json").write_text(json.dumps({"device": "station"}), encoding="utf-8")
        sender = "session-00000000-0000-4000-8000-000000000001"
        recipient = "session-00000000-0000-4000-8000-000000000002"
        bystander = "session-00000000-0000-4000-8000-000000000003"

        def call(session, *args):
            with caller_context.bind_dsh(session):
                return inprocess_cli.invoke(["msg", *args], root=root)

        with (patch("src.adapters.sessions.discover", return_value={"status": "not_available"}),
              patch("src.adapters.sessions.notify", return_value={
                  "ok": False, "status": "requires_receiver_binding",
                  "error_code": "SESSION_RECEIVER_NOT_REGISTERED"}) as notify):
            delivered = call(sender, "send", "--to", "station", "--client", "dsh", "--session", recipient,
                             "--id", LETTER, "--text", "Please check.", "--wait-reply", "0")
            self.assertEqual(delivered["delivery"]["state"], "pending_wake", delivered)
            self.assertTrue(delivered["ok"], delivered)
            self.assertEqual(call(sender, "status", LETTER)["delivery"]["state"], "pending_wake")
            self.assertFalse(call(bystander, "status", LETTER)["ok"])

            self.assertTrue(call(recipient, "read", LETTER)["ok"])
            self.assertEqual(call(sender, "status", LETTER)["delivery"]["state"], "read")
            reply = call(recipient, "reply", LETTER, "--id", "test-answer", "--text", "Checked.")
            self.assertTrue(reply["ok"], reply)
            answered = call(sender, "status", LETTER)
            self.assertEqual(answered["delivery"]["state"], "replied")
            self.assertEqual(answered["linked_reply"]["id"], reply["id"])
            self.assertEqual(notify.call_count, 1)
