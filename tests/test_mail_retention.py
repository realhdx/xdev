"""Exercise the public mailbox and retention paths in isolated device roots."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from src.runtime import mail_lifecycle, mail_retention as keep, messages
from src.runtime.letter_attachment import make_attachment

SENDER = {"device": "beta", "client": "review", "session": "writer"}
TARGET = {"device": "alpha", "client": "review", "session": "reader"}


class MailRetention(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.base / "alpha"
        self.root.mkdir()
        (self.root / "config.json").write_text(
            json.dumps({"device": "alpha", "peers": {"beta": {}}})
        )
        self.archive = self.base / "portable.sqlite3"
        self.payload = {
            "envelope_version": 2,
            "id": "a1",
            "from": "beta",
            "to": "alpha",
            "text": "Author's comment",
            "subject": "Review",
            "sender_endpoint": SENDER,
            "reply_endpoint": SENDER,
            "recipient_identity": TARGET,
            "recipient_endpoint": TARGET,
            "letter_attachment": make_attachment(
                {
                    "id": "original",
                    "from": "beta",
                    "to": "alpha",
                    "text": "Original notes",
                }
            ),
        }

    def send(self, identifier, *, read=False, **contents):
        letter = {**self.payload, "id": identifier, **contents}
        messages.handle(self.root, "deliver", letter)
        if read:
            messages.handle(
                self.root, "receipt", {"id": identifier, "reader_endpoint": TARGET}
            )
        return letter

    def record(self, identifier, action, target=None):
        return keep.record(self.root, identifier, action, target or self.archive)

    def test_archived_letter_preserves_routes_replay_and_original(self):
        letter = self.send("a1", read=True)
        before = messages.handle(self.root, "wait", {"id": "a1", "timeout": 0})[
            "message"
        ]
        self.assertEqual(self.record("a1", "archive")["status"], "archived")
        after = messages.handle(self.root, "wait", {"id": "a1", "timeout": 0})[
            "message"
        ]
        self.assertTrue(after["content_archived"])
        for key in (
            "id",
            "subject",
            "read_at",
            "read_by_endpoint",
            "reply_endpoint",
            "recipient_identity",
        ):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["text"], "")
        self.assertEqual(
            messages.handle(self.root, "deliver", letter)["status"], "duplicate"
        )
        with self.assertRaisesRegex(ValueError, "different content"):
            messages.handle(self.root, "deliver", {**letter, "text": "changed"})
        offline = self.base / "recovered.json"
        keep.extract(self.archive, "alpha", "a1", offline)
        recovered = json.loads(offline.read_bytes())
        self.assertEqual(recovered["text"], self.payload["text"])
        self.assertEqual(
            recovered["letter_attachment"], self.payload["letter_attachment"]
        )
        with self.assertRaises(FileExistsError):
            keep.extract(self.archive, "alpha", "a1", offline)
        self.assertEqual(self.record("a1", "restore")["status"], "restored")
        restored = messages.handle(self.root, "wait", {"id": "a1", "timeout": 0})[
            "message"
        ]
        self.assertEqual(
            restored["letter_attachment"], self.payload["letter_attachment"]
        )
        self.assertEqual(restored["text"], self.payload["text"])

    def test_ordinary_maintenance_only_archives_opted_in_completed_mail(self):
        for identifier in ("unread", "read", "held", "ready"):
            self.send(identifier, read=identifier != "unread")
        with self.assertRaisesRegex(ValueError, "Unread"):
            self.record("unread", "mark-processed")
        with self.assertRaisesRegex(ValueError, "Unread"):
            self.record("unread", "archive")
        for identifier in ("held", "ready"):
            self.record(identifier, "mark-processed")
        self.record("held", "protect")
        with self.assertRaisesRegex(ValueError, "protected"):
            self.record("held", "archive")
        self.assertEqual(
            keep.policy(self.root, self.archive, max_bytes=0)["selected"], ["ready"]
        )
        self.assertFalse(self.archive.exists())
        run = keep.policy(self.root, self.archive, max_bytes=0, apply=True)
        self.assertEqual(run["completed"], ["ready"])
        self.assertTrue(run["above_requested_capacity"])
        self.record("held", "unprotect")
        self.assertEqual(
            keep.policy(self.root, self.archive, keep_days=0, apply=True)["completed"],
            ["held"],
        )
        self.assertEqual(keep.usage(self.root)["archived"], 2)

    def test_verification_and_export_failure_leave_live_content_intact(self):
        self.send("a1", read=True)
        self.record("a1", "export")
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute("UPDATE blobs SET content=?", (b"damaged",))
        for action in ("verify", "archive"):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                self.record("a1", action)
        with self.assertRaises((ValueError, OSError)):
            self.record("a1", "archive", self.base / "missing" / "archive.sqlite3")
        self.assertEqual(keep.usage(self.root)["archived"], 0)
        message = messages.handle(self.root, "wait", {"id": "a1", "timeout": 0})[
            "message"
        ]
        self.assertEqual(message["text"], self.payload["text"])

    def test_metadata_damage_and_original_id_collision_are_rejected(self):
        self.send("a1", read=True)
        self.record("a1", "export")
        with closing(sqlite3.connect(self.archive)) as db, db:
            db.execute("UPDATE letters SET metadata='{}'")
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.record("a1", "archive")
        self.assertEqual(keep.usage(self.root)["archived"], 0)

    def test_concurrent_archive_dedup_and_measured_size(self):
        body = "abcdefghij" * 3000
        for identifier in ("first", "second"):
            self.send(identifier, read=True, text=body)
        with ThreadPoolExecutor(max_workers=3) as pool:
            out = list(
                pool.map(
                    lambda i: self.record(i, "archive"), ("first", "second", "first")
                )
            )
        self.assertEqual(len({x["sha256"] for x in out}), 1)
        with closing(sqlite3.connect(self.archive)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM blobs").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT count(*) FROM letters").fetchone()[0], 2
            )
        self.assertEqual(keep.usage(self.root)["active_material_bytes"], 0)
        change = keep.compact(self.root)
        measured = (self.root / "state/messages.sqlite3").stat().st_size
        self.assertEqual(change["after"]["database_bytes"], measured)
        self.assertEqual(
            change["inbox_files_bytes_change"],
            measured - change["before"]["database_bytes"],
        )

    def test_restore_rejects_withdrawn_row(self):
        self.send("a1", read=True)
        self.record("a1", "archive")
        with messages._database(self.root) as db:
            db.execute("UPDATE messages SET withdrawn_at='fixture' WHERE id='a1'")
        with self.assertRaisesRegex(ValueError, "withdrawn"):
            self.record("a1", "restore")

    def test_original_reader_completed_report_is_a_retention_signal(self):
        self.send("done", read=True)
        self.send("open", read=True)

        def report(ident, state):
            self.assertTrue(
                mail_lifecycle.apply(
                    self.root,
                    {
                        "op": "progress",
                        "id": ident,
                        "actor": TARGET,
                        "work_state": state,
                    },
                )["ok"]
            )

        report("open", "in_progress")
        report("done", "completed")
        self.assertEqual(
            keep.policy(self.root, self.archive, keep_days=0)["selected"], ["done"]
        )
        self.assertEqual(
            keep.policy(self.root, self.archive, keep_days=0, apply=True)["completed"],
            ["done"],
        )

    def test_cli_in_a_real_runtime_archive_and_shared_folder_usage(self):
        repo = Path(__file__).resolve().parents[1]
        kit = self.base / "kit"
        (kit / "deploy").mkdir(parents=True)
        for name in ("deploy.py", "mail-retention.py"):
            shutil.copyfile(repo / "deploy" / name, kit / "deploy" / name)
        build = runpy.run_path(str(kit / "deploy/deploy.py"))["build"]
        digest = build(repo, kit / "runtime.zip")["release"]
        (kit / "release.json").write_text(json.dumps({"runtime_sha256": digest}))
        folder = self.base / "paired"
        for slot in ("in", "out"):
            (folder / slot).mkdir(parents=True)
        (folder / "in" / "mail.json").write_bytes(b"measure")
        (folder / "in" / "other.txt").write_bytes(b"other")
        command = [
            sys.executable,
            "-I",
            "-B",
            str(kit / "deploy/mail-retention.py"),
            "--root",
            str(self.root),
        ]
        result = subprocess.run(
            command + ["folder-usage", "--directory", str(folder)],
            cwd=self.base,
            capture_output=True,
            check=True,
        )
        metrics = json.loads(result.stdout)
        self.assertTrue(metrics["complete"])
        self.assertEqual(
            metrics["slots"]["in"], {"files": 1, "bytes": 7, "skipped_entries": 1}
        )
        bad = kit / "private.sqlite3"
        denied = subprocess.run(
            command + ["export", "a1", "--archive", str(bad)],
            cwd=self.base,
            capture_output=True,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("outside the product", json.loads(denied.stderr)["error"])
        self.assertFalse(bad.exists())
