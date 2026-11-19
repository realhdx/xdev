"""The local ai entry applies shared data policy to a third mocked provider."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from src.adapters import sessions
from src.cli import main
from src.runtime import messages


class AIDeliveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="xdev-ai-policy-")))
        (self.root / "config.json").write_text('{"device":"laptop"}', encoding="utf-8")
        messages.handle(self.root, "deliver", {"id": "policy-fixture", "from": "desktop", "to": "laptop",
                                                "text": "HARNESS only; no native client or model."})
        self.spec = {"module": "xdev_test_policy_receiver", "session_kind": "pattern",
                     "session_pattern": r"fake_[a-z]+", "session_prefix": "",
                     "delivery_options": ["send_now", "queue", "idle_only"],
                     "default_delivery": "send_now", "native_send_now_flag": "steer",
                     "force_queue_option": False, "profile_field": None,
                     "acceptance_rules": [
                         {"scope": "current_turn", "all": {"ok": True, "ack.placement": "active"}},
                         {"scope": "next_turn", "all": {"ok": True, "ack.placement": "waiting"}},
                         {"scope": "surface", "all": {"ok": True, "ack.placement": "input"}}]}
        self.enterContext(patch.dict(sessions.ADAPTERS, {"fixture": self.spec}))
        module = types.ModuleType("xdev_test_policy_receiver")
        self.native_result = {"ok": True, "status": "admitted", "ack": {"placement": "active"}}
        module.submit = self.submit = Mock(side_effect=lambda *args, **kwargs: deepcopy(self.native_result))
        self.enterContext(patch.dict(sys.modules, {module.__name__: module}))
        self.enterContext(patch.dict(main.os.environ, {"CODEX_THREAD_ID": ""}))
        self.enterContext(patch.object(main, "_emit", side_effect=lambda result: result))

    def run_ai(self, *flags):
        return main._parse_and_run(self.root, ["ai", "--client", "fixture", "--session", "fake_chat",
                                               *flags, "policy-fixture"])

    def test_default_immediate_uses_descriptor_edge_flag_and_exact_ack(self):
        result = self.run_ai()
        self.assertTrue(result["ok"])
        self.assertTrue(result["current_turn_accepted"])
        self.assertTrue(result["session_accepted"])
        self.assertEqual(result["requested_delivery_mode"], "send_now")
        self.assertEqual(result["delivery_receipt"]["scope"], "current_turn")
        self.assertEqual(self.submit.call_args.args[1:], ("policy-fixture", "fake_chat", True, False))
        self.assertEqual(self.submit.call_args.kwargs, {})

    def test_other_provider_can_use_immediate_without_steer_flag(self):
        self.spec["native_send_now_flag"] = None
        result = self.run_ai()
        self.assertTrue(result["current_turn_accepted"])
        self.assertEqual(self.submit.call_args.args[-2:], (False, False))

    def test_default_queue_ack_needs_promotion_and_never_resends(self):
        self.native_result = {"ok": True, "status": "queued", "queue_id": "existing-original"}
        result = self.run_ai()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "promotion_required")
        self.assertEqual(result["native_status"], "queued")
        self.assertEqual(result["queue_id"], "existing-original")
        self.assertFalse(result["current_turn_accepted"])
        self.assertTrue(result["session_accepted"])
        self.assertEqual(result["delivery_receipt"]["scope"], "next_turn")
        self.submit.assert_called_once()

    def test_explicit_queue_respects_descriptor_force_option(self):
        self.native_result = {"ok": True, "status": "queued"}
        for force in (False, True):
            self.spec["force_queue_option"] = force
            self.submit.reset_mock()
            result = self.run_ai("--queue")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["requested_delivery_mode"], "queue")
            self.assertFalse(result["current_turn_accepted"])
            self.assertEqual(self.submit.call_args.args[-2:], (False, False))
            self.assertEqual(self.submit.call_args.kwargs, {"force_queue": True} if force else {})

    def test_idle_only_never_accidentally_sets_steer(self):
        result = self.run_ai("--idle-only")
        self.assertEqual(result["requested_delivery_mode"], "idle_only")
        self.assertEqual(self.submit.call_args.args[-2:], (False, True))

    def test_unsupported_default_stops_before_provider(self):
        self.spec["delivery_options"] = ["queue"]
        result = self.run_ai()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "requires_receiver_binding")
        self.assertFalse(result["current_turn_accepted"])
        self.submit.assert_not_called()

    def test_ok_or_legacy_admitted_without_exact_evidence_is_unknown(self):
        for native in ({"ok": True, "status": "admitted"},
                       {"ok": 1, "status": "admitted", "ack": {"placement": "active"}},
                       {"status": "admitted", "ack": {"placement": "active"}},
                       {"ok": True, "status": "admitted", "ack": {"placement": "almost-active"}},
                       {"ok": True, "status": "admitted", "ack": {"placement": True}}):
            self.native_result = native
            result = self.run_ai()
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["session_accepted"])
            self.assertFalse(result["current_turn_accepted"])

    def test_surface_and_unknown_never_claim_current_turn(self):
        self.native_result = {"ok": True, "status": "input_surface_accepted", "ack": {"placement": "input"}}
        surface = self.run_ai()
        self.assertFalse(surface["ok"])
        self.assertEqual(surface["delivery_receipt"]["scope"], "surface")
        self.assertFalse(surface["session_accepted"])
        self.native_result = {"ok": False, "status": "unknown", "outcome": "unknown", "error_code": "FIXTURE_TIMEOUT"}
        unknown = self.run_ai()
        self.assertEqual((unknown["ok"], unknown["status"], unknown["error_code"]), (False, "unknown", "FIXTURE_TIMEOUT"))
        self.assertFalse(unknown["current_turn_accepted"])

    def test_requires_activation_is_preserved_without_claiming_admission(self):
        self.native_result = {"ok": False, "status": "requires_activation", "requires_activation": True}
        result = self.run_ai()
        self.assertEqual((result["ok"], result["status"]), (False, "requires_activation"))
        self.assertFalse(result["session_accepted"])
        self.assertFalse(result["current_turn_accepted"])

    def test_receipt_suppression_remains_no_model_business_result(self):
        messages.handle(self.root, "deliver", {"id": "receipt-fixture", "from": "desktop", "to": "laptop",
                                                "kind": "receipt", "reply_to": "policy-fixture", "text": "receipt only"})
        result = main._parse_and_run(self.root, ["ai", "--client", "fixture", "--session", "fake_chat", "receipt-fixture"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "receipt_suppressed")
        self.assertFalse(result["session_accepted"])
        self.assertFalse(result["current_turn_accepted"])
        self.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
