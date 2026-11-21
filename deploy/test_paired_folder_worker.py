import importlib.util
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("paired-folder-worker.py")
spec = importlib.util.spec_from_file_location("paired_worker", SCRIPT)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.root = self.state / "install"
        self.root.mkdir()
        self.pending = []
        self.result = "accepted"
        self.seen = []

    def call(self, script, args):
        if args[0] == "step":
            return {"exported": 0}
        if args[0] == "pending":
            return {"pending": list(self.pending)}
        self.assertEqual(script, "github-pr-notice.py")
        self.assertEqual(args[:4], ["notify", "--root", self.root, "--notice-id"])
        self.seen.append(args[-1])
        if isinstance(self.result, BaseException):
            raise self.result
        return {"status": self.result}

    def tick(self):
        return worker.cycle(self.state, self.root, "test-route", self.call)

    def test_new_pending_are_batched_and_old_pending_survive_restart_silently(self):
        self.assertEqual(self.tick()["last_notification"], "no_new_mail")
        self.pending.extend(["first", "second", "second"])
        self.assertEqual(self.tick()["accepted_count"], 2)
        self.assertEqual(self.tick()["last_notification"], "no_new_mail")
        self.pending.append("third")
        self.assertEqual(self.tick()["accepted_count"], 3)
        self.assertEqual(len(set(self.seen)), 2)

    def test_only_definite_non_submission_retries_at_due_time(self):
        self.pending = ["first"]
        self.result = "not_submitted"
        with patch.object(worker.time, "time", return_value=100):
            self.assertEqual(self.tick()["retry_count"], 1)
            self.assertEqual(self.tick()["last_notification"], "no_new_mail")
        self.result = OSError("ack lost")
        with patch.object(worker.time, "time", return_value=112):
            self.assertEqual(self.tick()["unknown_count"], 1)
        with patch.object(worker.time, "time", return_value=10000):
            self.assertEqual(self.tick()["last_notification"], "no_new_mail")
        self.assertEqual(self.seen, [self.seen[0], self.seen[0]])

    def test_claim_persists_after_crash(self):
        self.pending = ["first"]
        self.result = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        self.assertEqual(self.tick()["unknown_count"], 1)
        self.assertEqual(len(self.seen), 1)

    def test_concurrent_worker_cannot_duplicate_in_flight_notification(self):
        self.pending = ["first"]
        entered, release = threading.Event(), threading.Event()
        output = []
        def invoke(script, args):
            if args[0] == "notify":
                entered.set()
                self.assertTrue(release.wait(10))
            return self.call(script, args)
        t = threading.Thread(target=lambda: output.append(worker.cycle(self.state, self.root, "test-route", invoke)))
        t.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(self.tick()["last_notification"], "no_new_mail")
        finally:
            release.set()
            t.join(10)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(output[0]["accepted_count"], 1)

    def test_all_pending_pages_scanned_before_one_new_batch(self):
        self.pending = ["old"]
        self.tick()
        def pages(script, args):
            if args[0] == "pending":
                return ({"pending": ["new"], "has_more": False} if "--after" in args else
                        {"pending": ["old"], "has_more": True, "next_cursor": 80})
            return self.call(script, args)
        result = worker.cycle(self.state, self.root, "test-route", pages)
        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(len(self.seen), 2)

    def test_invalid_page_or_id_cannot_emit_notification(self):
        self.pending = ["valid", "../../shared/request"]
        with self.assertRaises(ValueError):
            self.tick()
        self.assertFalse(self.seen)

    def test_enrolled_origins_select_stable_route_and_installation(self):
        cfg = dict(local_device="home", local_session="target", peer_device="office", peer_client="desktop",
                   peer_session="source", root=str(self.root))
        with closing(sqlite3.connect(self.state / "mail.sqlite")) as db, db:
            db.execute("CREATE TABLE settings(id INTEGER, config TEXT)")
            db.execute("INSERT INTO settings VALUES(1,?)", (json.dumps(cfg),))
        original = worker.route_identity(self.state, self.root)
        self.assertEqual(original, worker.route_identity(self.state, self.root))
        with self.assertRaises(ValueError):
            worker.route_identity(self.state, self.state)
        with closing(sqlite3.connect(self.state / "mail.sqlite")) as db, db:
            db.execute("UPDATE settings SET config=?", (json.dumps({**cfg, "peer_session": "other"}),))
        self.assertNotEqual(original, worker.route_identity(self.state, self.root))

    def test_stopped_loop_updates_a_new_startup_id_and_records_healthy_cycle(self):
        records = []
        for _ in range(2):
            def record(path, values):
                records.append(dict(values))
                if values["phase"] == "running":
                    raise KeyboardInterrupt()
            with patch.object(worker, "cycle", return_value={"last_notification": "no_new_mail"}), patch.object(
                worker, "write_heartbeat", side_effect=record
            ), self.assertRaises(KeyboardInterrupt):
                worker.follow(self.state, self.root, "route")
        starts = [row["startup_id"] for row in records if row["phase"] == "starting"]
        self.assertEqual(len(set(starts)), 2)
        self.assertEqual([r["cycle_ok"] for r in records if r["phase"] == "running"], [True, True])
        self.assertTrue(all(row["pid"] == os.getpid() for row in records))

    @unittest.skipUnless(os.name == "nt", "Windows headless receiver")
    def test_detached_pythonw_serves_with_no_console_across_two_processes(self):
        scripts = self.state / "installed"
        scripts.mkdir()
        entry = scripts / SCRIPT.name
        shutil.copyfile(SCRIPT, entry)
        (scripts / "paired-folder-mail.py").write_text(
            "import json,sys\nprint(json.dumps({'pending':['mail-1']} if sys.argv[1]=='pending' else {}))\n"
        )
        marker = scripts / "notifications"
        (scripts / "github-pr-notice.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "with Path(__file__).with_name('notifications').open('a') as f: f.write(sys.argv[-1]+'\\n')\n"
            "print('{\u0022status\u0022:\u0022accepted\u0022}')\n"
        )
        cfg = dict(local_device="local", local_session="in", peer_device="peer", peer_client="writer",
                   peer_session="out", root=str(self.root))
        with closing(sqlite3.connect(self.state / "mail.sqlite")) as db, db:
            db.execute("CREATE TABLE settings(id INTEGER,config TEXT)")
            db.execute("INSERT INTO settings VALUES(1,?)", (json.dumps(cfg),))
        births = []
        for _ in range(2):
            process = subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), "-I", "-B", str(entry),
                                        "--follow", "--state", str(self.state), "--xdev-root", str(self.root)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS)
            try:
                record = {}
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    status = self.state / worker.STATUS
                    if status.is_file():
                        record = json.loads(status.read_text())
                        if record.get("pid") == process.pid and record.get("cycle_ok"):
                            break
                    time.sleep(.1)
                self.assertEqual((record.get("pid"), record.get("accepted_count")), (process.pid, 1), record)
                births.append(record["startup_id"])
            finally:
                process.terminate()
                process.wait(15)
        self.assertEqual(len(set(births)), 2)
        self.assertEqual(len(marker.read_text().splitlines()), 1)


class SignalManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root, self.state, self.shared = [self.base / name for name in ("install", "private", "shared")]
        for path in (self.root, self.state, self.shared):
            path.mkdir()
        self.cfg = dict(local_device="home", local_session="chat", peer_device="remote",
                        peer_client="client", peer_session="other", root=str(self.root), folder=str(self.shared))
        with closing(sqlite3.connect(self.state / "mail.sqlite")) as db, db:
            db.execute("CREATE TABLE settings (id INTEGER,config TEXT)")
            db.execute("INSERT INTO settings VALUES (1,?)", (json.dumps(self.cfg),))
        self.route = worker.route_identity(self.state, self.root)
        pr = dict(enabled=True, repository_id=71, pr_number=2, pr_issue_id=31, branch="xdev-mailbox-notifications",
                  anchor="a" * 40, tree="b" * 40, actor="alice", actor_id=8)
        (self.root / "config.json").write_text(json.dumps({
            "github_pr_notice": pr, "github_mailbox": {"repository": "alice/messages", "repository_id": 71}
        }))
        # Matches the released notifier's ledger key, including its canonical JSON separators.
        self.scope = hashlib.sha256(json.dumps([71, 2, 31, pr["branch"], pr["anchor"], pr["tree"], 8]).encode()).hexdigest()
        self.provider = self.root / "github-pr-notices.sqlite3"
        with closing(sqlite3.connect(self.provider)) as db:
            db.execute("CREATE TABLE notices(route TEXT,notice TEXT,started INTEGER,state TEXT,sha TEXT,PRIMARY KEY(route,notice))")
        self.provider.chmod(0o600)

    def seed(self, mid, token, sha, *, host="accepted", provider="published", route=None, scope=None):
        with closing(worker.journal(self.state)) as db, db:
            db.execute("INSERT INTO notices VALUES(?,?,?,?,1,0)", (route or self.route, mid, token, host))
        with closing(sqlite3.connect(self.provider)) as db, db:
            db.execute("INSERT OR IGNORE INTO notices VALUES(?,?,0,?,?)", (scope or self.scope, token, provider, sha))

    def test_correlates_two_historic_notices_and_a_batch_without_latest_or_body(self):
        t1, t2, s1, s2 = "1" * 64, "2" * 64, "3" * 40, "4" * 40
        self.seed("first", t1, s1)
        self.seed("second", t2, s2)
        self.seed("same-batch", t2, s2)
        self.seed("foreign", t2, s2, route="another-address")
        with patch.object(worker, "invoke", side_effect=AssertionError("network or delivery called")):
            self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 2)
            files = sorted((self.shared / "signals").glob("*.json"))
            self.assertEqual([x.stem for x in files], [t1, t2])
            for (file, sha, proposals) in zip(files, (s1, s2), (["first"], ["second","same-batch"])):
                self.assertEqual(json.loads(file.read_text()), {
                    "schema":"xdev.paired-folder.notice.v1", "commit_sha":sha, "message_ids":proposals
                })
            old = [x.stat().st_mtime_ns for x in files]
            self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 0)
            self.assertEqual(old, [x.stat().st_mtime_ns for x in files])
            files[0].write_text("{malformed")
            files[1].unlink()
            self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 2)

    def test_only_exact_published_and_accepted_pair_is_exposed(self):
        for index, options in enumerate([{"host":"unknown"},{"host":"not_submitted"},{"provider":"rejected"},
                                         {"provider":"creating"},{"route":"another-route"},{"scope":"other-scope"}]):
            self.seed("letter" + str(index), str(index) * 64, "f" * 40, **options)
        self.seed("bad-sha", "6" * 64, "a" * 39)
        self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 0)
        self.assertEqual(list((self.shared / "signals").glob("*.json")), [])

    def test_refuses_linked_signal_targets(self):
        token = "7" * 64
        self.seed("letter", token, "a" * 40)
        folder = self.shared / "signals"
        folder.mkdir()
        target = self.base / "other"
        target.write_text("leave alone")
        os.link(target, folder / (token + ".json"))
        with self.assertRaises((ValueError, OSError)):
            worker.recover_signals(self.state, self.root, self.route)
        self.assertEqual(target.read_text(), "leave alone")

    def test_enriches_parent_only_for_the_confirmed_notice_and_commit(self):
        token, sha, parent = "5" * 64, "a" * 40, "b" * 40
        self.seed("mail", token, sha)
        self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 1)
        path = self.shared / "signals" / (token + ".json")
        def wrong_proof(script, args):
            return {"status": "accepted", "commit_sha": "c" * 40, "parent_sha": parent}
        self.assertEqual(worker.recover_signals(self.state, self.root, self.route,
                         lookup_parents=True, call=wrong_proof), 0)
        self.assertNotIn("parent_sha", json.loads(path.read_text()))
        def confirmed(script, args):
            self.assertEqual((script, args), ("github-pr-notice.py", ["proof", "--root", self.root, "--notice-id", token]))
            return {"status": "accepted", "commit_sha": sha, "parent_sha": parent}
        self.assertEqual(worker.recover_signals(self.state, self.root, self.route,
                         lookup_parents=True, call=confirmed), 1)
        self.assertEqual(json.loads(path.read_text())["parent_sha"], parent)
        with closing(sqlite3.connect(self.provider)) as db, db:
            db.execute("ALTER TABLE notices ADD COLUMN parent_sha TEXT")
            db.execute("UPDATE notices SET parent_sha=?", (parent,))
        self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 0)

    def test_receipts_and_unrelated_log_schemas_cannot_construct_manifest(self):
        self.seed("known", "2" * 64, "e" * 40)
        with closing(sqlite3.connect(self.provider)) as db, db:
            db.execute("DROP TABLE notices")
        self.assertEqual(worker.recover_signals(self.state, self.root, self.route), 0)

    def test_normal_cycle_exports_a_fresh_confirmed_batch_only_once(self):
        sha = "e" * 40
        notices = []
        def adapters(script, args):
            if args[0] == "step":
                return {}
            if args[0] == "pending":
                return {"pending": ["one", "two"]}
            notice = args[-1]
            self.assertEqual(args[:4], ["notify", "--root", self.root, "--notice-id"])
            notices.append(notice)
            with closing(sqlite3.connect(self.provider)) as db, db:
                db.execute("INSERT INTO notices VALUES(?,?,0,'published',?)", (self.scope, notice, sha))
            return {"status": "accepted"}
        worker.cycle(self.state, self.root, self.route, adapters)
        worker.cycle(self.state, self.root, self.route, adapters)
        self.assertEqual(len(notices), 1)
        received = json.loads((self.shared / "signals" / (notices[0] + ".json")).read_text())
        self.assertEqual((received["commit_sha"], received["message_ids"]), (sha, ["one", "two"]))

if __name__ == "__main__":
    unittest.main()
