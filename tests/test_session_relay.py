"""Regression tests for authenticated, session-addressed broker delivery."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.runtime import messages, session_relay as relay


class SessionRelayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.key = base / "shared.key"
        self.key.write_text("ab" * 32)
        password = base / "password"
        password.write_text("test-secret")
        self.a = {"device": "alpha", "client": "writer", "session": "editor/1"}
        self.b = {"device": "beta", "client": "reviewer", "session": "原会话-2"}
        for endpoint, peer in ((self.a, self.b), (self.b, self.a)):
            device, other = endpoint["device"], peer["device"]
            root = base / device
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"device": device,
                "peers": {other: {"mail_transport": "relay"}},
                "session_relay": {"url": "nats://127.0.0.1:4222", "user": device, "password_file": str(password),
                    "peers": {other: {"key_file": str(self.key),
                        "receivers": [{"client": endpoint["client"], "session": endpoint["session"]}]}}}}))
        self.source, self.dest = (relay._Settings(base / device) for device in ("alpha", "beta"))
        self.letter = {"envelope_version": 2, "id": "id-1", "from": "alpha", "to": "beta", "text": "交接原稿",
                       "subject": "原稿", "sender_endpoint": self.a, "reply_endpoint": self.a,
                       "recipient_endpoint": self.b, "recipient_identity": self.b}
        self.notice = {"id": "id-1", "client": "reviewer", "session": "原会话-2"}

    def packed(self, **updates):
        return relay._packet(self.source, {**self.letter, **updates}, self.notice)

    def test_peer_app_scope_allows_new_addresses_and_honors_profile_boundaries(self):
        config_path = self.dest.root / "config.json"
        config = json.loads(config_path.read_text())
        peer = config["session_relay"]["peers"]["alpha"]
        peer.pop("receivers")
        peer["receiver_apps"] = ["reviewer", "codex"]
        config_path.write_text(json.dumps(config))
        dest = relay._Settings(self.dest.root)
        fresh = {**self.b, "session": "created-after-setup"}
        self.assertTrue(dest.allows("alpha", fresh))
        self.assertFalse(dest.allows("alpha", {**fresh, "client": "unselected"}))
        self.assertFalse(dest.allows("alpha", {**fresh, "device": "alpha"}))
        profiled = {"device": "beta", "client": "codex", "session": "016d840d-afbc-4631-930c-6f2170815a13",
                    "codex_home": r"C:\PrivateProfile"}
        self.assertFalse(dest.allows("alpha", profiled))
        self.assertEqual(relay._unpack(dest, *relay._packet(self.source, {**self.letter,
            "recipient_endpoint": fresh, "recipient_identity": fresh}, {**self.notice, "session": fresh["session"]}))[0], "alpha")

    def test_authenticated_both_device_and_session(self):
        topic, wire = self.packed()
        self.assertEqual(topic, "xdev.session.v1.beta.alpha")
        self.assertEqual(relay._unpack(self.dest, topic, wire), ("alpha", self.letter, self.notice, False))
        for wrong in ("aicollab.inbox.beta", "xdev.session.v1.beta.stranger", "xdev.session.v1.alpha.beta"):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                relay._unpack(self.dest, wrong, wire)
        changed = json.loads(wire)
        changed["envelope"]["text"] += "!"
        with self.assertRaisesRegex(ValueError, "authentication"):
            relay._unpack(self.dest, topic, json.dumps(changed).encode())
        with self.assertRaisesRegex(ValueError, "addressed chat"):
            relay._packet(self.source, self.letter, {**self.notice, "session": "different"})
        with self.assertRaises(ValueError):
            self.packed(sender_endpoint=None)
        with self.assertRaises(ValueError):
            self.packed(kind="receipt", reply_to="orig")
        with self.assertRaises(ValueError):
            self.packed(kind="message", reply_to="orig")
        with self.assertRaises(ValueError):
            relay._packet(self.source, self.letter, {**self.notice, "command": "untrusted"})

    def test_independent_display_names_route_both_ways_using_paired_identity(self):
        for root, local_id, local_name, peer_id, peer_name in (
                (self.source.root, "alpha", "Office", "beta", "CloudHost"),
                (self.dest.root, "beta", "MyWorker", "alpha", "PartnerMachine")):
            file = root / "config.json"
            cfg = json.loads(file.read_text())
            cfg.update(device=local_name, aliases=[local_id], identity_key=local_id,
                       peers={peer_name: {"aliases": [peer_id], "identity_key": peer_id, "mail_transport": "relay"}})
            file.write_text(json.dumps(cfg))
        first, second = relay._Settings(self.source.root), relay._Settings(self.dest.root)
        for source, dest, sender, recipient, wire_names in (
            (first, second, {**self.a, "device": "Office"}, {**self.b, "device": "CloudHost"}, ("alpha", "beta")),
            (second, first, {**self.b, "device": "MyWorker"}, {**self.a, "device": "PartnerMachine"}, ("beta", "alpha"))):
            letter = {**self.letter, "from": sender["device"], "to": recipient["device"], "sender_endpoint": sender,
                      "reply_endpoint": sender, "recipient_identity": recipient, "recipient_endpoint": recipient}
            notice = {"id": letter["id"], "client": recipient["client"], "session": recipient["session"]}
            peer, received, _, _ = relay._unpack(dest, *relay._packet(source, letter, notice))
            self.assertEqual((received["from"], received["to"]), wire_names)
            self.assertEqual((received["reply_endpoint"]["device"], received["recipient_identity"]["device"]), wire_names)
            self.assertTrue(dest.allows(peer, received["recipient_endpoint"]))

    def test_relay_validation_prevents_adapter_substitution_with_shared_chat_id(self):
        sid = "2606bdcc-6a43-41e1-8267-9ac02c5be1ff"
        target = {"device": "beta", "client": "claude-channel", "session": sid, "delivery": "queue"}
        letter = {**self.letter, "recipient_identity": {**self.b, "client": "claude", "session": sid},
                  "recipient_endpoint": target}
        with self.assertRaisesRegex(ValueError, "addressed receiver"):
            relay._packet(self.source, letter, {**self.notice, "client": "claude", "session": sid})

    def test_exact_local_grant_required_even_when_peer_authenticated(self):
        target = {**self.b, "session": "other"}
        topic, wire = relay._packet(self.source, {**self.letter, "recipient_identity": target, "recipient_endpoint": target},
                                    {**self.notice, "session": "other"})
        with patch.object(relay.sessions, "notify") as spy:
            result = relay._receive(self.dest, *relay._unpack(self.dest, topic, wire))
            self.assertEqual(result["inbox_status"], "received")
            self.assertEqual(result["session_notification"]["status"], "requires_receiver_binding")
            spy.assert_not_called()

    def test_concurrent_delivery_keeps_native_result_distinct_from_read(self):
        topic, wire = self.packed()
        args = relay._unpack(self.dest, topic, wire)
        native = {"ok": True, "status": "admitted", "delivery_mode": "send_now",
                  "admission_evidence": "portable_host_ack", "host_placement": "current_turn"}
        with patch.object(relay.sessions, "notify", return_value=native) as spy:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: relay._receive(self.dest, *args), range(2)))
            self.assertEqual(sorted(r["inbox_status"] for r in results), ["duplicate", "received"])
            self.assertEqual(spy.call_count, 1)
            self.assertTrue(spy.call_args.args[1]["steer"])
            receipts = [r["session_notification"].get("delivery_receipt") for r in results]
            self.assertTrue(any(r and r["scope"] == "current_turn" for r in receipts))
            self.assertIsNone(messages.handle(self.dest.root, "wait", {"id": "id-1", "timeout": 0})["message"]["read_at"])
            messages.handle(self.dest.root, "receipt", {"id": "id-1", "reader_endpoint": self.b})
            self.assertEqual(relay._receive(self.dest, *args)["session_notification"]["status"], "already_read")
            self.assertEqual(spy.call_count, 1)

    def test_host_timeout_followed_by_replay_does_not_repeat_turn(self):
        args = relay._unpack(self.dest, *self.packed())
        with patch.object(relay.sessions, "notify", side_effect=TimeoutError("native outcome unknown")) as spy:
            self.assertEqual(relay._receive(self.dest, *args)["session_notification"]["status"], "unknown")
            self.assertTrue(relay._receive(self.dest, *args)["session_notification"]["repeat_suppressed"])
            self.assertEqual(spy.call_count, 1)

    def test_explicit_queue_translates_to_native_option(self):
        args = relay._unpack(self.dest, *self.packed(recipient_endpoint={**self.b, "delivery": "queue"}))
        with patch.object(relay.sessions, "notify", return_value={"ok": True, "status": "queued"}) as spy:
            note = relay._receive(self.dest, *args)["session_notification"]
            self.assertTrue(spy.call_args.args[1]["force_queue"])
            self.assertEqual(note["delivery_receipt"]["scope"], "next_turn")

    def test_receipts_passive_and_unrecognized_correlations_do_not_wake(self):
        with patch.object(relay.sessions, "notify") as spy, patch.object(relay, "_permits_reply", return_value=False):
            receipt = {**self.letter, "id": "receipt", "kind": "receipt", "reply_to": "original"}
            args = relay._unpack(self.dest, *relay._packet(self.source, receipt, None))
            self.assertEqual(relay._receive(self.dest, *args)["session_notification"]["status"], "not_requested")
            args = relay._unpack(self.dest, *self.packed(kind="reply", reply_to="not-our-letter"))
            self.assertEqual(relay._receive(self.dest, *args)["session_notification"]["status"], "unverified_reply_origin")
            spy.assert_not_called()

    def test_requested_report_is_stable_authenticated_and_cannot_start_an_exchange(self):
        from src.runtime import outbox
        outbox.remember(self.source.root, self.letter, delivery_report=True)
        request = relay._unpack(self.dest, *relay._packet(self.source, self.letter, self.notice, delivery_report=True))
        with patch.object(relay.sessions, "notify", return_value={"ok": True, "status": "queued"}) as spy:
            received = relay._receive(self.dest, *request)
            report = received["_report"]
            self.assertEqual(report["id"], relay.report_id("alpha", self.letter["id"]))
            self.assertEqual(json.loads(report["text"]), {"schema": relay.REPORT_SCHEMA, "message_id": "id-1",
                             "inbox_status": "received", "session_status": "queued", "placement": "next_turn"})
            messages.handle(self.dest.root, "receipt", {"id": self.letter["id"], "reader_endpoint": self.b})
            repeated = relay._receive(self.dest, *request)
            self.assertEqual(repeated["_report"], report)
            self.assertEqual(repeated["session_notification"]["status"], "already_read")
            messages.handle(self.source.root, "deliver", report)
            self.assertEqual(outbox.status(self.source.root, "id-1", self.a)["status"], "awaiting_report")
            signed = relay._unpack(self.source, *relay._packet(self.dest, report, None))
            stored = relay._receive(self.source, *signed)
            self.assertEqual(stored["session_notification"]["status"], "not_requested")
            self.assertNotIn("_report", stored)
            self.assertEqual(spy.call_count, 1)
            status = outbox.status(self.source.root, "id-1", self.a)
            self.assertEqual(status["status"], "report_received")
            self.assertTrue(status["session_accepted"])
            with self.assertRaises(ValueError):
                relay._packet(self.dest, report, None, delivery_report=True)
        self.assertFalse(relay.valid_progress("requires_activation", "current_turn"))

    def test_inflight_duplicate_cannot_finalize_an_unknown_delivery_report(self):
        args = relay._unpack(self.dest, *relay._packet(self.source, self.letter, self.notice, delivery_report=True))
        started, proceed = threading.Event(), threading.Event()
        def native(root, notice):
            started.set()
            proceed.wait(5)
            return {"ok": True, "status": "queued"}
        with ThreadPoolExecutor(1) as pool, patch.object(relay.sessions, "notify", side_effect=native) as spy:
            first = pool.submit(relay._receive, self.dest, *args)
            try:
                self.assertTrue(started.wait(5))
                other = relay._receive(self.dest, *args)
                self.assertTrue(other["session_notification"]["awaiting_native_result"])
                self.assertNotIn("_report", other)
            finally:
                proceed.set()
            self.assertEqual(json.loads(first.result()["_report"]["text"])["session_status"], "queued")
            self.assertEqual(spy.call_count, 1)

    def test_follow_reuses_subscription_and_backs_off_on_disconnection(self):
        class EndTest(Exception):
            pass

        connections, events = [], []
        async def run(root, consume):
            connections.append(root)
            if len(connections) < 3:
                raise relay._RelayUnavailable("disconnected")
            return await consume(None, None)

        one = {"id": "a", "inbox_status": "received"}
        two = {"id": "b", "inbox_status": "duplicate"}
        with patch.object(relay, "_run", side_effect=run), patch.object(asyncio, "sleep") as sleeps, \
             patch.object(relay, "_consumer") as subscribe, \
             patch.object(relay, "_next", side_effect=[None, one, None, two, EndTest()]), self.assertRaises(EndTest):
            relay.follow(self.source.root, events.append)
        self.assertEqual(subscribe.await_count, 1)
        self.assertEqual([c.args[0] for c in sleeps.await_args_list], [1, 2])
        self.assertEqual([e["status"] for e in events], ["disconnected", "disconnected", "received", "duplicate"])

    def test_external_broker_must_use_tls(self):
        file = self.source.root / "config.json"
        data = json.loads(file.read_text())
        for url in ("nats://10.0.0.1", "nats://localhost", "tls://user:password@broker"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                data["session_relay"]["url"] = url
                file.write_text(json.dumps(data))
                relay._Settings(self.source.root)
        data["session_relay"]["url"] = "tls://broker.example"
        file.write_text(json.dumps(data))
        self.assertTrue(relay._Settings(self.source.root).options["tls_handshake_first"])


if __name__ == "__main__":
    unittest.main()
