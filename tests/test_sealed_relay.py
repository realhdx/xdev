"""Broker-visible wire format and admission policy for encrypted device pairs."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from src.runtime import outbox, session_relay as relay
from src.runtime.letter_attachment import make_attachment

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESSIV
except ImportError:
    AESSIV = None


class SealedMailTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.key = secrets.token_bytes(32)
        (self.base / "pair.key").write_text(self.key.hex())
        (self.base / "password").write_text("test-only")
        self.a = {"device": "alpha", "client": "drafting", "session": "draft-session-private"}
        self.b = {"device": "beta", "client": "reviewing", "session": "review-session-private"}
        for node, other in (("alpha", "beta"), ("beta", "alpha")):
            root = self.base / node
            root.mkdir()
            config = {"device": node, "peers": {other: {"mail_transport": "relay"}},
                      "session_relay": {"user": node, "url": "nats://127.0.0.1:4222",
                                        "password_file": str(self.base / "password"),
                                        "peers": {other: {"key_file": str(self.base / "pair.key"),
                                                          "encryption": "required"}}}}
            (root / "config.json").write_text(json.dumps(config))
        self.source, self.dest = [relay._Settings(self.base / node) for node in ("alpha", "beta")]
        self.original = {"id": "private-first-letter", "from": "beta", "to": "alpha",
                         "subject": "private original subject", "text": "private attachment material"}
        self.letter = {"envelope_version": 2, "id": "private-test-id", "from": "alpha", "to": "beta",
                       "subject": "confidential topic", "text": "private challenge " + secrets.token_hex(12),
                       "sender_endpoint": self.a, "recipient_identity": self.b, "recipient_endpoint": self.b,
                       "reply_endpoint": self.a, "letter_attachment": make_attachment(self.original)}
        self.notice = {**{k: self.b[k] for k in ("client", "session")}, "id": self.letter["id"]}

    def packet(self, **changes):
        return relay._packet(self.source, {**self.letter, **changes}, self.notice, delivery_report=True)

    @unittest.skipUnless(AESSIV, "Optional AES-SIV dependency required")
    def test_network_publication_hides_the_complete_letter_and_preserves_dedup(self):
        broker = AsyncMock()
        for letter in (self.letter, self.letter, {**self.letter, "text": "different challenge"}):
            asyncio.run(relay._publish(self.source, broker, letter, self.notice, delivery_report=True))
        first, replay, conflict = broker.publish.await_args_list
        self.assertEqual(first, replay)
        self.assertNotEqual(first.kwargs["headers"], conflict.kwargs["headers"])
        topic, wire = first.args
        self.assertEqual(topic, "xdev.session.v1.beta.alpha")
        self.assertEqual(first.kwargs["headers"], {"Nats-Msg-Id": hashlib.sha256(wire).hexdigest()})
        self.assertTrue(wire.startswith(relay.SEALED_PREFIX))
        for hidden in [self.letter["text"], self.letter["subject"], self.letter["id"],
                       self.original["text"], self.original["id"], self.a["session"], self.b["session"],
                       self.b["client"], "request_report", "notification"]:
            self.assertNotIn(hidden.encode(), wire)
        self.assertEqual(relay._unpack(self.dest, topic, wire), ("alpha", self.letter, self.notice, True))
        relay._receive(self.dest, *relay._unpack(self.dest, topic, wire))
        with self.assertRaises(ValueError):
            relay._receive(self.dest, *relay._unpack(self.dest, *conflict.args))

    @unittest.skipUnless(AESSIV, "Optional AES-SIV dependency required")
    def test_encrypted_report_round_trip_keeps_subject_and_status_private(self):
        outbox.remember(self.source.root, self.letter, delivery_report=True)
        received = relay._receive(self.dest, *relay._unpack(self.dest, *self.packet()))
        report = received["_report"]
        topic, wire = relay._packet(self.dest, report, None)
        self.assertEqual(topic, "xdev.session.v1.alpha.beta")
        for hidden in (relay.REPORT_SCHEMA, report["id"], report["subject"], "requires_receiver_binding"):
            self.assertNotIn(hidden.encode(), wire)
        status = relay._receive(self.source, *relay._unpack(self.source, topic, wire))
        self.assertEqual(status["session_notification"]["status"], "not_requested")
        self.assertEqual(outbox.status(self.source.root, self.letter["id"], self.a)["report"]["session_status"],
                         "requires_receiver_binding")

    @unittest.skipUnless(AESSIV, "Optional AES-SIV dependency required")
    def test_wrong_key_tamper_truncation_reflection_and_downgrade(self):
        topic, wire = self.packet()
        for invalid in (wire[:-1], wire[:len(relay.SEALED_PREFIX)],
                        wire[:-1] + bytes([wire[-1] ^ 1]), wire + b"x"):
            with self.assertRaises(ValueError):
                relay._unpack(self.dest, topic, invalid)
        with self.assertRaises(ValueError):
            relay.open_sealed_mail(secrets.token_bytes(32), "alpha", "beta", wire)
        with self.assertRaises(ValueError):
            relay.open_sealed_mail(self.key, "beta", "alpha", wire)
        bound = relay.seal_mail(self.key, "alpha", "beta", b"carrier test", context=b"xdev.sqs-mail.v1/queue-one")
        self.assertEqual(relay.open_sealed_mail(self.key, "alpha", "beta", bound,
                                               context=b"xdev.sqs-mail.v1/queue-one"), b"carrier test")
        for other_context in (b"", b"xdev.sqs-mail.v1/queue-two"):
            with self.assertRaises(ValueError):
                relay.open_sealed_mail(self.key, "alpha", "beta", bound, context=other_context)
        self.source.encrypted_peers.clear()
        with self.assertRaisesRegex(ValueError, "encrypted"):
            relay._unpack(self.dest, *self.packet())
        self.dest.encrypted_peers.clear()
        with self.assertRaisesRegex(ValueError, "both endpoints"):
            relay._unpack(self.dest, topic, wire)
        self.assertEqual(relay._unpack(self.dest, *self.packet()), ("alpha", self.letter, self.notice, True))

    def test_malformed_settings_cannot_silently_turn_off_encryption(self):
        path = self.source.root / "config.json"
        config = json.loads(path.read_text())
        for value in ("require", "auto", False, None):
            config["session_relay"]["peers"]["beta"]["encryption"] = value
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "required"):
                relay._Settings(self.source.root)

    def test_missing_backend_cannot_publish_as_plaintext(self):
        broker = AsyncMock()
        with patch.dict(sys.modules, {"cryptography.exceptions": None}), self.assertRaisesRegex(RuntimeError, "AES-SIV"):
            asyncio.run(relay._publish(self.source, broker, self.letter, self.notice))
        broker.publish.assert_not_awaited()

    @unittest.skipUnless(AESSIV, "Optional AES-SIV dependency required")
    @unittest.skipUnless(os.environ.get("XDEV_TEST_NATS_SERVER"), "Set XDEV_TEST_NATS_SERVER for a real JetStream")
    def test_real_broker_operator_reads_ciphertext_while_endpoints_exchange_reports(self):
        import nats
        from nats.js.api import ConsumerConfig
        from nats.js.errors import NotFoundError

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"nats://127.0.0.1:{port}"
        admin = secrets.token_hex(32)
        users = [{"user": "admin", "password": admin}]
        for endpoint in (self.source, self.dest):
            path = endpoint.root / "config.json"
            cfg = json.loads(path.read_text())
            pwd = secrets.token_hex(32)
            password_file = endpoint.root / "test.password"
            password_file.write_text(pwd)
            cfg["session_relay"].update(url=url, password_file=str(password_file))
            path.write_text(json.dumps(cfg))
            users.append({"user": endpoint.local, "password": pwd})
        conf = self.base / "broker.conf"
        conf.write_text(json.dumps({"listen": f"127.0.0.1:{port}", "authorization": {"users": users},
                                    "jetstream": {"store_dir": str(self.base / "storage")}}))
        with subprocess.Popen([os.environ["XDEV_TEST_NATS_SERVER"], "-c", str(conf)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as broker:
            try:
                for _ in range(100):
                    if broker.poll() is not None:
                        self.fail("Test JetStream terminated during startup")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.05)
                else:
                    self.fail("Test JetStream did not open the loopback listener")

                async def exchange():
                    nc = await nats.connect(url, user="admin", password=admin)
                    try:
                        js = nc.jetstream()
                        await js.add_stream(name=relay.STREAM, subjects=[relay.PREFIX + ".*.*"], storage="file",
                                            retention="workqueue", max_age=5, duplicate_window=2)
                        for node in ("alpha", "beta"):
                            await js.add_consumer(relay.STREAM, ConsumerConfig(
                                durable_name="xdev-" + node, filter_subject=f"{relay.PREFIX}.{node}.*", ack_policy="explicit"))
                        outbox.remember(self.source.root, self.letter, delivery_report=True)
                        for duplicate in (False, True):
                            sent = await asyncio.to_thread(relay.publish, self.source.root, self.letter, self.notice,
                                                           delivery_report=True)
                            self.assertEqual(sent["duplicate"], duplicate)
                        stored = await js.get_msg(relay.STREAM, seq=sent["stream_seq"])
                        self.assertEqual(stored.data, self.packet()[1])
                        for marker in (self.letter["text"], self.original["text"], self.b["session"],
                                       self.letter["subject"], self.letter["id"], self.b["client"]):
                            self.assertNotIn(marker.encode(), stored.data)
                        incoming = await asyncio.to_thread(relay.listen, self.dest.root, 1, 3)
                        self.assertEqual(incoming["results"][0]["inbox_status"], "received")
                        report = await js.get_msg(relay.STREAM, subject=f"{relay.PREFIX}.alpha.beta")
                        self.assertTrue(report.data.startswith(relay.SEALED_PREFIX))
                        self.assertNotIn(relay.REPORT_SCHEMA.encode(), report.data)
                        await asyncio.to_thread(relay.listen, self.source.root, 1, 3)
                        self.assertEqual(outbox.status(self.source.root, self.letter["id"], self.a)["status"], "report_received")
                        # A WorkQueue ACK removes the stored original and its report.
                        for seq in (sent["stream_seq"], report.seq):
                            with self.assertRaises(NotFoundError):
                                await js.get_msg(relay.STREAM, seq=seq)
                        offline = {**self.letter, "id": "private-unread"}
                        offline_notice = {**self.notice, "id": offline["id"]}
                        expired = await asyncio.to_thread(relay.publish, self.source.root, offline, offline_notice)
                        self.assertTrue((await js.get_msg(relay.STREAM, seq=expired["stream_seq"])).data.startswith(relay.SEALED_PREFIX))
                        # Without a receiver or ACK, the message expires at the configured maximum age.
                        deadline = time.monotonic() + 9
                        while time.monotonic() < deadline:
                            try:
                                await js.get_msg(relay.STREAM, seq=expired["stream_seq"])
                            except NotFoundError:
                                break
                            await asyncio.sleep(.2)
                        else:
                            self.fail("Unread packet remained accessible beyond the test retention window")
                    finally:
                        await nc.close()
                asyncio.run(exchange())
            finally:
                broker.terminate()
