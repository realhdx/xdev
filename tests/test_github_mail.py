"""Exercise the GitHub wire contract without a token, network or model."""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.runtime import github_mail as gh, outbox
from src.runtime.letter_attachment import make_attachment


class FakeAPI:
    def __init__(self):
        self.repo = {"full_name": "Example/Private-Mail", "id": 555, "private": True, "visibility": "private"}
        self.release = {"id": 77, "immutable": False}
        self.assets = {}
        self.calls = []
        self.lose_upload = False
        self.redirect = "https://release-assets.githubusercontent.com/signed?value=opaque"

    def http(self, host, verb, path, headers, body=None, **kw):
        self.calls.append((host, verb, path, dict(headers)))
        if host == "release-assets.githubusercontent.com":
            assert "Authorization" not in headers
            return 200, {}, self.current
        assert headers["Authorization"] == "Bearer test-token"
        if host == gh.UPLOAD:
            name = path.split("name=")[1]
            if any(a["name"] == name for a, _ in self.assets.values()):
                return 422, {}, b"{}"
            idx = len(self.calls) + 101
            item = {"id": idx, "name": name, "state": "uploaded", "size": len(body),
                    "created_at": datetime.now(timezone.utc).isoformat(), "digest": "sha256:" + hashlib.sha256(body).hexdigest()}
            self.assets[idx] = (item, body)
            if self.lose_upload:
                raise gh.Unavailable("lost after storage")
            return 201, {}, json.dumps(item).encode()
        if "/releases/assets/" in path:
            idx = int(path.rsplit("/", 1)[1])
            if idx not in self.assets:
                return 404, {}, b""
            if verb == "DELETE":
                del self.assets[idx]
                return 204, {}, b""
            _, self.current = self.assets[idx]
            return 302, {"location": self.redirect}, b""
        if "/assets?" in path:
            result = [a for a, _ in self.assets.values()]
        elif path.endswith("/releases/77"):
            result = self.release
        else:
            result = self.repo
        data = json.dumps(result).encode()
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        if headers.get("If-None-Match") == etag:
            return 304, {}, b""
        return 200, {"etag": etag}, data


