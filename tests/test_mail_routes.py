"""A carrier switch is safe only when the sending endpoint never submitted the letter."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.adapters import inprocess_cli
from src.cli import main
from src.runtime import mail_routes, managed_queue, session_relay, transport


class AutomaticMailTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = {"device": "alpha", "peers": {"beta": {"mail_routes": ["direct", "relay", "sqs"]}},
                       "session_relay": {"peers": {"beta": {"key_file": "pair", "encryption": "required"}}},
                       "managed_queue": {"peers": {"beta": {"key_file": "pair", "queue_url": "paired-queue"}}}}
        self.write()

    def write(self):
        (self.root / "config.json").write_text(json.dumps(self.config))

    def post(self, *extra):
        return inprocess_cli.invoke(["msg", "send", "--to", "beta", "--id", "crossing",
                                     "--text", "draft", "--as-device", *extra], root=self.root)

    def test_cli_rotates_only_across_before_submission_failures(self):
        with patch.object(transport, "check_direct_connection", side_effect=mail_routes.NotSubmitted("unreachable")), \
                patch.object(session_relay, "publish", side_effect=mail_routes.NotSubmitted("not connected")), \
                patch.object(managed_queue, "publish", return_value={"ok": True, "status": "queue_acknowledged",
                                                                    "id": "crossing", "transport": "sqs"}) as queue:
            result = self.post()
            repeat = self.post()
            explicit = self.post("--transport", "direct")
        self.assertEqual([(x["transport"], x["status"]) for x in result["route_attempts"]],
                         [("direct", "not_submitted"), ("relay", "not_submitted"), ("sqs", "queue_acknowledged")])
        self.assertEqual(queue.call_count, 1)
        self.assertTrue(repeat["route_reused"])
        self.assertEqual((explicit["transport"], explicit["route_reused"]), ("sqs", True))
        status = inprocess_cli.invoke(["msg", "status", "crossing", "--as-device"], root=self.root)
        self.assertEqual((status["transport"], status["submission_status"]), ("sqs", "queue_acknowledged"))
        other = inprocess_cli.invoke(["msg", "status", "crossing", "--as-client", "pen",
                                      "--as-session", "different"], root=self.root)
        self.assertFalse(other["ok"])

    def test_remote_failure_text_cannot_trigger_another_carrier(self):
        response = {"ok": False, "outcome": "unknown", "status": "not_submitted", "error_code": "SSH_HOST_KEY_REJECTED"}
        with patch.object(transport, "check_direct_connection"), patch.object(main, "_call", return_value=response) as ssh, \
                patch.object(session_relay, "publish") as relay, patch.object(managed_queue, "publish") as queue:
            result = self.post()
            self.assertEqual((result["error_code"], result["transport"]), ("SSH_HOST_KEY_REJECTED", "direct"))
            self.assertTrue(self.post()["route_reused"])
            self.assertTrue(self.post("--transport", "relay")["route_reused"])
        self.assertEqual(ssh.call_count, 1)
        relay.assert_not_called()
        queue.assert_not_called()

    def test_unknown_relay_publication_never_reaches_queue_even_on_a_later_process_retry(self):
        self.config["peers"]["beta"]["mail_routes"] = ["relay", "sqs"]
        self.write()
        with patch.object(session_relay, "publish", side_effect=TimeoutError("lost acknowledgement")) as relay, \
                patch.object(managed_queue, "publish") as queue:
            result = self.post()
            self.assertEqual(result["status"], "submission_unconfirmed")
            self.assertTrue(self.post("--transport", "sqs")["route_reused"])
            self.assertTrue(self.post()["route_reused"])
        self.assertEqual(relay.call_count, 1)
        queue.assert_not_called()

    def test_pinned_route_stays_fixed_and_an_unpaired_or_plaintext_carrier_is_excluded(self):
        self.config["peers"]["beta"]["mail_transport"] = "relay"
        self.write()
        self.assertEqual(mail_routes.candidates(self.root, "beta", "auto"), ("relay",))
        self.assertEqual(mail_routes.candidates(self.root, "beta", "direct"), ("direct",))
        self.config["peers"]["beta"] = {}
        self.config.pop("managed_queue")
        self.config["session_relay"]["peers"]["beta"].pop("encryption")
        self.write()
        self.assertEqual(mail_routes.candidates(self.root, "beta", "auto"), ("ssh",))
        self.config["peers"]["beta"]["mail_routes"] = ["relay"]
        self.write()
        with self.assertRaisesRegex(ValueError, "requires encryption"):
            mail_routes.candidates(self.root, "beta", "auto")

    def test_default_preserves_existing_proxy_and_explicit_direct_remains_direct(self):
        self.config["peers"]["beta"] = {}
        self.write()
        with patch.object(transport, "ssh_uses_proxy", return_value=True):
            self.assertEqual(mail_routes.candidates(self.root, "beta", "auto"), ("ssh", "relay", "sqs"))
            self.assertEqual(mail_routes.candidates(self.root, "beta", "direct"), ("direct",))

    def test_private_repository_is_opt_in_for_this_peer_and_blocks_plaintext_mail(self):
        from src.runtime import messages
        self.config["peers"]["beta"] = {}
        self.config["github_mailbox"] = {"delivery_scope": "mailbox", "peers": {"beta": {"key_file": "pair"}}}
        self.write()
        with patch.object(transport, "ssh_uses_proxy", return_value=False):
            self.assertEqual(mail_routes.candidates(self.root, "beta", "auto"), ("direct", "relay", "sqs", "github"))
            self.assertFalse(messages.legacy_broker_allowed(self.root, "beta"))
            self.config.pop("github_mailbox")
            self.write()
            self.assertNotIn("github", mail_routes.candidates(self.root, "beta", "auto"))

    def test_mailbox_migration_rejects_conflicting_or_wake_enabled_config(self):
        from src.runtime import messages
        from src.runtime.device_names import github_mailbox_config
        self.config["peers"]["beta"] = {}
        self.config.pop("managed_queue")
        self.config.pop("session_relay")
        mailbox = {"peers": {"beta": {"key_file": "pair"}}}
        for section in ("github_mailbox", "github_mail"):
            self.config[section] = mailbox
            self.write()
            with patch.object(transport, "ssh_uses_proxy", return_value=False):
                self.assertEqual(mail_routes.candidates(self.root, "beta", "auto"), ("direct", "github"))
            self.assertFalse(messages.legacy_broker_allowed(self.root, "beta"))
            self.config.pop(section)
        self.config.update(github_mailbox=mailbox, github_mail=mailbox)
        with self.assertRaisesRegex(ValueError, "one GitHub"):
            github_mailbox_config(self.config)
        self.config.pop("github_mail")
        self.config["github_mailbox"]["delivery_scope"] = "native"
        self.write()
        with self.assertRaisesRegex(ValueError, "delivery_scope"):
            mail_routes.candidates(self.root, "beta", "auto")
        with self.assertRaisesRegex(ValueError, "delivery_scope"):
            messages.legacy_broker_allowed(self.root, "beta")

    def test_local_event_reader_uses_cursor_without_listening_or_fetching_credentials(self):
        with patch.object(main, "_relay_carrier") as load_carrier:
            carrier = load_carrier.return_value
            carrier.events.return_value = {"ok": True, "events": [], "next_after": 6}
            result = inprocess_cli.invoke(["msg", "events", "--after", "6", "--limit", "2"], root=self.root)
        self.assertTrue(result["ok"])
        carrier.events.assert_called_once_with(self.root, after=6, limit=2)
        carrier.listen.assert_not_called()

    def test_fixed_repository_send_and_listener_use_one_carrier_and_unknown_never_switches(self):
        self.config["peers"]["beta"] = {"mail_transport": "github", "ssh": None}
        self.config["github_mail"] = {"peers": {"beta": {"key_file": "pair"}}}
        self.write()
        carrier = Mock()
        carrier.publish.side_effect = TimeoutError("upload acknowledgment missing")
        carrier.listen.return_value = {"ok": True, "results": [], "status": "timeout", "transport": "github"}
        with patch.object(main, "_relay_carrier", return_value=carrier) as lookup:
            result = self.post("--delivery-report")
            recovered = self.post("--transport", "sqs", "--delivery-report")
            polled = inprocess_cli.invoke(["msg", "listen", "--transport", "github", "--timeout", "1",
                                           "--limit", "1"], root=self.root)
        self.assertTrue(polled["ok"])
        self.assertEqual((result["transport"], result["status"]), ("github", "submission_unconfirmed"))
        self.assertTrue(recovered["route_reused"])
        self.assertEqual([c.args for c in lookup.call_args_list], [("github",), ("github",)])
        carrier.listen.assert_called_once_with(self.root, limit=1, timeout=1.0)
        carrier.publish.assert_called_once()

    def test_uncertain_repository_upload_can_only_be_reconciled_read_only(self):
        self.config["peers"]["beta"] = {"mail_transport": "github"}
        self.write()
        adapter = Mock()
        adapter.publish.side_effect = TimeoutError("write result unknown")
        adapter.reconcile.side_effect = [{"ok": False, "status": "submission_unconfirmed"},
                                         {"ok": True, "status": "queue_acknowledged", "transport": "github"}]
        with patch.object(main, "_relay_carrier", return_value=adapter):
            self.assertEqual(self.post()["status"], "submission_unconfirmed")
            self.assertEqual(self.post()["lookup_status"], "unconfirmed")
            self.assertEqual(self.post()["lookup_status"], "confirmed")
            self.assertTrue(self.post()["route_reused"])
        self.assertEqual(adapter.reconcile.call_count, 2)
        self.assertEqual(adapter.publish.call_count, 1)
        status = inprocess_cli.invoke(["msg", "status", "crossing", "--as-device"], root=self.root)
        self.assertEqual((status["transport"], status["submission_status"]), ("github", "queue_acknowledged"))

    def test_concurrent_call_does_not_republish_or_switch_while_original_is_pending(self):
        started, finish = threading.Event(), threading.Event()
        def slow(carrier):
            started.set()
            self.assertTrue(finish.wait(5))
            return {"ok": True, "status": "broker_acknowledged"}
        with ThreadPoolExecutor() as pool:
            first = pool.submit(mail_routes.send, self.root, "beta", "same", ("relay", "sqs"), slow)
            self.assertTrue(started.wait(5))
            try:
                other = mail_routes.send(self.root, "beta", "same", ("sqs",), Mock(side_effect=AssertionError()))
                self.assertEqual((other["status"], other["transport"]), ("submission_unconfirmed", "relay"))
            finally:
                finish.set()
            self.assertTrue(first.result()["ok"])

    def test_policy_error_does_not_fall_through(self):
        with patch.object(transport, "check_direct_connection", side_effect=ValueError("invalid SSH configuration")), \
                patch.object(session_relay, "publish") as relay:
            result = self.post()
        self.assertFalse(result["ok"])
        relay.assert_not_called()

    def test_discovery_does_not_register_a_host_or_notify_and_accepts_optional_filters(self):
        with patch.object(main.sessions, "discover", create=True, return_value={"ok": True}) as discover, \
                patch.object(main, "_register_caller") as register, patch.object(main.sessions, "submit") as notify:
            for args, filters in [([], (None, None)), (["--client", "codex"], ("codex", None)),
                                  (["--session", "id"], (None, "id"))]:
                self.assertTrue(inprocess_cli.invoke(["ai", "--discover", *args], root=self.root)["ok"])
                discover.assert_called_with(self.root, client=filters[0], session=filters[1])
            for args in (["--capabilities"], ["--register"], ["body"], ["--steer"]):
                self.assertFalse(inprocess_cli.invoke(["ai", "--discover", *args], root=self.root)["ok"])
        self.assertEqual(discover.call_count, 3)
        notify.assert_not_called()
        register.assert_not_called()
