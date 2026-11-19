"""Verify OpenCode submission retries never trigger a second prompt."""

import json
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.adapters import opencode


class OpenCodeAdmissionTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.enterContext(patch.object(opencode.messages, "handle", return_value={
            "status": "received", "message": {"from": "laptop", "text": "Synthetic receipt."},
        }))
        self.enterContext(patch.object(opencode, "_cli", return_value=Path("opencode-cli.exe")))
        self.active = self.enterContext(patch.object(opencode, "_active_sessions", return_value={}))
        self.run_host = self.enterContext(patch.object(opencode.subprocess, "run"))

    def submit(self, steer=True, **kwargs):
        return opencode.submit(self.root, "note-1", "ses_example", steer, **kwargs)

    def successful_native(self, argv, **kwargs):
        body = json.loads(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"data": {"id": body["id"]}}).encode())

    def test_successful_retry_is_already_admitted(self):
        def native_run(argv, **kwargs):
            body = json.loads(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
                "data": {"id": body["id"]},
            }).encode())

        self.run_host.side_effect = native_run
        first, second = self.submit(), self.submit()
        self.assertEqual((first["status"], second["status"]), ("admitted", "already_admitted"))
        self.assertEqual(first["prompt_id"], second["prompt_id"])
        self.assertEqual((first["delivery_mode"], second["delivery_mode"]), ("steer", "steer"))
        self.assertEqual(second["model_status"], "unverified")
        self.run_host.assert_called_once()

    def test_timeout_does_not_resubmit(self):
        self.run_host.side_effect = subprocess.TimeoutExpired("opencode-cli.exe", timeout=20)
        self.assertEqual(self.submit()["error_code"], "OPENCODE_TIMEOUT")
        retry = self.submit()
        self.assertEqual((retry["error_code"], retry["outcome"]),
                         ("OPENCODE_ADMISSION_UNVERIFIED", "unknown"))
        self.assertEqual(retry["delivery_mode"], "unknown")
        self.run_host.assert_called_once()

    def test_idle_only_deferral_does_not_reserve(self):
        self.active.return_value = {"ses_example": {}}
        self.assertEqual(self.submit(idle_only=True)["status"], "deferred_in_inbox")
        self.run_host.assert_not_called()
        self.active.return_value = {}
        self.run_host.side_effect = subprocess.TimeoutExpired("opencode-cli.exe", timeout=20)
        self.assertEqual(self.submit(idle_only=True)["error_code"], "OPENCODE_TIMEOUT")

    def test_long_mailbox_content_is_not_injected_into_notice(self):
        marker = "PRIVATE-MAILBOX-CONTENT-" * 5000
        with patch.object(opencode.messages, "handle", return_value={
            "status": "received", "message": {"from": "laptop", "text": marker},
        }):
            self.run_host.side_effect = lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"data": {"id": json.loads(argv[-1])["id"]}}).encode())
            self.assertEqual(self.submit()["status"], "admitted")
        body = json.loads(self.run_host.call_args.args[0][-1])
        self.assertEqual(body["delivery"], "steer")
        self.assertIn("note-1", body["text"])
        self.assertIn("laptop", body["text"])
        self.assertIn("receipt", body["text"])
        self.assertNotIn("PRIVATE-MAILBOX-CONTENT-", body["text"])
        self.assertLess(len(body["text"]), 1000)

    def test_queue_retry_preserves_actual_queue_mode(self):
        self.run_host.side_effect = self.successful_native
        first, second = self.submit(steer=False), self.submit(steer=False)
        self.assertEqual((first["delivery_mode"], second["delivery_mode"]), ("queue", "queue"))
        self.assertEqual(second["status"], "already_admitted")
        self.run_host.assert_called_once()

    def test_existing_queue_cannot_be_relabelled_steer_or_resubmitted(self):
        self.run_host.side_effect = self.successful_native
        self.assertEqual(self.submit(steer=False)["delivery_mode"], "queue")
        promoted = self.submit()
        self.assertEqual(promoted["status"], "promotion_required")
        self.assertFalse(promoted["ok"])
        self.assertTrue(promoted["original_accepted"])
        self.assertEqual(promoted["delivery_mode"], "queue")
        self.assertEqual(promoted["requested_delivery_mode"], "steer")
        self.assertEqual(promoted["error_code"], "OPENCODE_QUEUE_PROMOTION_REQUIRED")
        self.run_host.assert_called_once()
        self.active.assert_called_once()

    def test_existing_steer_is_reused_when_later_request_says_queue(self):
        self.run_host.side_effect = self.successful_native
        self.submit()
        repeated = self.submit(steer=False)
        self.assertEqual(repeated["status"], "already_admitted")
        self.assertEqual(repeated["delivery_mode"], "steer")
        self.assertEqual(repeated["requested_delivery_mode"], "queue")
        self.run_host.assert_called_once()

    def legacy(self, admitted):
        directory = self.root / "state"
        directory.mkdir()
        prompt = "msg_" + sha256("ses_example\nnote-1".encode()).hexdigest()[:32]
        with closing(sqlite3.connect(directory / "opencode-admissions.sqlite3")) as db, db:
            db.execute("CREATE TABLE admissions (prompt_id TEXT PRIMARY KEY, admitted INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO admissions VALUES (?, ?)", (prompt, admitted))

    def test_legacy_admitted_rows_migrate_to_unknown_delivery_without_guessing(self):
        self.legacy(1)
        for steer in (True, False):
            with self.subTest(steer=steer):
                result = self.submit(steer=steer)
                self.assertEqual(result["status"], "already_admitted")
                self.assertEqual(result["delivery_mode"], "unknown")
                self.assertTrue(result["requires_delivery_verification"])
        self.run_host.assert_not_called()
        self.active.assert_not_called()

    def test_legacy_reserved_rows_remain_unknown_and_never_resubmit(self):
        self.legacy(0)
        result = self.submit()
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["delivery_mode"], "unknown")
        self.run_host.assert_not_called()

    def test_timeout_then_different_mode_does_not_create_a_second_prompt(self):
        self.run_host.side_effect = subprocess.TimeoutExpired("fixture", timeout=20)
        self.assertEqual(self.submit(steer=False)["delivery_mode"], "unknown")
        self.assertEqual(self.submit()["delivery_mode"], "unknown")
        self.run_host.assert_called_once()

    def test_concurrent_steer_during_pending_queue_is_unknown_then_requires_promotion(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def pending(argv, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.successful_native(argv, **kwargs)

        self.run_host.side_effect = pending
        with ThreadPoolExecutor(2) as pool:
            queue = pool.submit(self.submit, steer=False)
            self.assertTrue(entered.wait(3))
            concurrent = pool.submit(self.submit).result(timeout=3)
            self.assertEqual((concurrent["outcome"], concurrent["delivery_mode"]), ("unknown", "unknown"))
            release.set()
            self.assertEqual(queue.result(timeout=3)["delivery_mode"], "queue")
        self.assertEqual(self.submit()["status"], "promotion_required")
        self.run_host.assert_called_once()

    def test_native_start_failure_remains_unknown_without_retry(self):
        self.run_host.side_effect = OSError("private fixture diagnostic")
        first = self.submit()
        self.assertEqual(first["error_code"], "OPENCODE_START_UNVERIFIED")
        self.assertEqual(first["delivery_mode"], "unknown")
        self.assertEqual(self.submit()["outcome"], "unknown")
        self.assertNotIn("private fixture diagnostic", json.dumps(first))
        self.run_host.assert_called_once()