class GitHubMail(unittest.TestCase):
    def setUp(self):
        parent = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token, pair = parent / "token", parent / "key"
        token.write_text("test-token")
        pair.write_text(secrets.token_hex(32))
        self.addresses = {d: {"device": d, "client": "reviewer", "session": "secret-room-" + d}
                          for d in ("alpha", "beta")}
        self.sites = {}
        for node, peer in (("alpha", "beta"), ("beta", "alpha")):
            base = parent / node
            base.mkdir()
            conf = {"device": node, "peers": {peer: {"mail_transport": "github"}},
                    "github_mail": {"repository": "Example/Private-Mail", "repository_id": 555, "release_id": 77,
                                    "token_file": str(token), "retention_seconds": 60,
                                    "peers": {peer: {"key_file": str(pair), "receiver_apps": ["reviewer"]}}}}
            (base / "config.json").write_text(json.dumps(conf))
            self.sites[node] = base
        self.letter = {"envelope_version": 2, "id": "secret-letter-id", "from": "alpha", "to": "beta",
                       "sender_endpoint": self.addresses["alpha"], "reply_endpoint": self.addresses["alpha"],
                       "recipient_identity": self.addresses["beta"], "recipient_endpoint": self.addresses["beta"],
                       "text": "secret-body", "subject": "secret-subject", "letter_attachment": make_attachment(
                           {"id": "quoted-id", "from": "alpha", "to": "beta", "text": "private-attachment"})}
        self.fake = FakeAPI()
        self.enterContext(patch.object(gh, "_http", self.fake.http))

    def send(self, **kwargs):
        return gh.publish(self.sites["alpha"], self.letter, **kwargs)

    def receive(self, node="beta"):
        return gh.listen(self.sites[node], limit=1, timeout=1)["results"]

    def test_cipher_only_delivery_reporting_and_deletion(self):
        outbox.remember(self.sites["alpha"], self.letter, delivery_report=True)
        sent = self.send(delivery_report=True)
        self.assertEqual(sent["status"], "queue_acknowledged")
        self.assertIsNone(sent["session_accepted"])
        meta, wire = next(iter(self.fake.assets.values()))
        observer = json.dumps(meta).encode() + wire
        for secret in ("secret-letter-id", "secret-room", "secret-body", "secret-subject", "private-attachment",
                       "alpha", "beta", "reviewer"):
            self.assertNotIn(secret.encode(), observer)
        self.assertEqual(wire[8:8 + len(gh.mail.SEALED_PREFIX)], gh.mail.SEALED_PREFIX)
        got, = self.receive()
        self.assertIn(got["inbox_status"], {"received", "delivered"})
        self.assertNotIn(meta["id"], self.fake.assets)
        ack, = self.receive("alpha")
        self.assertEqual(ack["id"], got["report_id"])
        self.assertFalse(self.fake.assets)
        self.assertEqual(outbox.status(self.sites["alpha"], self.letter["id"], self.addresses["alpha"])["status"],
                         "report_received")
        self.assertEqual(self.send(delivery_report=True)["provider_message_id"], sent["provider_message_id"])
        self.assertFalse(self.fake.assets)

    def test_one_slot_is_released_before_a_delivery_report_upload(self):
        for root in self.sites.values():
            path = root / "config.json"
            config = json.loads(path.read_text())
            config["github_mail"]["max_pending_assets"] = 1
            path.write_text(json.dumps(config))
        outbox.remember(self.sites["alpha"], self.letter, delivery_report=True)
        original = self.send(delivery_report=True)["provider_message_id"]
        received, = self.receive()
        self.assertEqual(received["report_status"], "queue_acknowledged")
        deleted = next(i for i,c in enumerate(self.fake.calls) if c[1]=="DELETE" and c[2].endswith("/"+original))
        sends = [i for i,c in enumerate(self.fake.calls) if c[1]=="POST"]
        self.assertEqual(len(sends),2)
        self.assertLess(deleted,sends[1])
        self.assertEqual(self.receive("alpha")[0]["id"],received["report_id"])
        self.assertFalse(self.fake.assets)

    def test_report_is_durable_before_cleanup_and_resumes_after_restart(self):
        a,b = self.sites["alpha"],self.sites["beta"]
        outbox.remember(a,self.letter,delivery_report=True)
        self.send(delivery_report=True)
        original_delete = gh.Api.delete
        def lost_delete_response(api,box,aid):
            with closing(gh._journal(b)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pending_reports").fetchone()[0],1)
            original_delete(api,box,aid)
            raise gh.Unavailable("process lost its deletion acknowledgment")
        with patch.object(gh.Api,"delete",lost_delete_response), self.assertRaises(gh.Unavailable):
            self.receive()
        self.assertFalse(self.fake.assets)
        receiver = gh.Settings(b)
        gh._collect(gh.Api(b),receiver,1,{})
        self.assertEqual(self.receive("alpha")[0]["id"], gh.mail.report_id("alpha",self.letter["id"]))
        gh._collect(gh.Api(b),receiver,1,{})
        self.assertEqual(sum(c[1]=="POST" for c in self.fake.calls),2)
        self.assertEqual(len(gh.events(b)["results"]),1)

    def test_no_report_requested_and_report_window_expires_without_an_upload(self):
        self.send()
        first, = self.receive()
        self.assertNotIn("report_id",first)
        self.assertEqual(sum(c[1]=="POST" for c in self.fake.calls),1)
        a,b=self.sites["alpha"],self.sites["beta"]
        self.letter={**self.letter,"id":"test-expiring-receipt"}
        outbox.remember(a,self.letter,delivery_report=True)
        self.send(delivery_report=True)
        with patch.object(gh,"_flush_reports",return_value=set()):
            received,=self.receive()
            self.assertEqual(received["report_status"],"pending")
        self.assertFalse(self.fake.assets)
        with patch("time.time",return_value=time.time()+61):
            gh._collect(gh.Api(b),gh.Settings(b),1,{})
        self.assertFalse(self.fake.assets)
        with closing(gh._journal(b)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pending_reports").fetchone()[0],0)

    def test_pending_report_obeys_retry_after_and_lost_ack_does_not_publish_again(self):
        a,b=self.sites["alpha"],self.sites["beta"]
        outbox.remember(a,self.letter,delivery_report=True)
        self.send(delivery_report=True)
        start = int(time.time())
        with patch("time.time",return_value=start), patch.object(gh,"_send",side_effect=gh.Unavailable("wait",30)):
            item, = self.receive()
        self.assertEqual(item["report_status"],"pending")
        with patch("time.time",return_value=start+3), patch.object(gh,"_send",side_effect=AssertionError):
            gh._collect(gh.Api(b),gh.Settings(b),1,{})
        self.fake.lose_upload=True
        with patch("time.time",return_value=start+31):
            gh._collect(gh.Api(b),gh.Settings(b),1,{})
        self.fake.lose_upload=False
        linked,=self.receive("alpha")
        self.assertEqual(linked["id"],item["report_id"])
        with patch("time.time",return_value=start+53):
            gh._collect(gh.Api(b),gh.Settings(b),1,{})
        self.assertEqual(sum(c[1]=="POST" for c in self.fake.calls),2)

    def test_lost_ack_same_id_reconciles_same_bytes_and_cannot_replace(self):
        self.fake.lose_upload = True
        with self.assertRaises(gh.Unavailable):
            self.send()
        first = next(iter(self.fake.assets))
        self.assertEqual(self.send()["provider_message_id"], str(first))
        self.letter["text"] += " changed"
        with self.assertRaisesRegex(ValueError, "different content"):
            self.send()
        self.assertEqual(sum(v == "POST" for _, v, _, _ in self.fake.calls), 1)

    def test_lost_ack_removed_before_recheck_never_reposts(self):
        self.fake.lose_upload = True
        with self.assertRaises(gh.Unavailable):
            self.send()
        self.receive()
        with self.assertRaises(gh.Unavailable):
            self.send()
        self.assertFalse(self.fake.assets)
        self.assertEqual(sum(v == "POST" for _, v, _, _ in self.fake.calls), 1)

    def test_reject_wrong_github_identity_before_submission(self):
        for change in ({"visibility": "public"}, {"private": False}, {"full_name": "moved/name"}, {"id": 33}):
            with self.subTest(change=change), patch.dict(self.fake.repo, change):
                with self.assertRaises(ValueError):
                    self.send()
        self.fake.release["immutable"] = True
        with self.assertRaises(ValueError):
            self.send()
        self.assertFalse(self.fake.assets)

    def test_tampered_expiration_is_not_authorized_and_later_replay_expires(self):
        self.send()
        idx, (meta, wire) = next(iter(self.fake.assets.items()))
        modified = bytes([wire[0] ^ 1]) + wire[1:]
        self.fake.assets[idx] = {**meta, "digest": "sha256:" + hashlib.sha256(modified).hexdigest()}, modified
        refused, = self.receive()
        self.assertEqual((refused["status"], refused["reason"]), ("rejected", "envelope_rejected"))
        self.assertIn(idx, self.fake.assets)
        self.fake.assets[idx] = meta, wire
        with patch.object(gh.time, "time", return_value=time.time() + 65):
            self.assertEqual(self.receive()[0]["status"], "expired")
        self.assertFalse(self.fake.assets)

    def test_follow_retries_the_same_partial_download_without_restart_or_resend(self):
        self.send()
        idx, (meta, wire) = next(iter(self.fake.assets.items()))
        self.fake.assets[idx] = meta, wire[:-1]
        now = time.time()
        observed = []

        def sleep(seconds):
            nonlocal now
            if any(x.get("event") == "session_mail_available" for x in observed):
                raise KeyboardInterrupt()
            self.assertLess(len(observed), 3, observed)
            now += seconds
            self.fake.assets[idx] = meta, wire

        with patch.object(gh.time, "time", side_effect=lambda: now), patch.object(gh.time, "sleep", sleep):
            with self.assertRaises(KeyboardInterrupt):
                gh.follow(self.sites["beta"], observed.append)
        self.assertEqual(observed[0], {"transport": "github", "status": "asset_pending", "reason": "incomplete_download"})
        self.assertEqual(observed[-1]["event"], "session_mail_available")
        self.assertFalse(self.fake.assets)
        self.assertEqual(sum(verb == "POST" for _, verb, _, _ in self.fake.calls), 1)

    def test_same_size_stale_bytes_and_missing_object_get_a_short_retry(self):
        self.send()
        idx, (meta, wire) = next(iter(self.fake.assets.items()))
        self.fake.assets[idx] = meta, b"?" * len(wire)
        deferred, = self.receive()
        self.assertEqual((deferred["status"], deferred["reason"]), ("asset_pending", "download_digest_mismatch"))
        with patch.object(gh.Api, "download", return_value=None):
            deferred, = self.receive()
        self.assertEqual(deferred["reason"], "asset_not_visible")
        self.assertTrue(self.fake.assets)

    def test_conditional_asset_list_periodically_requires_a_fresh_representation(self):
        api = gh.Api()
        box = gh.Settings(self.sites["beta"]).inbox
        with patch.object(gh.time, "monotonic", return_value=100):
            self.assertEqual(api.assets(box), [])
        self.send()
        with patch.object(gh.time, "monotonic", return_value=401):
            self.assertEqual(len(api.assets(box)), 1)
        self.assertNotIn("If-None-Match", self.fake.calls[-1][3])
        self.assertEqual(self.fake.calls[-1][3]["Cache-Control"], "no-cache")

    def test_download_redirect_and_scope(self):
        self.send()
        self.fake.redirect = "https://release-assets.githubusercontent.com.example/signed"
        self.assertEqual(self.receive()[0]["status"], "rejected")
        self.assertFalse(any(".example" in host for host, _, _, _ in self.fake.calls))
        self.fake.redirect = "https://release-assets.githubusercontent.com/signed"
        root = self.sites["beta"]
        settings = json.loads((root/"config.json").read_bytes())
        settings["github_mail"]["peers"]["alpha"]["receiver_apps"] = []
        (root/"config.json").write_text(json.dumps(settings))
        self.assertEqual(self.receive()[0]["status"], "rejected")
        self.assertTrue(self.fake.assets)

    def test_etag_and_retry_after(self):
        api = gh.Api()
        box = gh.Settings(self.sites["alpha"]).inbox
        api.inspect(box)
        api.inspect(box)
        self.assertTrue(any("If-None-Match" in h for _, _, _, h in self.fake.calls))
        with self.assertRaises(gh.Unavailable) as failure:
            gh._status(403, {"retry-after": "37"}, {200})
        self.assertGreaterEqual(failure.exception.retry_after, 37)

    def test_read_only_reconciliation_even_without_the_local_journal(self):
        root = self.sites["alpha"]
        unknown = gh.reconcile(root, self.letter)
        self.assertEqual(unknown["status"], "submission_unconfirmed")
        self.assertFalse(self.fake.calls)
        self.fake.lose_upload = True
        with self.assertRaises(gh.Unavailable):
            self.send()
        self.fake.calls.clear()
        self.assertEqual(gh.reconcile(root, self.letter)["status"], "queue_acknowledged")
        self.assertTrue(all(verb == "GET" for _, verb, _, _ in self.fake.calls))
        (root / "state/github-mail.sqlite3").unlink()
        self.assertFalse(gh.reconcile(root, self.letter)["ok"])

    def test_only_one_concurrent_sender_can_issue_post(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            if args[1] == "POST":
                entered.set()
                if not release.wait(7):
                    raise RuntimeError("timed out")
            return self.fake.http(*args, **kwargs)
        with patch.object(gh, "_http", delayed), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.send)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(gh.Unavailable):
                    self.send()
            finally:
                release.set()
            self.assertTrue(first.result()["ok"])
        self.assertEqual(sum(v == "POST" for _, v, _, _ in self.fake.calls), 1)

    def test_sender_online_cleans_expiry_without_waking_or_touching_unrelated_assets(self):
        self.send()
        idx, (meta, wire) = next(iter(self.fake.assets.items()))
        self.fake.assets[900] = ({**meta, "name": "release-notes.zip", "id": 900}, wire)
        with patch.object(gh.time, "time", return_value=time.time() + 65):
            self.assertEqual(self.receive("alpha"), [])
        self.assertNotIn(idx, self.fake.assets)
        self.assertIn(900, self.fake.assets)

    def test_uncorrelated_receipt_and_device_only_letter_fail_closed(self):
        self.letter["kind"] = "receipt"
        self.letter["reply_to"] = "not-an-original"
        with self.assertRaisesRegex(ValueError, "delivery reports"):
            self.send()
        del self.letter["kind"], self.letter["reply_to"], self.letter["recipient_identity"]
        with self.assertRaisesRegex(ValueError, "recipient chat"):
            self.send()
        self.assertFalse(self.fake.assets)

    def test_both_legacy_wake_and_replay_only_raise_one_safe_local_event(self):
        notice = {"id": self.letter["id"], "client": "reviewer", "session": self.addresses["beta"]["session"]}
        self.send(notification=notice)
        aid, (meta, current) = next(iter(self.fake.assets.items()))
        source, dest = [gh.Settings(self.sites[n]) for n in ("alpha", "beta")]
        topic = f"{gh.mail.PREFIX}.{dest.local}.{source.local}"
        unpacked = gh.mail._unpack(dest, topic, current[8:], context=dest.inbox.context + current[:8])
        self.assertIsNone(unpacked[2], "the upgraded sender must protect an older receiver")
        _, legacy = gh.mail._packet(source, self.letter, notice, context=dest.inbox.context + current[:8])
        old_wire = current[:8] + legacy
        self.fake.assets[aid] = ({**meta, "size": len(old_wire), "digest": "sha256:"+hashlib.sha256(old_wire).hexdigest()}, old_wire)
        original = dict(self.fake.assets)
        with patch.object(gh.mail.sessions, "notify", side_effect=AssertionError("cloud wake")) as receiver:
            first, = self.receive()
            self.fake.assets.update(original)
            second, = self.receive()
        receiver.assert_not_called()
        self.assertEqual(second["inbox_status"], "duplicate")
        self.assertTrue(first["event_created"])
        self.assertFalse(second["event_created"])
        log = gh.events(self.sites["beta"])
        self.assertEqual(log["results"], [first["local_event"]])
        self.assertEqual(gh.events(self.sites["beta"], log["next_after"])["results"], [])
        self.assertEqual(set(first["local_event"]), {"transport", "event", "event_id", "inbox_seq",
                                                   "delivery_scope", "model_woken"})
        self.assertFalse(first["local_event"]["model_woken"])

    def test_cloud_cannot_opt_into_full_host_wake(self):
        root = self.sites["beta"]
        profile = json.loads((root / "config.json").read_bytes())
        profile["github_mailbox"] = profile.pop("github_mail")
        profile["github_mailbox"]["delivery_scope"] = "mailbox"
        (root / "config.json").write_text(json.dumps(profile))
        self.assertEqual(gh.Settings(root).delivery_scope, "mailbox")
        profile["github_mailbox"]["delivery_scope"] = "current_turn"
        (root / "config.json").write_text(json.dumps(profile))
        with self.assertRaises(ValueError):
            gh.Settings(root)

    def test_previous_release_lost_ack_can_reconcile_without_resubmitting_legacy_wake(self):
        root = self.sites["alpha"]
        source = gh.Settings(root)
        notice = {"id": self.letter["id"], "client": "reviewer", "session": self.addresses["beta"]["session"]}
        self.send(notification=notice)
        aid, (meta, wire) = next(iter(self.fake.assets.items()))
        name = meta["name"]
        _, payload = gh.mail._packet(source, self.letter, notice, context=source.inbox.context + wire[:8])
        previous = wire[:8] + payload
        digest = hashlib.sha256(previous).hexdigest()
        self.fake.assets[aid] = ({**meta, "size": len(previous), "digest": "sha256:" + digest}, previous)
        from contextlib import closing
        with closing(gh._journal(root)) as db, db:
            db.execute("UPDATE uploads SET digest=?,asset=NULL WHERE name=?", (digest, name))
        self.fake.calls.clear()
        self.assertTrue(gh.reconcile(root, self.letter, notice)["ok"])
        self.assertTrue(all(verb == "GET" for _, verb, _, _ in self.fake.calls))
        # A previous release that reserved but demonstrably never attempted the POST can migrate
        # to safe bytes. Only a new download on the upgraded recipient can attest their contents.
        self.fake.assets.clear()
        with closing(gh._journal(root)) as db, db:
            db.execute("UPDATE uploads SET attempted=0, asset=NULL WHERE name=?", (name,))
        self.send(notification=notice)
        _, new_wire = next(iter(self.fake.assets.values()))
        dest = gh.Settings(self.sites["beta"])
        topic = f"{gh.mail.PREFIX}.{dest.local}.{source.local}"
        self.assertIsNone(gh.mail._unpack(dest, topic, new_wire[8:], context=dest.inbox.context + new_wire[:8])[2])

    def test_provider_cooldown_survives_a_new_client(self):
        root = self.sites["alpha"]
        box = gh.Settings(root).inbox
        with patch.object(gh, "_http", return_value=(429, {"retry-after": "120"}, b"{}")) as limited:
            with self.assertRaises(gh.Unavailable):
                gh.Api(root).inspect(box)
            with self.assertRaises(gh.Unavailable) as later:
                gh.Api(root).inspect(box)
        self.assertEqual(limited.call_count, 1)
        self.assertGreaterEqual(later.exception.retry_after, 120)

    def test_only_the_original_waiting_caller_receives_a_tool_reply(self):
        root = self.sites["alpha"]
        self.letter["reply_endpoint"] = {"device": "alpha", "channel": "tool_result",
                                         "request_id": self.letter["id"]}
        outbox.remember(root, self.letter)
        profile = json.loads((root / "config.json").read_bytes())
        profile["github_mail"]["peers"]["beta"]["receiver_apps"] = []
        (root / "config.json").write_text(json.dumps(profile))
        reply = {"envelope_version": 2, "kind": "reply", "id": "answer", "reply_to": self.letter["id"],
                 "sender_endpoint": self.addresses["beta"], "recipient_identity": self.addresses["alpha"],
                 "from": "beta", "to": "alpha", "subject": "QA reply", "text": "result"}
        gh.publish(self.sites["beta"], reply)
        with patch.object(gh.mail.sessions, "notify") as notify:
            self.assertEqual(self.receive("alpha")[0]["session_notification"]["status"], "not_requested")
            forged = {**reply, "id": "wrong", "recipient_identity": {**self.addresses["alpha"], "session": "other"}}
            gh.publish(self.sites["beta"], forged)
            self.assertEqual(self.receive("alpha")[0]["status"], "rejected")
        notify.assert_not_called()
