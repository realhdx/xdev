"""A waiting local chat can shorten an idle cloud listener's sleep without bypassing backoff."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.adapters import inprocess_cli
from src.runtime import github_demand, github_mail, messages


class DemandWindow(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.endpoint = {"device": "local-test", "client": "workspace", "session": "chat-a"}
        self.mailbox = {"repository": "unit/test", "repository_id": 6, "release_id": 12, "peers": {"second": {}}}
        self.configure()

    def configure(self, enabled=True):
        config = {"device": "local-test"}
        if enabled:
            config["github_mail"] = self.mailbox
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf8")

    def inbox(self, seconds):
        return inprocess_cli.invoke(
            ["msg", "inbox", "--wait", str(seconds), "--as-client", "workspace", "--as-session", "chat-a"],
            root=self.root,
        )

    def test_only_an_empty_positive_scoped_wait_signals(self):
        d = self.root / "state" / "github-demand"
        self.assertNotIn("github_poll_demand", self.inbox(0))
        self.assertFalse(d.exists())
        self.configure(False)
        self.assertNotIn("github_poll_demand", self.inbox(0.02))
        self.assertFalse(d.exists())
        self.configure()
        messages.handle(
            self.root,
            "deliver",
            {
                "from": "second",
                "to": "local-test",
                "id": "one",
                "text": "a",
                "subject": "topic",
                "recipient_identity": self.endpoint,
            },
        )
        self.assertEqual(self.inbox(15)["status"], "received")
        self.assertFalse(d.exists())
        messages.handle(self.root, "receipt", {"id": "one", "reader_endpoint": self.endpoint})
        result = self.inbox(0.07)
        self.assertEqual((result["status"], result["github_poll_demand"]), ("timeout", "signaled"))
        self.assertEqual(list(d.iterdir()), [])

    def test_overlaps_crash_expiry_and_no_inbox_information_in_markers(self):
        d = github_demand._directory(self.root)
        old = d / ("wait-1-" + "f" * 32)
        old.touch()
        unrelated = d / "unrelated"
        unrelated.touch()
        with github_demand.request(self.root, 2) as requested:
            self.assertEqual(requested, "signaled")
            self.assertFalse(old.exists())
            with github_demand.request(self.root, 3):
                names = [p for p in d.iterdir() if p != unrelated]
                self.assertEqual(len(names), 2)
                for path in names:
                    self.assertEqual(path.stat().st_size, 0)
                    self.assertIsNotNone(github_demand.MARKER.fullmatch(path.name))
            self.assertTrue(github_demand._active(d))
        self.assertFalse(github_demand._active(d))
        self.assertTrue(unrelated.is_file())

    def test_active_window_obeys_minimum_and_server_cooldown(self):
        now = [100.0]

        @contextmanager
        def untouched_watch(directory):
            def changed(_):
                raise AssertionError("existing interest was lost")

            yield changed

        with (
            github_demand.request(self.root, 30),
            patch.object(messages, "_watch", untouched_watch),
            patch.object(github_demand.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(
                github_demand.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)
            ) as slept,
        ):
            github_demand.wait(self.root, 300, 20)
            self.assertEqual(slept.call_args.args, (20,))
            self.assertEqual(now[0], 120)
            github_demand.wait(self.root, 900, 900)
            self.assertEqual(slept.call_args.args, (900,))

    def test_rate_limit_and_pending_delivery_use_separate_floors(self):
        result = []

        def paused(root, delay, floor):
            result.append((delay, floor))
            if len(result) == 8:
                raise StopIteration()

        from unittest.mock import Mock

        trace = [[], [], [], [], [], github_mail.Unavailable("rate", 900), [{"status": "asset_pending"}], []]
        with (
            patch.object(github_mail, "Settings"),
            patch.object(github_mail, "Api"),
            patch.object(github_mail, "_collect", side_effect=trace),
            patch.object(github_demand, "wait", side_effect=paused),
            self.assertRaises(StopIteration),
        ):
            github_mail.follow(self.root, Mock())
        self.assertEqual(result[-3:], [(900, 900), (20, 20), (20, 20)])

    @unittest.skipUnless(sys.platform in ("win32", "linux"), "requires a real OS file notification")
    def test_real_idle_follower_and_chat_in_separate_processes(self):
        # Only the provider result and elapsed idle history are simulated. Both
        # Windows and Linux execute the actual follower, CLI waiter, OS notifications
        # and SQLite mailbox across distinct processes.
        child = """
import json,sys,time
from pathlib import Path
from unittest.mock import patch
from src.runtime import github_demand,github_mail,messages
root=Path(sys.argv[1]);ready=root/'sleeping'; awakened=False
native_wait=github_demand.wait
def interval(root,delay,floor):
    global awakened
    if delay<300: return
    assert floor==20
    start=time.monotonic();ready.write_text('sleeping')
    native_wait(root,delay,0.35)
    awakened=True
    (root/'elapsed').write_text(str(time.monotonic()-start))
def collect(*args):
    if awakened:
        messages.handle(root,'deliver',{'id':'observed','from':'second','to':'local-test','text':'test',
                                       'recipient_identity':{'client':'workspace','session':'chat-a','device':'local-test'}})
        raise SystemExit(0)
    return []
with patch.object(github_mail,'Settings'),patch.object(github_mail,'Api'),patch.object(github_mail,'_collect',side_effect=collect),patch.object(github_demand,'wait',side_effect=interval):
    github_mail.follow(root,lambda event:None)
"""
        env = {**os.environ, "XDEV_HOME": str(self.root)}
        env.pop("CODEX_THREAD_ID", None)
        cwd = Path(__file__).resolve().parents[1]
        with subprocess.Popen(
            [sys.executable, "-B", "-c", child, str(self.root)],
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        ) as follower:
            try:
                end = time.monotonic() + 7
                while not (self.root / "sleeping").exists() and follower.poll() is None and time.monotonic() < end:
                    time.sleep(0.025)
                self.assertTrue((self.root / "sleeping").exists())
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "src.cli.main",
                        "msg",
                        "inbox",
                        "--wait",
                        "6",
                        "--as-client",
                        "workspace",
                        "--as-session",
                        "chat-a",
                    ],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
                notice = json.loads(result.stdout)
                self.assertEqual([m["id"] for m in notice["messages"]], ["observed"])
                self.assertEqual(notice["github_poll_demand"], "signaled")
                self.assertEqual(follower.wait(timeout=3), 0)
                span = float((self.root / "elapsed").read_text())
                self.assertGreaterEqual(span, 0.34)
                self.assertLess(span, 5)
            finally:
                if follower.poll() is None:
                    follower.terminate()
