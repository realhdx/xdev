"""Exercise mailbox snapshots using xdev's public storage operations on disposable Windows roots."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import migrate
from test_install import DEPLOY, KitFixture, NAMES, SOURCE

sys.path.insert(0, str(SOURCE))
from src.runtime import correspondence, letter_attachment, messages


def participant(session):
    return {"device": "laptop", "client": "test-chat", "session": session}


A, B, C = map(participant, ("作者", "读者", "合作者"))
GROUP = {"id": "migration-group", "members": [A, B, C]}


class ProductMigrationTests(KitFixture, unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="xdev-mail-")))
        self.source, self.target = self.base / "源机", self.base / "目标机"
        self.archive = self.base / "业务信箱.zip"
        for root in (self.source, self.target):
            DEPLOY.initialize(root)
            self.configure(root, device="laptop")
        self.directory = NAMES.DeviceDirectory({"device": "laptop"})

    def configure(self, root, **settings):
        (root / "config.json").write_bytes(DEPLOY.canonical_json(settings))

    def seed(self):
        for name in migrate.DATABASES:
            db = self.enterContext(closing(sqlite3.connect(self.source / "state" / name)))
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("BEGIN")
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        self.letter = {"id": "original", "from": "laptop", "to": "laptop", "text": "原信第一行\r\n第二行",
                       "sender_endpoint": A, "recipient_identity": B, "subject": "两机核对", "group": GROUP}
        delivered = messages.handle(self.source, "deliver", self.letter)
        self.attachment = letter_attachment.make_attachment(delivered["message"])
        self.read = messages.handle(self.source, "receipt", {"id": "original", "reader_endpoint": B})["message"]["read_at"]
        self.assertTrue(self.read)
        for kind in ("receipt", "reply"):
            messages.handle(self.source, "deliver", {**self.letter, "id": kind, "kind": kind, "reply_to": "original",
                            "sender_endpoint": B, "recipient_identity": A, "text": "已阅" if kind == "receipt" else "认可"})
        messages.handle(self.source, "deliver", {**self.letter, "id": "forward", "sender_endpoint": B,
                        "recipient_identity": C, "text": "请参看原件", "letter_attachment": self.attachment})
        self.accepted = {}
        for letter in ("original", "forward"):
            self.accepted = correspondence.batch_state(self.source, A, "batch-1", "original-plan",
                            receipt={"id": letter, "recipient_inbox": "confirmed"})
        for name in migrate.DATABASES:
            self.assertGreater((self.source / "state" / (name + "-wal")).stat().st_size, 0)

    def export(self):
        return migrate.export_snapshot(self.source, self.archive, "laptop", DEPLOY)

    def restore(self, checksum, **options):
        return migrate.restore_snapshot(self.target, self.archive, self.directory, checksum, DEPLOY, NAMES, **options)

    def cli(self, kit, root, *args, ok=True):
        result = subprocess.run([sys.executable, "-I", "-B", str(kit / "migrate.py"), "--root", str(root), *args],
                                capture_output=True, timeout=30)
        reply = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if ok else 1, (reply, result.stderr))
        self.assertEqual(reply["ok"], ok)
        return reply

    def test_real_wal_mailbox_and_group_ledger_round_trip_and_new_activity(self):
        self.seed()
        kit = self.kit("1.0.0")
        receipt = self.cli(kit, self.source, "export", "--archive", str(self.archive))
        self.assertEqual(receipt["databases"], sorted(migrate.DATABASES))
        with zipfile.ZipFile(self.archive) as bundle:
            self.assertEqual(set(bundle.namelist()), set(migrate.DATABASES) | {"manifest.json"})
        go = ("restore", "--sha256", receipt["sha256"], "--archive", str(self.archive))
        self.assertEqual(self.cli(kit, self.target, *go)["status"], "completed")
        original = messages.handle(self.target, "wait", {"id": "original", "timeout": 0})["message"]
        self.assertEqual((original["read_at"], original["read_by_endpoint"], original["subject"]),
                         (self.read, B, self.letter["subject"]))
        replies = messages.handle(self.target, "list", {"reply_to": "original"})["messages"]
        self.assertEqual([(m["id"], m["kind"]) for m in replies], [("receipt", "receipt"), ("reply", "reply")])
        forward = messages.handle(self.target, "wait", {"id": "forward", "timeout": 0})["message"]
        self.assertEqual((forward["letter_attachment"], forward["recipient_identity"], forward["read_at"]),
                         (self.attachment, C, None))
        self.assertEqual(forward["group"]["id"], GROUP["id"])
        self.assertEqual(correspondence.batch_state(self.target, A, "batch-1", "original-plan"), self.accepted)
        with self.assertRaisesRegex(ValueError, "different"):
            correspondence.batch_state(self.target, A, "batch-1", "other-plan")
        messages.handle(self.target, "deliver", {**self.letter, "id": "after-restore"})
        self.assertTrue(self.cli(kit, self.target, *go)["already_imported"])
        self.assertTrue(messages.handle(self.target, "wait", {"id": "after-restore", "timeout": 0})["message"])

    def test_interruption_after_one_database_resumes_without_overwriting_it(self):
        self.seed()
        checksum = self.export()["sha256"]
        original_rename = migrate.os.rename

        def interrupt(src, dst):
            if Path(dst).name == "messages.sqlite3":
                raise OSError("interruption after one published database")
            return original_rename(src, dst)

        with patch.object(migrate.os, "rename", side_effect=interrupt), self.assertRaisesRegex(OSError, "interruption"):
            self.restore(checksum)
        journal = self.target / "state/product/mail-import.json"
        self.assertEqual(json.loads(journal.read_bytes())["status"], "importing")
        first = self.target / "state/discussion-sends.sqlite3"
        stamp = (DEPLOY.digest(first), first.stat().st_mtime_ns)
        self.assertFalse((self.target / "state/messages.sqlite3").exists())
        self.assertEqual(self.restore(checksum)["status"], "completed")
        self.assertEqual((DEPLOY.digest(first), first.stat().st_mtime_ns), stamp)

    def test_alias_adoption_requires_matching_source_and_explicit_local_alias(self):
        self.seed()
        checksum = self.export()["sha256"]
        self.directory = NAMES.DeviceDirectory({"device": "新的主地址", "aliases": ["laptop"]})
        for opts in ({}, {"source_device": "desktop"}):
            with self.assertRaises(ValueError):
                self.restore(checksum, **opts)
        self.directory = NAMES.DeviceDirectory({"device": "新的主地址", "peers": {"laptop": {}}})
        with self.assertRaises(ValueError):
            self.restore(checksum, source_device="laptop")
        config = {"device": "新的主地址", "aliases": ["laptop"]}
        self.configure(self.target, **config)
        self.directory = NAMES.DeviceDirectory(config)
        receipt = self.restore(checksum, source_device="LAPTOP")
        self.assertEqual((receipt["device"], receipt["source_device"]), ("新的主地址", "laptop"))
        arrived = messages.handle(self.target, "list", {"recipient_endpoint": {**B, "device": "新的主地址"}})["messages"]
        self.assertEqual([m["id"] for m in arrived], ["original"])

    def test_custom_primary_name_migration_preserves_author_retry_ledger(self):
        kit = self.kit("1.0.0")
        source, target = "研究一站", "替换站"
        self.configure(self.source, device=source, aliases=["更早的站名"], identity_key=source)
        author = {**A, "device": source}
        accepted = correspondence.batch_state(self.source, author, "repeat", "same-work", receipt={"id": "first"})
        export = self.cli(kit, self.source, "export", "--archive", str(self.archive))
        self.assertEqual(export["identity_key"], source)
        self.assertEqual(set(export["aliases"]), {source, "更早的站名", NAMES.machine_name()})
        with zipfile.ZipFile(self.archive) as bundle:
            verified = json.loads(bundle.read("manifest.json"))
        self.assertEqual([export[k] for k in ("device", "aliases", "identity_key")],
                         [verified[k] for k in ("device", "aliases", "identity_key")])
        args = ("restore", "--archive", str(self.archive), "--sha256", export["sha256"], "--source-device", source)
        self.configure(self.target, device=target, aliases=[source], identity_key=target)
        self.assertIn("identity-key", self.cli(kit, self.target, *args, ok=False)["error"])
        self.assertFalse((self.target / "state/discussion-sends.sqlite3").exists())
        self.configure(self.target, device=target, aliases=[source], identity_key=source)
        self.assertIn("--alias 更早的站名", self.cli(kit, self.target, *args, ok=False)["error"])
        self.configure(self.target, device=target, aliases=[source, "更早的站名"], identity_key=source)
        self.assertEqual(self.cli(kit, self.target, *args)["identity_key"], source)
        self.assertEqual(correspondence.batch_state(self.target, {**author, "device": target},
                                                    "repeat", "same-work"), accepted)

    def test_legacy_compatibility_exports_canonical_name_and_keeps_historical_key(self):
        kit = self.kit("1.0.1")
        address = {"device": "laptop", "identity_key": "laptop", "primary_name": "正式站名", "legacy_clients": True,
                   "peers": {"Peer-PC": {"aliases": ["desktop"], "identity_key": "desktop"}}}
        self.configure(self.source, **address)
        self.configure(self.target, **address)
        correspondence.batch_state(self.source, {**A, "device": "正式站名"}, "one", "plan", receipt={"id": "accepted"})
        receipt = self.cli(kit, self.source, "export", "--archive", str(self.archive))
        self.assertEqual((receipt["device"], receipt["identity_key"]), ("正式站名", "laptop"))
        self.assertIn("laptop", receipt["aliases"])
        restored = self.cli(kit, self.target, "restore", "--sha256", receipt["sha256"], "--archive", str(self.archive))
        self.assertEqual((restored["device"], restored["identity_key"]), ("正式站名", "laptop"))

    def test_wrong_hash_existing_database_and_sidecar_do_not_publish_any_other_database(self):
        self.seed()
        checksum = self.export()["sha256"]
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.restore("0" * 64)
        wal = self.target / "state/messages.sqlite3-wal"
        wal.write_bytes(b"active client fixture")
        with self.assertRaisesRegex(ValueError, "Close mailbox"):
            self.restore(checksum)
        wal.unlink()
        messages.handle(self.target, "deliver", {**self.letter, "id": "belongs-to-target"})
        existing = self.target / "state/messages.sqlite3"
        before = DEPLOY.digest(existing)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.restore(checksum)
        self.assertEqual(DEPLOY.digest(existing), before)
        self.assertFalse((self.target / "state/discussion-sends.sqlite3").exists())
        self.assertFalse((self.target / "state/product/mail-import.json").exists())

    def test_malformed_source_schema_and_invalid_kit_or_configuration_are_rejected(self):
        file = self.source / "state/messages.sqlite3"
        with closing(sqlite3.connect(file)) as db:
            db.execute("CREATE TABLE messages(value)")
        with self.assertRaisesRegex(ValueError, "layout"):
            self.export()
        self.assertFalse(self.archive.exists())
        kit = self.kit("1.0.0")
        self.configure(self.source, device=[])
        args = ("export", "--archive", str(self.archive), "--device", "laptop")
        self.cli(kit, self.source, *args, ok=False)
        (kit / "deploy/deploy.py").write_text("raise RuntimeError('unexpected execution')", encoding="utf-8")
        error = self.cli(kit, self.source, *args, ok=False)
        self.assertNotIn("unexpected execution", error["error"])
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
