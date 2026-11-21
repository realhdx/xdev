"""Silent correlated control frames on a private GitHub mailbox."""

import json
import time
from contextlib import closing
import unittest
from unittest.mock import patch

import test_github_mail as fixtures
from src.runtime import github_mail as gh, mail_control, outbox


class MailControlTests(unittest.TestCase):
    setUp = fixtures.GitHubMail.setUp

    def payload(self, op="recall"):
        data = {"op": op, "actor": self.addresses["alpha"], "target": self.addresses["beta"]}
        if op != "preflight":
            data["id"] = self.letter["id"]
        return data

    def poll(self, node):
        return gh._collect(gh.Api(self.sites[node]), gh.Settings(self.sites[node]), 10, {})

    def deliver_original(self):
        outbox.remember(self.sites["alpha"], self.letter)
        gh.publish(self.sites["alpha"], self.letter)
        self.poll("beta")

    def test_recall_requires_a_real_receiver_decision_and_never_wakes_a_model(self):
        self.deliver_original()
        req = self.payload()
        a, b = self.sites["alpha"], self.sites["beta"]
        initial = gh.events(b)
        pending = mail_control.submit(a, "beta", req)
        self.assertEqual(pending["status"], "requested")
        self.assertNotIn("evidence", pending)
        _, wire = next(iter(self.fake.assets.values()))
        self.assertNotIn(self.letter["id"].encode(), wire)
        self.assertNotIn(b"secret-room", wire)
        self.assertEqual(self.poll("beta")[0]["status"], "control_processed")
        self.assertEqual(self.poll("alpha")[0]["status"], "control_result_received")
        result = mail_control.latest(a, "beta", req)
        self.assertEqual(result["evidence"]["status"], "recalled")
        self.assertNotIn("secret-body", json.dumps(result))
        self.assertEqual(gh.events(b), initial)
        calls = len(self.fake.calls)
        self.assertEqual(mail_control.submit(a, "beta", req), result)
        self.assertEqual(len(self.fake.calls), calls)

    def test_report_cannot_be_injected_by_another_peer_or_chat(self):
        self.deliver_original()
        req = self.payload("status")
        sent = mail_control.submit(self.sites["alpha"], "beta", req)
        rid = sent["request_id"]
        forged = {"from":"gamma","to":"alpha","id":"mc-a-" + rid[5:], "request":rid,
                  "result":{"ok":True,"status":"recalled"}}
        with self.assertRaisesRegex(ValueError, "Unsolicited"):
            mail_control.accept(gh.Settings(self.sites["alpha"]), "gamma", forged)
        for k in ("actor", "target"):
            with self.subTest(k=k), self.assertRaisesRegex(ValueError,"No original letter"):
                mail_control.latest(self.sites["alpha"], "beta", {**req,k:{**req[k],"session":"other"}})

    def test_first_contact_runs_only_an_allowed_passive_probe(self):
        req = self.payload("preflight")
        a = self.sites["alpha"]
        with patch("src.runtime.mail_contact.local_probe", return_value={"host_state":"unknown"}) as probe:
            first = mail_control.submit(a, "beta", req)
            again = mail_control.submit(a, "beta", req)
            self.assertEqual(first["request_id"], again["request_id"])
            self.poll("beta")
            self.poll("alpha")
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(mail_control.latest(a, "beta", req)["evidence"]["observation"], {"host_state":"unknown"})
            req = {**req,"target":{**req["target"],"client":"other-client"}}
            mail_control.submit(a, "beta", req)
            self.poll("beta")
            self.poll("alpha")
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(mail_control.latest(a, "beta", req)["evidence"]["status"], "unavailable")

    def test_live_schema_and_delivery_window_use_the_original_ledger(self):
        req = self.payload("preflight")
        a = self.sites["alpha"]
        mail_control.submit(a, "beta", req)
        self.poll("beta")
        self.poll("alpha")
        obs = mail_control.latest(a, "beta", req)["evidence"]["observation"]
        self.assertTrue(obs["host_xdev_version"])
        self.assertEqual(obs["model_calls"], 0)
        self.deliver_original()
        r = gh.retention_for(a, "beta", self.letter["id"])
        self.assertFalse(r["delivery_window_elapsed"])
        self.assertEqual(r["state"], "deletion_unconfirmed")
        self.assertEqual(gh.retention_for(a, "beta", "no-such-letter"), {"state":"unknown"})

    def test_missing_provider_ack_for_one_result_does_not_starve_another(self):
        self.deliver_original()
        a, b = self.sites["alpha"], self.sites["beta"]
        commands = [self.payload("status"), self.payload("recall")]
        for command in commands:
            mail_control.submit(a,"beta",command)
        original = gh.Api.upload
        attempts = []
        def lose_first(api, *args):
            attempts.append(args[1])
            self.fake.lose_upload = len(attempts) == 1
            try:
                return original(api, *args)
            finally:
                self.fake.lose_upload = False
        with patch.object(gh.Api,"upload",lose_first):
            self.assertEqual(len(self.poll("beta")), 2)
            self.assertEqual(len(self.poll("alpha")), 2)
            with closing(mail_control.db_open(b)) as db, db:
                db.execute("UPDATE handled SET due=0")
            self.poll("beta")
        self.assertEqual(len(attempts),2)
        for command in commands:
            self.assertEqual(mail_control.latest(a,"beta",command)["status"],"peer_reported")

    def test_previous_version_request_expiry_is_read_from_original_upload(self):
        self.deliver_original()
        a = self.sites["alpha"]
        command = self.payload()
        sent = mail_control.submit(a,"beta",command)
        expiry = sent["expires_at_unix"]
        with closing(mail_control.db_open(a)) as db, db:
            db.execute("ALTER TABLE requests DROP COLUMN expires")
        path = a / "config.json"
        config = json.loads(path.read_text())
        config["github_mail"]["retention_seconds"] = 3600
        path.write_text(json.dumps(config))
        calls = len(self.fake.calls)
        with patch("time.time",return_value=expiry+1):
            for checked in (mail_control.latest(a,"beta",command),mail_control.submit(a,"beta",command)):
                self.assertEqual(checked["expires_at_unix"],expiry)
                self.assertTrue(checked["delivery_window_elapsed"])
                self.assertEqual(checked["status"],"outcome_unknown")
                self.assertNotIn("evidence",checked)
        self.assertEqual(len(self.fake.calls),calls)

    def test_malformed_or_conflicting_correlated_status_is_rejected(self):
        self.deliver_original()
        a = self.sites["alpha"]
        req = self.payload("status")
        rid = mail_control.submit(a,"beta",req)["request_id"]
        frame = {"from":"beta","to":"alpha","id":"mc-a-" + rid[5:],"request":rid}
        def receive(result):
            return mail_control.accept(gh.Settings(a),"beta",{**frame,"result":result})
        valid = {"ok":True,"id":self.letter["id"],"status":"observed"}
        for bad in ({"ok":True,"status":"observed"}, {**valid,"id":"different"}, {**valid,"status":{}},
                    {**valid,"read_at":{}}, {**valid,"reason":{"body":"arbitrary"}}, {**valid,"version":True},
                    {**valid,"revisions":[{"revision":"next","at":None,"operation":"other"}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                receive(bad)
        self.assertEqual(mail_control.latest(a,"beta",req)["status"],"requested")
        receive(valid)
        receive(valid)
        with self.assertRaisesRegex(ValueError,"Changed"):
            receive({**valid,"status":"recalled"})

    def test_rate_hint_defers_all_response_uploads_across_polls(self):
        self.deliver_original()
        a,b = self.sites["alpha"],self.sites["beta"]
        for op in ("status","recall"):
            mail_control.submit(a,"beta",self.payload(op))
        with patch.object(mail_control,"flush"):
            self.poll("beta")
        def throttled(*args):
            try:
                raise gh.Unavailable("rate limited",135)
            except gh.Unavailable as exc:
                raise gh.NotSubmitted("not yet submitted") from exc
        now = int(time.time())
        with patch.object(gh,"_send",side_effect=throttled) as send:
            for offset in (0,20,120):
                with patch("time.time",return_value=now+offset):
                    mail_control.flush(gh.Api(b),gh.Settings(b),10)
            self.assertEqual(send.call_count,1)
