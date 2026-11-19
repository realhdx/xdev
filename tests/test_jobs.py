from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from src.runtime import jobs


@unittest.skipUnless(os.name == "nt", "The persisted job worker targets Windows")
class JobTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.launch = self.enterContext(mock.patch.object(jobs, "_launch"))

    def test_id_binds_one_request(self):
        payload = {"id": "test-1", "argv": ["echo", "first"], "cwd": str(self.root)}
        self.assertFalse(jobs.handle(self.root, "start", payload)["duplicate"])
        with self.assertRaisesRegex(ValueError, "different command"):
            jobs.handle(self.root, "start", {**payload, "argv": ["echo", "second"]})
        self.assertEqual(self.launch.call_count, 1)
        self.assertTrue(jobs.handle(self.root, "start", payload)["duplicate"])

    def test_reject_job_ids_that_can_escape_state_directory(self):
        for value in ("../test", "a/b", "a\\b", "a:", ".", "中文", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jobs.handle(self.root, "start", {"id": value, "argv": ["echo"]})
        self.launch.assert_not_called()

    def test_cancel_queued_job_without_creating_a_process(self):
        payload = {"id": "test-2", "argv": ["echo", "first"]}
        self.assertEqual(jobs.handle(self.root, "start", payload)["state"], "queued")
        canceled = jobs.handle(self.root, "cancel", {"id": "test-2"})
        self.assertEqual(canceled["state"], "canceled")
        self.assertTrue(canceled["cancel_requested"])
        self.assertEqual(jobs.handle(self.root, "start", payload)["state"], "canceled")
        self.launch.assert_called_once()

    def test_small_byte_limit_preserves_unicode_and_read_offsets(self):
        job_id = "test-3"
        jobs.handle(self.root, "start", {"id": job_id, "argv": ["echo", "Chinese"]})
        folder = self.root / "jobs" / "job-test-3"
        (folder / "stdout.log").write_bytes("文图x".encode("utf-8"))
        first = jobs.handle(self.root, "log", {"id": job_id, "limit": 1})
        second = jobs.handle(
            self.root, "log", {"id": job_id, "limit": 1, "offset": first["next_offset"]},
        )
        tail = jobs.handle(self.root, "tail", {"id": job_id, "limit": 2})
        self.assertEqual((first["text"], first["next_offset"]), ("文", 3))
        self.assertEqual((second["text"], second["next_offset"]), ("图", 6))
        self.assertEqual(tail["text"], "x")


if __name__ == "__main__":
    unittest.main()
