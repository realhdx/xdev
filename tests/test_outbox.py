import json
from pathlib import Path
import tempfile
import unittest

from src.runtime import outbox, transport


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = {"device": "orchard", "aliases": ["writer-node"],
                  "peers": {"mill": {"aliases": ["review-node"], "mail_transport": "relay"}}}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.a = {"device": "orchard", "client": "pen", "session": "draft"}
        self.b = {"device": "mill", "client": "lens", "session": "review"}
        self.letter = {"id": "test-origin", "from": "orchard", "to": "mill", "text": "Review this.",
                       "recipient_identity": self.b, "recipient_endpoint": self.b,
                       "sender_endpoint": self.a, "reply_endpoint": self.a}
        self.reply = {"id": "test-answer", "kind": "reply", "text": "Looks good.", "reply_to": "test-origin",
                      "sender_endpoint": self.b, "recipient_endpoint": self.a}

    def test_registered_chat_without_host_admission_only_confirms_its_address(self):
        from unittest.mock import patch
        from src.cli.main import _return_readiness
        from src.runtime import messages

        endpoint = {**self.a, "client": "dsh"}
        with patch("src.adapters.dsh.receiver_binding", return_value={"bound": True}):
            actual = _return_readiness(self.root, endpoint)
        self.assertTrue(actual["return_address_known"])
        self.assertIsNone(actual["bidirectional_ready"])
        self.assertEqual(actual["return_session_status"], "receiver_registered")
        local = {**self.letter, "to": "orchard", "from": "mill",
                 "recipient_identity": self.a, "recipient_endpoint": self.a,
                 "sender_endpoint": self.b, "reply_endpoint": self.b}
        messages.handle(self.root, "deliver", local)
        route = messages.resolve_reply_route(self.root, self.letter["id"])
        self.assertTrue(route["return_address_known"])
        self.assertIsNone(route["bidirectional_ready"])

    def test_only_expected_chat_and_callback_can_wake(self):
        self.assertFalse(outbox.permits_reply(self.root, self.reply))
        outbox.remember(self.root, self.letter)
        self.assertTrue(outbox.permits_reply(self.root, self.reply))
        for fields in ({"reply_to": "unrelated"}, {"kind": "receipt"}, {"text": " "},
                       {"sender_endpoint": {**self.b, "session": "another"}},
                       {"recipient_endpoint": {**self.a, "client": "other"}}):
            self.assertFalse(outbox.permits_reply(self.root, {**self.reply, **fields}))
        aliases = {**self.reply, "sender_endpoint": {**self.b, "device": "review-node"}}
        self.assertTrue(outbox.permits_reply(self.root, aliases))

    def test_automatic_arrival_report_preserves_original_mode_on_retry(self):
        historical = {**self.letter, "id": "historical-letter"}
        self.assertFalse(outbox.remember(self.root, historical))
        self.assertFalse(outbox.remember(self.root, historical, automatic_report=True))
        self.assertTrue(outbox.remember(self.root, self.letter, automatic_report=True))
        self.assertTrue(outbox.remember(self.root, self.letter, automatic_report=True))
        self.assertEqual(outbox.status(self.root, self.letter["id"], self.a)["status"], "awaiting_report")

    def test_carrier_report_can_change_on_retry_including_preview_three_rows(self):
        import hashlib
        import sqlite3
        from contextlib import closing
        from src.runtime import messages

        outbox.remember(self.root, self.letter)
        outbox.remember(self.root, self.letter, delivery_report=True)
        self.assertEqual(outbox.status(self.root, self.letter["id"], self.a)["status"], "awaiting_report")
        canonical = {**self.letter, **messages.address_fields(self.letter, transport.directory(self.root).identity_key)}
        legacy = hashlib.sha256(outbox._json({**canonical, "delivery_report_requested": True}).encode()).hexdigest()
        with closing(sqlite3.connect(self.root / "state" / "sent-mail.sqlite3")) as db, db:
            db.execute("UPDATE sent SET digest=? WHERE id=?", (legacy, self.letter["id"]))
        outbox.remember(self.root, self.letter)
        with self.assertRaisesRegex(ValueError, "different letter"):
            outbox.remember(self.root, {**self.letter, "text": "changed"}, delivery_report=True)

    def test_dsh_unbound_sender_preserves_return_address_and_explains_wake(self):
        from unittest.mock import patch
        from src.adapters import caller_context, inprocess_cli
        from src.runtime import session_relay
        session = "session-17263819-cab2-4cde-9382-c1c0e301c4a3"
        with caller_context.bind_dsh(session), patch.object(
                session_relay, "publish", return_value={"ok": True, "status": "broker_acknowledged"}):
            owner = inprocess_cli.invoke(["msg", "whoami"], root=self.root)
            sent = inprocess_cli.invoke(["msg", "send", "--to", "mill", "--client", "lens", "--session", "review",
                                         "--text", "One", "--subject", "Test"], root=self.root)
        self.assertEqual(owner["return_session_status"], "requires_receiver_binding")
        self.assertEqual(sent["reply_endpoint"]["session"], session)
        self.assertTrue(sent["return_address_known"])
        self.assertFalse(sent["bidirectional_ready"])
        self.assertIn("automatic", sent["return_next_action"])
        self.assertNotIn("--session", sent["return_next_action"])

    def test_github_mailbox_report_does_not_overstate_reading_or_rewrite_historical_admission(self):
        from src.runtime import mail_routes, session_relay
        for state, placement in (("not_requested", "unknown"), ("admitted", "current_turn")):
            letter = {**self.letter, "id": "report-" + state}
            mid = letter["id"]
            outbox.remember(self.root, letter, delivery_report=True)
            mail_routes.send(self.root, "mill", mid, ("github",),
                             lambda route: {"ok": True, "status": "queue_acknowledged"})
            self.assertNotIn("model_woken", outbox.status(self.root, mid, self.a))
            report = {"schema": session_relay.REPORT_SCHEMA, "message_id": mid, "inbox_status": "received",
                      "session_status": state, "placement": placement}
            receipt = {"id": session_relay.report_id("orchard", mid), "reply_to": mid, "kind": "receipt",
                       "from": "mill", "to": "orchard", "recipient_identity": self.a, "text": json.dumps(report)}
            self.assertTrue(outbox.record_report(self.root, receipt))
            actual = outbox.status(self.root, mid, self.a)
            self.assertEqual(actual["model_status"], "unverified")
            if state == "not_requested":
                self.assertEqual(actual["delivery_scope"], "mailbox")
                self.assertTrue(actual["available_to_addressed_chat"])
                self.assertFalse(actual["model_woken"])
                self.assertFalse(actual["session_accepted"])
            else:
                self.assertTrue(actual["session_accepted"])
                self.assertNotIn("model_woken", actual)

    def test_report_is_private_to_sender_and_requires_requested_matching_peer(self):
        from src.runtime import session_relay
        outbox.remember(self.root, self.letter, delivery_report=True)
        report = {"schema": session_relay.REPORT_SCHEMA, "message_id": "test-origin", "inbox_status": "received",
                  "session_status": "queued", "placement": "next_turn"}
        receipt = {"id": session_relay.report_id("orchard", "test-origin"), "reply_to": "test-origin",
                   "kind": "receipt", "from": "mill", "to": "writer-node", "recipient_identity": self.a,
                   "text": json.dumps(report)}
        self.assertEqual(outbox.status(self.root, "test-origin", self.a)["status"], "awaiting_report")
        for override in ({"from": "orchard"}, {"recipient_identity": self.b}, {"sender_endpoint": self.b},
                         {"id": "wrong-report"}, {"text": json.dumps({**report, "placement": "current_turn"})}):
            self.assertFalse(outbox.record_report(self.root, {**receipt, **override}))
        self.assertTrue(outbox.record_report(self.root, receipt))
        self.assertEqual(outbox.status(self.root, "test-origin", self.a)["report"], report)
        self.assertFalse(outbox.status(self.root, "test-origin", self.b)["ok"])
        self.assertFalse(outbox.status(self.root, "test-origin", None, as_device=True)["ok"])

    def test_private_peer_blocks_both_explicit_and_automatic_legacy_broker(self):
        import asyncio
        from unittest.mock import patch
        from src.adapters import inprocess_cli
        from src.cli import main
        from src.runtime import messages
        config = json.loads((self.root / "config.json").read_text())
        config["broker"] = {"url": "nats://127.0.0.1:4222"}
        config["peers"]["mill"]["mail_transport"] = "auto"
        config["managed_queue"] = {"peers": {"review-node": {}}}
        (self.root / "config.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "requires a sealed"):
            asyncio.run(messages._broker(self.root, "publish", self.letter))
        with patch.object(main, "_request", side_effect=AssertionError("unexpected legacy publish")), patch.object(
                main, "_call", return_value={"ok": True, "status": "received"}):
            result = inprocess_cli.invoke(["msg", "send", "--to", "mill", "--id", "test-guard", "--text", "Hello"],
                                         root=self.root)
        self.assertEqual(result["transport"], "ssh")

    def test_generic_mcp_identity_and_builtin_provenance(self):
        from src.adapters import caller_context, inprocess_cli
        with caller_context.bind({"xdev/session": {"client": "pen", "session": "draft"}}):
            result = inprocess_cli.invoke(["msg", "whoami"], root=self.root)
            self.assertEqual(result["sender_endpoint"], self.a)
        with caller_context.bind({"xdev/session": {"client": "codex", "session": "1542ab0a-d493-4735-8678-0a7a22b8c72e"}}):
            self.assertNotEqual(caller_context.current()["status"], "ready")
        with caller_context.bind({"ai.opencode/sessionID": "ses_ABCDE", "xdev/session": {"client": "pen", "session": "draft"}}):
            self.assertEqual(caller_context.current()["status"], "conflict")

    def test_conflicting_replay_leaves_first_intent_in_place(self):
        outbox.remember(self.root, self.letter)
        outbox.remember(self.root, {**self.letter, "to": "review-node"})
        with self.assertRaisesRegex(ValueError, "different letter"):
            outbox.remember(self.root, {**self.letter, "text": "A different letter."})
        self.assertTrue(outbox.permits_reply(self.root, self.reply))

    def test_legacy_send_reply_to_uses_correlated_relay_envelope(self):
        from unittest.mock import patch
        from src.adapters import caller_context, inprocess_cli
        from src.runtime import messages, session_relay
        original = {"id": "incoming", "envelope_version": 2, "from": "mill", "to": "orchard", "text": "Hello",
                    "sender_endpoint": self.b, "recipient_identity": self.a, "recipient_endpoint": self.a,
                    "reply_endpoint": self.b}
        messages.handle(self.root, "deliver", original)
        with caller_context.bind({"xdev/session": {"client": "pen", "session": "draft"}}), patch.object(
                session_relay, "publish", return_value={"ok": True, "status": "broker_acknowledged"}) as publish:
            result = inprocess_cli.invoke(["msg", "send", "--reply-to", "incoming", "--text", "Answered"], root=self.root)
        self.assertTrue(result["ok"], result)
        sent = publish.call_args.args[1]
        self.assertEqual((sent["kind"], sent["reply_to"], sent["recipient_identity"]), ("reply", "incoming", self.b))

    def test_waiting_call_never_authorizes_host_wake(self):
        callback = {"device": "orchard", "channel": "tool_result", "request_id": "test-origin"}
        outbox.remember(self.root, {**self.letter, "reply_endpoint": callback})
        self.assertFalse(outbox.permits_reply(self.root, self.reply))
        self.assertFalse(outbox.permits_tool_reply(self.root, self.reply))
        response = {**self.reply, "recipient_endpoint": None, "recipient_identity": self.a}
        self.assertTrue(outbox.permits_tool_reply(self.root, response))
        for changes in ({"reply_to": "unknown"}, {"recipient_identity": self.b}, {"sender_endpoint": self.a},
                        {"recipient_endpoint": self.a}, {"kind": "receipt"}, {"text": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(outbox.permits_tool_reply(self.root, {**response, **changes}))

    def test_relay_only_peer_does_not_fall_back_to_local_execution(self):
        directory = transport.directory(self.root)
        self.assertEqual(directory.mail_transport("review-node"), "relay")
        self.assertIsNone(directory.ssh_target("orchard"))
        with self.assertRaisesRegex(ValueError, "No SSH route"):
            directory.ssh_target("mill")
