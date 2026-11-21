"""Exercise the non-Python host contract through the actual CLI pipes."""

from contextlib import closing
import json
import os
from pathlib import Path
from queue import Queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from src.runtime import github_mail


class HostEventPipes(unittest.TestCase):
    def start_host(self, root):
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "src.cli.main", "msg", "host-events",
             "--peer", "alpha", "--client", "reviewer", "--session", "review-a"],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, "XDEV_HOME": str(root)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        def close_host():
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(close_host)
        lines = Queue()
        def observe():
            for line in process.stdout:
                lines.put(json.loads(line))
        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        self.assertEqual(lines.get(timeout=8), {"protocol":"xdev.mail-arrival.v1","event":"ready"})
        return process, lines

    def check(self, *, mismatched):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token, key = root / "token", root / "key"
        token.write_text("unused")
        key.write_text("ab"*32)
        config = {"device":"beta","peers":{"alpha":{"mail_transport":"github"}},
                  "github_mailbox":{"repository":"example/mail","repository_id":51,"release_id":2,
                  "token_file":str(token),"peers":{"alpha":{"receiver_apps":["reviewer"],"key_file":str(key)}}}}
        (root / "config.json").write_text(json.dumps(config))
        process, lines = self.start_host(root)
        target = {"device":"beta","client":"reviewer","session":"review-a"}
        author = {"device":"alpha","client":"reviewer","session":"author"}
        item = github_mail._accept(github_mail.Settings(root), "alpha", {
            "id":"letter","kind":"message","envelope_version":2,"from":"alpha","to":"beta",
            "recipient_endpoint":target,"recipient_identity":target,"sender_endpoint":author,
            "reply_endpoint":author,"subject":"private subject","text":"private body"}, None, False)
        event = lines.get(timeout=8)
        self.assertEqual(set(event), {"protocol","event","notice_id","inbox_seq"})
        self.assertEqual((event["event"],event["inbox_seq"]), ("mail_available",item["seq"]))
        self.assertRegex(event["notice_id"], r"^[a-f0-9]{64}$")
        ack = {"protocol":event["protocol"],"notice_id":"other" if mismatched else event["notice_id"],"status":"accepted"}
        process.stdin.write(json.dumps(ack) + "\n")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=8), 0, process.stderr.read())
        with closing(sqlite3.connect(root / "state/github-mail.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT state FROM host_arrivals").fetchall(),
                             [("unknown" if mismatched else "accepted",)])
        with closing(sqlite3.connect(root / "state/messages.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT read_at FROM messages WHERE seq=?", (item["seq"],)).fetchone()[0])

    def test_host_accepts_locally_generated_event_then_disconnects(self):
        self.check(mismatched=False)

    def test_unmatched_ack_cannot_confirm_wake(self):
        self.check(mismatched=True)
