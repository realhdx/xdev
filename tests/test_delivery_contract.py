"""Pure contracts: no clients, subprocesses, sessions, or model requests."""

from copy import deepcopy
import unittest

from src.adapters.delivery_contract import acceptance, select_delivery


class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.descriptor = {"delivery_options": ["send_now", "queue", "idle_only"],
                           "default_delivery": "send_now"}

    def test_default_immediate_and_explicit_options(self):
        self.assertEqual(select_delivery(self.descriptor), "send_now")
        for flags, expected in (({"steer": True}, "send_now"), ({"queue": True}, "queue"),
                                ({"idle_only": True}, "idle_only")):
            self.assertEqual(select_delivery(self.descriptor, **flags), expected)

    def test_queue_only_never_becomes_default(self):
        descriptor = {"delivery_options": ["queue"], "default_delivery": "queue"}
        self.assertEqual(select_delivery(descriptor), "unsupported_requires_binding")
        self.assertEqual(select_delivery(descriptor, queue=True), "queue")
        self.assertEqual(select_delivery(descriptor, steer=True), "unsupported_requires_binding")
        self.assertEqual(select_delivery({"default_delivery": "idle_only"}), "unsupported_requires_binding")

    def test_legacy_capability_aliases_still_need_explicit_queue(self):
        for default in ("steer", "send_now_when_registered"):
            descriptor = {"default_delivery": default, "delivery_options": ["steer"], "explicit_queue": True}
            self.assertEqual(select_delivery(descriptor), "send_now")
            self.assertEqual(select_delivery(descriptor, queue=True), "queue")

    def test_default_cannot_invent_a_capability_missing_from_options(self):
        descriptor = {"default_delivery": "send_now", "delivery_options": ["queue"]}
        self.assertEqual(select_delivery(descriptor), "unsupported_requires_binding")
        self.assertEqual(select_delivery(descriptor, steer=True), "unsupported_requires_binding")
        self.assertEqual(select_delivery(descriptor, queue=True), "queue")
        self.assertEqual(select_delivery({"default_delivery": "send_now"}), "unsupported_requires_binding")

    def test_unknown_or_malformed_descriptors_never_queue(self):
        for descriptor in (None, {}, [], {"default_delivery": []},
                           {"delivery_options": "queue"}, {"default_delivery": "magic"},
                           {"callback": lambda: self.fail("must not run")},
                           {"delivery_options": ["queue"], "requires_binding": True}):
            self.assertEqual(select_delivery(descriptor), "unsupported_requires_binding")

    def test_conflicting_or_nonboolean_flags_fail_before_dispatch(self):
        for flags in ({"steer": True, "queue": True}, {"queue": 1}, {"idle_only": "yes"}):
            with self.assertRaises(ValueError):
                select_delivery(self.descriptor, **flags)

    def test_native_send_now_requires_evidence_not_ok_alone(self):
        result = {"ok": True, "status": "admitted", "delivery_mode": "send_now",
                  "admission_evidence": "native_send_now_accepted"}
        normalized = acceptance(result, "send_now")
        self.assertEqual(normalized["scope"], "current_turn")
        self.assertTrue(normalized["current_turn"])
        self.assertEqual(normalized["model_processing"], "unverified")
        for missing in ("admission_evidence", "delivery_mode", "status"):
            changed = dict(result)
            del changed[missing]
            self.assertEqual(acceptance(changed, "send_now")["scope"], "unknown")

    def test_native_steering_contract_shapes(self):
        results = [
            {"ok": True, "status": "admitted", "delivery_mode": "steer", "admission": {"accepted": True}},
            {"ok": True, "status": "admitted", "delivery": "steer"},
            {"ok": True, "status": "already_admitted", "delivery_mode": "steer",
             "promotion": {"status": "already_steering", "to_placement": "steering"}},
        ]
        for result in results:
            self.assertEqual(acceptance(result, "send_now")["scope"], "current_turn")
        self.assertEqual(acceptance({"ok": True, "status": "already_admitted", "delivery_mode": "steer",
                                    "promotion": {"status": "already_consumed"}}, "send_now")["scope"], "unknown")

    def test_queued_is_next_turn_even_when_immediate_requested(self):
        for status in ("queued", "already_queued"):
            result = acceptance({"ok": True, "status": status}, "send_now")
            self.assertEqual(result["scope"], "next_turn")
            self.assertFalse(result["current_turn"])
            self.assertTrue(result["next_turn"])
        for result in ({"ok": True, "status": "admitted", "delivery": "queue"},
                       {"ok": True, "status": "admitted", "delivery_mode": "queue", "admission": {"accepted": True}}):
            self.assertEqual(acceptance(result, "queue")["scope"], "next_turn")

    def test_input_surface_never_claims_session_or_model_acceptance(self):
        normalized = acceptance({"ok": True, "status": "input_surface_accepted", "outcome": "unknown",
                                 "admission_evidence": "native_input_surface_ack"}, "queue")
        self.assertEqual(normalized["scope"], "surface")
        self.assertTrue(normalized["surface"])
        self.assertFalse(normalized["current_turn"])
        self.assertFalse(normalized["next_turn"])

    def test_failed_unknown_or_legacy_admitted_is_not_success(self):
        for result in (None, {}, {"ok": True}, {"ok": True, "status": "admitted"},
                       {"ok": False, "status": "queued"}, {"ok": 1, "status": "queued"},
                       {"ok": True, "status": "requires_activation"}, {"ok": True, "status": []},
                       {"ok": True, "status": "unknown", "delivery": "steer"}):
            self.assertEqual(acceptance(result, "send_now")["scope"], "unknown")
        self.assertEqual(acceptance({"ok": True, "status": "queued"}, "unsupported_requires_binding")["scope"], "unknown")

    def test_third_client_entire_contract_is_data_not_name_branches(self):
        descriptor = {"name": "third-party-fixture", "delivery_options": ["send_now", "queue"],
                      "default_delivery": "send_now", "acceptance_rules": [
                          {"scope": "current_turn", "all": {"ok": True, "native.ack": "inserted-active"}},
                          {"scope": "next_turn", "all": {"ok": True, "native.ack": "enqueued-next"}}]}
        before = deepcopy(descriptor)
        self.assertEqual(select_delivery(descriptor), "send_now")
        self.assertEqual(select_delivery(descriptor, queue=True), "queue")
        self.assertEqual(acceptance({"ok": True, "native": {"ack": "inserted-active"}}, "send_now", descriptor)["scope"], "current_turn")
        self.assertEqual(acceptance({"ok": True, "native": {"ack": "enqueued-next"}}, "send_now", descriptor)["scope"], "next_turn")
        self.assertEqual(descriptor, before)

    def test_conflicting_or_unsafe_rules_fail_closed(self):
        result = {"ok": True, "status": "queued", "delivery": "steer"}
        # Generic queue evidence and provider steering evidence contradict.
        result["status"] = "admitted"
        result["delivery_mode"] = "queue"
        result["admission"] = {"accepted": True}
        normalized = acceptance(result, "send_now")
        self.assertEqual(normalized["scope"], "unknown")
        self.assertEqual(normalized["evidence"]["conflicting_scopes"], ["current_turn", "next_turn"])
        for rule in ({"scope": "current_turn", "all": {}},
                     {"scope": "current_turn", "all": {"__import__('os')": True}},
                     {"scope": "current_turn", "all": {"ok": lambda: True}},
                     {"scope": "current_turn", "all": {"ok": True}, "exec": "anything"}):
            self.assertEqual(acceptance({"ok": True}, "send_now", {"acceptance_rules": [rule]})["scope"], "unknown")

    def test_boolean_ack_does_not_match_integer(self):
        result = {"ok": True, "status": "admitted", "delivery_mode": "steer", "admission": {"accepted": 1}}
        self.assertEqual(acceptance(result, "send_now")["scope"], "unknown")

    def test_custom_rules_cannot_upgrade_queue_surface_or_unknown(self):
        self.assertEqual(acceptance({"ok": True, "status": "queued", "outcome": "unknown"},
                                    "queue")["scope"], "unknown")
        descriptor = {"acceptance_rules": [{"scope": "current_turn", "all": {"ok": True}}]}
        self.assertEqual(acceptance({"ok": True, "status": "queued"}, "send_now", descriptor)["scope"], "next_turn")
        self.assertEqual(acceptance({"ok": True, "status": "input_surface_accepted"}, "send_now", descriptor)["scope"], "unknown")
        self.assertEqual(acceptance({"ok": True, "outcome": "unknown"}, "send_now", descriptor)["scope"], "unknown")


if __name__ == "__main__":
    unittest.main()
