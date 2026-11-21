"""Windows headless worker tests with an isolated install and offline carrier polls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).with_name("mail-listener.py").resolve()
spec = importlib.util.spec_from_file_location("mail_listener_service", SOURCE)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
PROBE = '''
import json, os, runpy, sys
from pathlib import Path
root = Path(__file__).parent
os.environ["XDEV_HOME"] = str(root)
sys.path.insert(0, SOURCE)
from src.runtime import github_mail
def isolate(event, args):
    if event == "socket.connect":
        raise AssertionError("Network access is not part of this fixture")
sys.addaudithook(isolate)
def collect(api, settings, limit, seen):
    if (root / "finish").exists():
        raise github_mail.AccessDenied("PRIVATE-CONTENT-SENTINEL")
    if (root / "short_download").exists():
        return [{"status": "asset_pending", "reason": "incomplete_download", "body": "PRIVATE-CONTENT-SENTINEL"}]
    streams = root / "streams.json"
    if not streams.exists():
        streams.write_text(json.dumps({
            "no_console_stream": sys.__stdin__ is None, "stdin": sys.stdin.read(),
            "stdout": sys.stdout.writable(), "stderr": sys.stderr.writable()}))
    return [{"status": "stored", "body": "PRIVATE-CONTENT-SENTINEL"}]
github_mail.Settings = lambda root: object()
github_mail.POLL = .1
github_mail._collect = collect
runpy.run_module("src.cli.main", run_name="__main__", alter_sys=True)
'''


@unittest.skipUnless(os.name == "nt" and PYTHONW.is_file(), "requires Windows CPython GUI subsystem")
class MailListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "CrossDeviceCollab"
        self.state = self.root / "state" / service.SPOOL
        self.installed = self.root / "integrations/github-mail-listener.py"
        self.release = "a" * 64
        src = self.root / "releases" / self.release / "src"
        src.mkdir(parents=True)
        (src / "product.json").write_text('{"version":"1.1.0-preview.9"}')
        (self.root / "current.txt").write_text(self.release)
        (self.root / "config.json").write_text('{"github_mailbox":{}}')
        (self.root / "launch.py").write_text(PROBE.replace("SOURCE", repr(str(SOURCE.parents[1]))))
        self.installed.parent.mkdir()
        shutil.copyfile(SOURCE, self.installed)
        self.generation = hashlib.sha256(self.installed.read_bytes()).hexdigest()
        self.workers = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for worker in self.workers:
            if worker.poll() is None:
                worker.terminate()
            worker.wait(10)

    def start(self, launch_id):
        env = {**os.environ, "LOCALAPPDATA": self.tmp.name}
        worker = subprocess.Popen([str(PYTHONW), "-I", "-B", "-X", "utf8", str(self.installed),
                                   "run", "--launch-id", launch_id], env=env,
                                  creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
        self.workers.append(worker)
        return worker

    def test_install_waits_for_headless_heartbeat_and_reuses_same_process(self):
        current = {"registered": False, "state": "absent", "launch_id": None}
        def scheduler(action, pythonw, installed, launch_id):
            if action == "install":
                current.update(registered=True, state="Running", launch_id=launch_id)
                self.start(launch_id)
            return dict(current)

        with patch.dict(os.environ, LOCALAPPDATA=self.tmp.name), patch.object(service, "_scheduler", scheduler):
            started = time.monotonic()
            receipt = service.manage("install")
            self.assertTrue(receipt["worker_running"], receipt)
            self.assertGreaterEqual(time.monotonic() - started, service.STARTUP_BEAT)
            self.assertEqual(receipt["last_provider_event"], "activity_observed")
            self.assertEqual(json.loads((self.root / "streams.json").read_text()),
                             {"no_console_stream": True, "stdin": "", "stdout": True, "stderr": True})
            self.assertTrue(service.manage("install")["worker_running"])
            self.assertEqual(len(self.workers), 1)
            (self.root / "short_download").touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                diagnostic = service.manage("status")
                if diagnostic["last_provider_reason"] == "incomplete_download":
                    break
                time.sleep(.05)
            self.assertTrue(diagnostic["worker_running"])
            self.assertEqual(diagnostic["last_provider_event"], "asset_pending")
            self.assertEqual(diagnostic["last_provider_reason"], "incomplete_download")
            self.assertGreaterEqual(diagnostic["last_provider_at"], receipt["last_provider_at"])
            self.assertNotIn("SENTINEL", self.state.read_text())
            (self.root / "finish").touch()
            self.assertEqual(self.workers[0].wait(10), 1)
            report = service.manage("status")
            self.assertFalse(report["worker_running"])
            self.assertEqual((report["phase"], report["stage"], report["reason"]),
                             ("failed", "follow", "github_access_denied"))
            self.assertNotIn("SENTINEL", self.state.read_text())

    def test_rejects_old_heartbeat_and_dead_pid(self):
        task = {"registered": True, "state": "Running", "launch_id": "b" * 32}
        worker = {"phase": "running", "launch_id": task["launch_id"], "pid": os.getpid(),
                  "generation": self.generation, "updated_at": time.time(), "process_birth": 1}
        for changes in ({}, {"launch_id": "c" * 32}, {"generation": "old"},
                        {"updated_at": time.time() - 300, "process_birth": service._process_birth(os.getpid())}):
            service._save(self.state, {**worker, **changes})
            self.assertFalse(service._status(task, self.state, self.generation)["worker_running"])

    def test_restart_keeps_registration_and_waits_for_a_new_process(self):
        launch = "c" * 32
        task = {"registered": True, "state": "Running", "launch_id": launch}
        original = self.start(launch)
        deadline = time.monotonic() + 15
        while service._stored(self.state).get("phase") != "running" and time.monotonic() < deadline:
            time.sleep(.1)
        before = service._stored(self.state)
        self.assertEqual(before["phase"], "running")
        def scheduler(action, pythonw, installed, launch_id):
            if action == "restart":
                self.assertEqual(launch_id, launch)
                original.terminate()
                original.wait(10)
                self.start(launch_id)
            else:
                self.assertEqual(action, "status")
            return dict(task)
        with patch.dict(os.environ, LOCALAPPDATA=self.tmp.name), patch.object(service, "_scheduler", scheduler):
            result = service.manage("restart")
        after = service._stored(self.state)
        self.assertTrue(result["restarted"], result)
        self.assertEqual((result["release"], after["launch_id"]), (self.release, launch))
        self.assertNotEqual(before["process_birth"], after["process_birth"])
        self.assertEqual(len(self.workers), 2)

    def test_restart_without_registration_does_not_create_one(self):
        with patch.dict(os.environ, LOCALAPPDATA=self.tmp.name), patch.object(
            service, "_scheduler", return_value={"registered": False, "launch_id": None}
        ) as scheduler:
            with self.assertRaisesRegex(ValueError, "Install the receiver"):
                service.manage("restart")
            self.assertEqual([call.args[0] for call in scheduler.call_args_list], ["status"])

    def test_restart_cannot_use_a_heartbeat_from_the_original_process(self):
        launch = "d" * 32
        task = {"registered": True, "state": "Running", "launch_id": launch}
        service._save(self.state, {"phase": "running", "launch_id": launch, "pid": os.getpid(),
                      "generation": self.generation, "release": self.release, "updated_at": time.time(),
                      "process_birth": service._process_birth(os.getpid())})
        with patch.dict(os.environ, LOCALAPPDATA=self.tmp.name), patch.object(
            service, "_scheduler", return_value=task
        ), patch.object(service.time, "monotonic", side_effect=[0, 21]):
            result = service.manage("restart")
        self.assertTrue(result["worker_running"])
        self.assertFalse(result["restarted"])

    def test_immediate_headless_failure_is_diagnosed(self):
        (self.root / "config.json").unlink()
        worker = self.start("b" * 32)
        self.assertEqual(worker.wait(10), 1)
        failure = service._stored(self.state)
        self.assertEqual((failure["phase"], failure["stage"], failure["reason"]),
                         ("failed", "release_check", "required_file_missing"))


if __name__ == "__main__":
    unittest.main()
