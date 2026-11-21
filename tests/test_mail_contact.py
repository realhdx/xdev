"""Exercise the normal CLI, body-free status lookup and paired control RPC."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.adapters import caller_context, inprocess_cli, sessions
from src.cli import contact
from src.runtime import mail_contact, mail_control_ssh, messages, outbox, ssh_mail


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.area = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key = self.area / "pair"
        key.write_text("1a" * 32)
        self.a = self.area / "a"
        self.b = self.area / "b"
        for root, own, peer in [(self.a, "alpha", "beta"), (self.b, "beta", "alpha")]:
            root.mkdir()
            (root / "config.json").write_text(json.dumps({
                "device": own, "peers": {peer: {"ssh":"paired-alias", "mail_transport":"direct",
                                               "receiver_apps":["writer"]}},
                "github_mailbox": {"peers":{peer: {"key_file":str(key),"receiver_apps":["writer"]}}}
            }))
        self.author = {"device":"alpha","client":"writer","session":"one"}
        self.target = {"device":"beta","client":"writer","session":"two"}
        self.original = {"envelope_version":2,"id":"a1","from":"alpha","to":"beta","text":"original private text",
                         "sender_endpoint":self.author,"recipient_identity":self.target,"reply_endpoint":self.author,
                         "communication_intent":"invite"}
        self.frames = []

    def call(self, root, *args):
        return inprocess_cli.invoke(["msg", *args], root=root)

    def bridge(self, peer, frame, timeout, *, root=None, direct=False):
        self.frames.append(frame)
        return mail_control_ssh.receive(self.b, frame["payload"])

    def test_signed_probe_no_model_inference_or_state_leak(self):
        mail_contact.report_presence(self.b, self.target, "quota_limited", 300, "quota")
        with (patch.object(sessions, "discover", return_value={"ok":True,"status":"ready","active":True,"running":False,
                                                                "private_tool_trace":"never returned"}),
              patch("src.runtime.transport.peer_request", self.bridge)):
            resp = self.call(self.a,"preflight","--to","beta","--client","writer","--session","two")
        evidence = resp["preflight"]
        self.assertEqual(evidence["host_state"],"idle")
        self.assertEqual(evidence["declaration"]["state"],"quota_limited")
        self.assertEqual(evidence["source"],"local_host")
        self.assertEqual(evidence["model_calls"],0)
        self.assertNotIn("private_tool_trace", str(evidence))
        with self.assertRaisesRegex(ValueError,"already used"):
            mail_control_ssh.receive(self.b,self.frames[0]["payload"])
        forged = {**self.frames[0]["payload"],"command":{"op":"preflight","target":self.author}}
        with self.assertRaisesRegex(ValueError,"signature"):
            mail_control_ssh.receive(self.b,forged)

    def test_expired_availability_and_host_observation_label(self):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        resp = mail_contact.cached(self.a, self.target, refresh={"source":"local_host","host_state":"idle","observed_at":stamp,
             "declaration":{"state":"busy","expires_at":stamp}})
        self.assertEqual(resp["host_state"],"unknown")
        self.assertTrue(resp["host_observation_stale"])
        self.assertTrue(resp["declaration"]["expired"])

    def test_remote_clock_skew_and_delayed_parallel_samples(self):
        reference = datetime.now(timezone.utc)
        for offset, session in ((30, "ahead"), (-90, "behind")):
            target = {**self.target, "session": session}
            peer = reference + timedelta(seconds=offset)
            sample = {"source": "local_host", "host_state": "idle", "observed_at": peer.isoformat(),
                      "declaration": {"state": "busy", "expires_at": (peer + timedelta(seconds=20)).isoformat()}}
            start = (reference - timedelta(seconds=2)).isoformat()
            fresh = mail_contact.cached(self.a, target, actor=self.author, refresh=sample, earliest_sample=start)
            self.assertEqual(fresh["host_state"], "idle")
            self.assertFalse(fresh["declaration"]["expired"])
            self.assertEqual(fresh["age_basis"], "local_request_upper_bound")
            older = (reference - timedelta(seconds=70)).isoformat()
            kept = mail_contact.cached(self.a, target, actor=self.author, refresh={**sample, "host_state": "busy"},
                                       earliest_sample=older)
            self.assertEqual(kept["host_state"], "idle")
            pending = mail_contact.cached(self.a, {**target, "session": session+"-delayed"},
                                          refresh=sample, earliest_sample=older)
            self.assertTrue(pending["host_observation_stale"])
            self.assertTrue(pending["declaration"]["expired"])

    def test_passive_github_send_never_publishes_a_probe(self):
        with (patch("src.runtime.mail_control.latest",return_value={"status":"no_control_observation"}) as latest,
              patch("src.runtime.mail_control.submit",side_effect=AssertionError("Unexpected network call"))):
            self.assertEqual(contact.observe(self.a,self.author,self.target,"github")["host_state"],"unknown")
            latest.assert_called_once()

    def test_github_previous_sample_is_scoped_and_never_called_a_live_probe(self):
        stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        receipt = {"status": "requested", "earlier_observation": True, "response_observed_at": 100,
                   "evidence": {"observation": {"source": "local_host", "host_state": "idle", "observed_at": stamp}}}
        with patch("src.runtime.mail_control.latest", return_value=receipt):
            observed = contact.observe(self.a, self.author, self.target, "github")
        self.assertEqual(observed["status"], "cached")
        self.assertEqual(observed["live_check"], "cached_peer_report")
        self.assertTrue(observed["earlier_observation"])
        self.assertEqual(observed["host_state"], "unknown")
        self.assertIn("check_pending_preflight_later", observed["next_options"])
        other = mail_contact.cached(self.a, self.target, actor={**self.author, "session": "other"})
        self.assertEqual(other["status"], "unknown")

    def test_real_cli_delivery_makes_intent_visible_even_with_host_admission_pending(self):
        session = "session-00000000-0000-4000-8000-000000000001"
        with (patch.object(sessions, "discover", return_value={"status": "not_available"}),
              patch.object(sessions, "notify", return_value={"status": "unknown", "outcome": "unknown"})):
            sent = self.call(self.a, "send", "--id", "fresh", "--text", "check the table", "--to", "alpha",
                             "--client", "dsh", "--session", session, "--from-client", "writer",
                             "--from-session", "one", "--one-way", "--intent", "request_action")
        self.assertEqual(sent["inbox_status"], "received", sent)
        self.assertEqual(sent["intent_delivery"], "recipient_copy_confirmed")
        self.assertEqual(sent["session_status"], "unknown")
        read = self.call(self.a, "read", "fresh", "--as-client", "dsh", "--as-session", session)
        self.assertEqual(read["message"]["communication_intent"], "request_action")
        with caller_context.bind(None):
            denied = self.call(self.a, "report", "fresh", "--as-client", "dsh", "--as-session", session,
                               "--reply-intent", "no_reply")
        self.assertEqual(denied["status"], "host_chat_identity_required")
        with caller_context.bind_dsh(session):
            report = self.call(self.a, "report", "fresh", "--reply-intent", "no_reply", "--operation-id", "report-one")
        self.assertTrue(report["ok"])
        status = self.call(self.a, "status", "fresh", "--as-client", "writer", "--as-session", "one")
        self.assertEqual(status["dispatch"]["intent"], "request_action")
        self.assertEqual(status["next_options"], ["no_further_wait_needed"])
        self.assertIsNone(status["recipient_observation"]["work_state"])

    def test_normal_send_read_report_status_and_reply_do_not_confuse_silence_with_refusal(self):
        # A tool-wait callback supplies a named return address without a live model.
        author = {**self.author,"client":"writer"}
        target = {"device":"alpha","client":"reader","session":"two"}
        letter = {**self.original,"to":"alpha","recipient_identity":target,"sender_endpoint":author,
                  "recipient_endpoint":target,"reply_endpoint":author}
        messages.handle(self.a,"deliver",letter)
        mail_contact.before_send(self.a,letter,"invite")
        outbox.remember(self.a,letter)
        before = self.call(self.a,"status","a1","--as-client","writer","--as-session","one")
        self.assertEqual(before["dispatch"]["intent"],"invite")
        self.assertIsNone(before["recipient_observation"]["read_at"])
        self.assertEqual(before["reply_evidence"],"unverified")
        self.assertIsNone(before["recipient_observation"]["reply_intent"])
        self.call(self.a,"read","a1","--as-client","reader","--as-session","two")
        report = self.call(self.a,"report","a1","--as-client","reader","--as-session","two",
                           "--work-state","blocked","--reason","review next week","--reply-intent","no_reply")
        self.assertTrue(report["ok"], report)
        later = self.call(self.a,"status","a1","--as-client","writer","--as-session","one")
        self.assertEqual(later["recipient_observation"]["reply_intent"],"no_reply")
        self.assertTrue(later["recipient_observation"]["work_at"])
        self.assertNotIn("original private text",str(later))
        wrong = self.call(self.a,"status","a1","--as-client","writer","--as-session","other")
        self.assertFalse(wrong["ok"])
        self.assertNotIn("review next week",str(wrong))
        self.assertFalse(self.call(self.a,"recall","a1","--as-client","writer","--as-session","one")["ok"])

    def test_forced_peer_cannot_query_other_session(self):
        messages.handle(self.b,"deliver",self.original)
        cmd={"op":"status","id":"a1","target":self.target,"actor":{**self.author,"session":"different"}}
        frame={"service":"msg","action":"control","expected_device":"beta","payload":{"command":cmd}}
        self.assertFalse(ssh_mail.receive(self.b,"alpha",frame)["ok"])
        cmd["actor"] =self.author
        self.assertEqual(ssh_mail.receive(self.b,"alpha",frame)["scope"],"recipient_mailbox")

    def test_confirmed_recall_preserves_arrival_without_offering_withdrawn_mail(self):
        mail_contact.before_send(self.a, self.original, "invite")
        arrival = {"ok": True, "status": "report_received", "inbox_status": "received",
                   "available_to_addressed_chat": True}
        with (patch("src.runtime.outbox.status", return_value=arrival),
              patch("src.cli.contact.remote_or_local", return_value={
                  "status": "peer_reported", "evidence": {"withdrawn_at": "2026-01-01T10:00:00Z"}})):
            status = self.call(self.a, "status", "a1", "--as-client", "writer", "--as-session", "one")
        self.assertEqual(status["inbox_status"], "received")
        self.assertEqual(status["current_mailbox_state"], "withdrawn")
        self.assertFalse(status["available_to_addressed_chat"])

    def test_transport_retention_follows_actual_submission_not_the_selected_status_route(self):
        mail_contact.before_send(self.a, self.original, "invite")
        mail_contact.after_send(self.a, "a1", {"transport": "github", "status": "broker_acknowledged"})
        with (patch("src.cli.contact.remote_or_local", return_value={"status": "unconfirmed"}),
              patch("src.runtime.github_mail.retention_for", return_value={"state": "pending"}) as check):
            report = self.call(self.a, "status", "a1", "--as-client", "writer", "--as-session", "one",
                               "--transport", "ssh")
        check.assert_called_once_with(self.a, "beta", "a1")
        self.assertEqual(report["transport_retention"]["state"], "pending")
        self.assertEqual(report["dispatch"]["submission"]["transport"], "github")


if __name__ == "__main__":
    unittest.main()
