"""Admission and cleanup tests; the GitHub API runs entirely in memory."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import runpy
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from src.runtime import github_capacity as capacity, github_mail as gh
from src.runtime.mail_routes import NotSubmitted


class GitHubCapacity(unittest.TestCase):
    def setUp(self):
        fixture = runpy.run_path(str(Path(__file__).with_name("test_github_mail.py")))[
            "GitHubMail"
        ]
        fixture.setUp(self)
        self.root = self.sites["alpha"]

    def test_count_bytes_and_retry_before_the_only_post(self):
        self.assertEqual(capacity.limits({}), (500, 64 * 1024 * 1024))
        for bad in (
            {"max_pending_assets": True},
            {"max_pending_bytes": 0},
            {"max_pending_assets": 1001},
        ):
            with self.assertRaises(ValueError):
                capacity.limits(bad)
        cfg = self.root / "config.json"
        raw = json.loads(cfg.read_bytes())
        raw["github_mail"]["max_pending_assets"] = 1
        raw["github_mail"]["max_pending_bytes"] = 1
        cfg.write_text(json.dumps(raw))
        self.fake.assets[10] = ({"id": 10, "name": "owner-file.zip", "size": 1}, b"x")
        with self.assertRaisesRegex(NotSubmitted, "capacity"):
            gh.publish(self.root, self.letter)
        self.assertIn(10, self.fake.assets)
        with closing(gh._journal(self.root)) as db:
            self.assertEqual(
                db.execute("SELECT attempted FROM uploads").fetchone()[0], 0
            )
        del self.fake.assets[10]
        with self.assertRaisesRegex(NotSubmitted, "capacity"):
            gh.publish(self.root, self.letter)
        self.assertEqual(
            [v for _, v, _, _ in self.fake.calls if v in ("DELETE", "POST")], []
        )
        raw["github_mail"]["max_pending_bytes"] = 2**20
        cfg.write_text(json.dumps(raw))
        self.assertTrue(gh.publish(self.root, self.letter)["ok"])
        self.assertEqual(sum(v == "POST" for _, v, _, _ in self.fake.calls), 1)

    def test_one_slot_reopens_after_receiver_removed_confirmed_upload(self):
        cfg = self.root / "config.json"
        raw = json.loads(cfg.read_bytes())
        raw["github_mail"]["max_pending_assets"] = 1
        cfg.write_text(json.dumps(raw))
        self.assertTrue(gh.publish(self.root, self.letter)["ok"])
        next_letter = {**self.letter, "id": "next-letter"}
        with self.assertRaisesRegex(NotSubmitted, "capacity"):
            gh.publish(self.root, next_letter)
        gh.listen(self.sites["beta"], limit=1, timeout=1)
        self.assertEqual(len(self.fake.assets), 0)
        self.assertTrue(gh.publish(self.root, next_letter)["ok"])
        self.assertEqual(sum(v == "POST" for _, v, _, _ in self.fake.calls), 2)
        self.assertEqual(capacity.cleanup_status(self.root)["observations"], {})

    def test_hidden_unconfirmed_upload_continues_to_occupy_its_slot(self):
        cfg = self.root / "config.json"
        raw = json.loads(cfg.read_bytes())
        raw["github_mail"]["max_pending_assets"] = 1
        cfg.write_text(json.dumps(raw))
        self.fake.lose_upload = True
        with self.assertRaises(gh.Unavailable):
            gh.publish(self.root, self.letter)
        self.fake.assets.clear()
        self.fake.lose_upload = False
        with self.assertRaisesRegex(NotSubmitted, "capacity"):
            gh.publish(self.root, {**self.letter, "id": "unrelated-letter"})
        self.assertEqual(sum(v == "POST" for _, v, _, _ in self.fake.calls), 1)

    def test_snapshot_cannot_release_ack_that_arrived_during_listing(self):
        settings = gh.Settings(self.root)
        settings.capacity = (1, 10)
        with closing(gh._journal(self.root)) as db:
            with db:
                db.execute(
                    "INSERT INTO uploads(name,attempted,expires) VALUES('first',1,100)"
                )
                capacity.reserve(db, settings, settings.inbox, [], "first", 5, 100, 1)

            class Interleaved:
                def assets(self, box):
                    with db:
                        db.execute("UPDATE uploads SET asset=9 WHERE name='first'")
                    return []

            listing, confirmed = capacity.snapshot(db, settings.inbox, Interleaved())
            with self.assertRaisesRegex(NotSubmitted, "capacity"), db:
                capacity.reserve(
                    db,
                    settings,
                    settings.inbox,
                    listing,
                    "second",
                    5,
                    100,
                    1,
                    confirmed_before_listing=confirmed,
                )

    def test_simultaneous_new_names_cannot_both_claim_last_slot_from_stale_list(self):
        settings = gh.Settings(self.root)
        settings.capacity = (1, 100)
        sync = threading.Barrier(2)
        dest = self.root / "capacity.sqlite3"

        def claim(name):
            with closing(sqlite3.connect(dest, timeout=5)) as db:
                sync.wait(timeout=5)
                try:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        capacity.reserve(
                            db, settings, settings.inbox, [], name, 10, 100, 1
                        )
                    return True
                except NotSubmitted:
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim, ("a", "b"))), [False, True])
        with closing(sqlite3.connect(dest)) as db, db:
            existing = db.execute("SELECT name FROM capacity_claims").fetchone()[0]
            capacity.reserve(
                db,
                settings,
                settings.inbox,
                [{"name": existing, "size": 10}],
                existing,
                10,
                100,
                1,
            )

    def test_absent_and_mismatched_assets_do_not_create_deletion_or_delivery_evidence(
        self,
    ):
        gh.publish(self.root, self.letter)
        assetid, content = self.fake.assets.popitem()
        clock = time.time() + 65

        def sweep(offset):
            with patch.object(gh.time, "time", return_value=clock + offset):
                gh._sweep_sent(gh.Api(self.root), gh.Settings(self.root))
            return capacity.cleanup_status(self.root)["observations"]

        self.assertEqual(sweep(0), {"not_visible_outcome_unknown": 1})
        metadata, body = content
        self.fake.assets[assetid] = ({**metadata, "digest": "sha256:" + "0" * 64}, body)
        self.assertEqual(sweep(86401), {"identity_not_verified": 1})
        self.assertIn(assetid, self.fake.assets)
        self.fake.assets[assetid] = (metadata, body)
        self.assertEqual(sweep(2 * 86401), {"expired_asset_removed": 1})
        self.assertNotIn(assetid, self.fake.assets)
        self.assertFalse((self.root / "state/messages.sqlite3").exists())
