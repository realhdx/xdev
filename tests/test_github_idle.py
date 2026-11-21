"""Simulate daemon time without GitHub access or real sleep."""

import unittest
from unittest.mock import patch

from src.runtime import github_demand, github_mail


class EndOfTrace(Exception):
    pass


class IdlePolling(unittest.TestCase):
    def delays(self, outcomes):
        delays, reports = [], []

        def sleep(seconds):
            delays.append(seconds)
            if len(delays) == len(outcomes):
                raise EndOfTrace()

        with (
            patch.object(github_mail, "Settings"),
            patch.object(github_mail, "Api"),
            patch.object(github_mail, "_collect", side_effect=outcomes),
            patch.object(github_demand, "wait", side_effect=lambda root, delay, minimum: sleep(delay)),
            self.assertRaises(EndOfTrace),
        ):
            github_mail.follow(None, reports.append)
        return delays, reports

    def test_idle_cap_activity_reset_and_duplicate_receipts(self):
        new = [{"event_created": True, "local_event": {"event": "session_mail_available"}}]
        duplicate = [{"event_created": False, "local_event": {"event": "session_mail_available"}}]
        expired = [{"status": "expired", "provider_message_id": "21"}]
        delays, reports = self.delays([[], [], [], [], [], [], duplicate, expired, new, [], []])
        self.assertEqual(delays, [20, 40, 80, 160, 300, 300, 300, 300, 20, 20, 40])
        self.assertEqual(reports, [{"transport": "github", "status": "expired"}, {"event": "session_mail_available"}])

    def test_provider_retry_after_overrides_idle_cap_and_transient_errors_back_off(self):
        disconnected = github_mail.Unavailable
        delays, reports = self.delays([[], disconnected("provider cooldown", 900), disconnected("offline"), [], []])
        self.assertEqual(delays, [20, 900, 80, 40, 80])
        self.assertEqual([r["retry_seconds"] for r in reports], [900, 80])

    def test_regular_blocking_listen_keeps_its_existing_request_budget(self):
        with (
            patch.object(github_mail, "Settings"),
            patch.object(github_mail, "Api"),
            patch.object(github_mail, "_collect", return_value=[]),
            patch.object(github_mail.time, "monotonic", side_effect=[0, 0, 20, 20, 35]),
            patch.object(github_mail.time, "sleep") as sleep,
        ):
            result = github_mail.listen(None, limit=1, timeout=35)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [20, 15])
