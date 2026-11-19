"""Deterministic lifecycle regressions: these tests never run SSH or a command."""
import io
import json
import subprocess
import threading
import time
import unittest
from unittest import mock

from src.runtime import commands, transport


class StalledStream:
    def __init__(self, release, frame=None):
        self.release = release
        self.frame = frame

    def readline(self, limit):
        if self.frame is not None:
            return self.frame[:limit]
        self.release.wait()
        return b""

    def read(self, limit):
        self.release.wait()
        return b""

    def close(self):
        pass


class FakeSSH:
    def __init__(self, frame=None, *, never_wait=False, blocked_write=False):
        self.release = threading.Event()
        self.returncode = None
        self.stdin = io.BytesIO()
        if blocked_write:
            release = self.release
            class Writer:
                def write(self, data):
                    release.wait()
                def close(self):
                    pass
            self.stdin = Writer()
        self.stdout = StalledStream(self.release, frame)
        self.stderr = StalledStream(self.release)
        self.never_wait = never_wait
        self.kills = 0

    def kill(self):
        self.kills += 1

    def wait(self, timeout=None):
        if self.never_wait:
            self.release.wait()
        raise subprocess.TimeoutExpired("fake ssh", timeout)

    def communicate(self, *args, **kwargs):
        raise AssertionError("RPC completion must not wait for communicate/EOF")


class TransportLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(transport, "config", return_value={"device": "laptop"}))

    def call(self, proc, timeout=0.06):
        self.addCleanup(proc.release.set)
        with mock.patch.object(transport.subprocess, "Popen", return_value=proc) as factory:
            started = time.monotonic()
            result = transport.peer_request("desktop", {"service": "system"}, timeout)
            elapsed = time.monotonic() - started
        factory.assert_called_once()
        self.assertLess(elapsed, timeout + 0.45)
        self.assertGreaterEqual(proc.kills, 1)
        return result

    def test_valid_response_does_not_wait_for_exit_or_stderr_eof(self):
        proc = FakeSSH(b'{"ok":true,"device":"desktop"}\n', never_wait=True)
        result = self.call(proc)
        self.assertEqual(result, {"ok": True, "device": "desktop"})

    def test_valid_response_does_not_wait_for_blocked_stdin_writer(self):
        proc = FakeSSH(b'{"ok":true}\n', blocked_write=True)
        self.assertEqual(self.call(proc), {"ok": True})

    def test_timeout_kill_never_eof_and_never_exit_is_bounded_unknown(self):
        result = self.call(FakeSSH(never_wait=True))
        self.assertEqual(result["error_code"], "SSH_TIMEOUT")
        self.assertEqual(result["outcome"], "unknown")

    def test_negative_reply_is_authoritative_and_body_is_not_exit_status(self):
        self.assertEqual(self.call(FakeSSH(b'{"ok":false,"status":"deferred"}\n')),
                         {"ok": False, "status": "deferred"})
        self.assertEqual(self.call(FakeSSH(b'{"ok":true,"passed":false}\n')),
                         {"ok": True, "passed": False})

    def test_rejects_malformed_unframed_and_nonboolean_reply(self):
        for frame in (b"not-json\n", b'{"ok":true}', b'{"ok":1}\n', b'[]\n'):
            with self.subTest(frame=frame):
                result = self.call(FakeSSH(frame))
                self.assertEqual(result["error_code"], "SSH_TRANSPORT_ERROR")
                self.assertEqual(result["outcome"], "unknown")

    def test_limits_response_bytes_before_parsing(self):
        with mock.patch.object(transport, "RESPONSE_LIMIT", 64):
            result = self.call(FakeSSH(json.dumps({"ok": True, "data": "x" * 100}).encode() + b"\n"))
        self.assertEqual(result["error_code"], "SSH_TRANSPORT_ERROR")

    def test_rejects_nonfinite_or_zero_budget_before_start(self):
        with mock.patch.object(transport.subprocess, "Popen") as factory:
            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    transport.peer_request("desktop", {}, timeout)
        factory.assert_not_called()


class CommandProcess:
    def __init__(self, returncode=0, *, timeout=False):
        self.returncode = None
        self.exit_code = returncode
        self.timeout = timeout
        self.wait_calls = 0
        self.kills = 0
        self.stdin = self.stdout = self.stderr = None
        self.release = threading.Event()

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.timeout:
            if self.wait_calls > 1:
                self.release.wait()  # Kill did not produce an exit indication.
            raise subprocess.TimeoutExpired("fake command", timeout)
        self.returncode = self.exit_code
        return self.exit_code

    def kill(self):
        self.kills += 1


class CommandLifecycleTests(unittest.TestCase):
    def test_regular_file_output_does_not_require_pipe_eof(self):
        proc = CommandProcess()
        def launch(*args, **kwargs):
            kwargs["stdout"].write('中文 {"passed":false}\n'.encode())
            kwargs["stderr"].write(b"diagnostic")
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            return proc
        with mock.patch.object(commands.subprocess, "Popen", side_effect=launch):
            result = commands.run({"argv": ["fake"], "timeout": 0.05})
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn('"passed":false', result["stdout"])
        self.assertEqual(result["stderr"], "diagnostic")

    def test_nonzero_exit_preserves_contract(self):
        with mock.patch.object(commands.subprocess, "Popen", return_value=CommandProcess(7)):
            result = commands.run({"argv": ["fake"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)

    def test_timeout_cleanup_cannot_wait_indefinitely_after_kill(self):
        proc = CommandProcess(timeout=True)
        self.addCleanup(proc.release.set)
        with mock.patch.object(commands.subprocess, "Popen", return_value=proc):
            started = time.monotonic()
            result = commands.run({"argv": ["fake"], "timeout": 0.04})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.45)
        self.assertEqual(result["error_code"], "COMMAND_TIMEOUT")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(proc.kills, 1)


class RemoteFileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(transport, "config", return_value={"device": "laptop"}))

    def invoke(self, proc, request, timeout=0.05):
        from src.file_layer import remote_fs_helper
        self.addCleanup(proc.release.set)
        with mock.patch.object(transport.subprocess, "Popen", return_value=proc), \
             mock.patch.object(remote_fs_helper, "get_remote_bootstrap_b64", return_value="test-bootstrap"):
            started = time.monotonic()
            answer = remote_fs_helper.invoke_target_op("desktop", request, timeout)
        self.assertLess(time.monotonic() - started, timeout + 0.45)
        return answer

    def test_final_file_json_does_not_wait_for_ssh_exit(self):
        original = {"ok": True, "op": "stat", "host": "desktop", "target_path": "C:/fixture/中文.txt",
                    "elapsed_ms": 99999, "data": {"size": 3}, "error_code": None, "error_message": None}
        proc = FakeSSH(json.dumps(original, ensure_ascii=False).encode() + b"\n", never_wait=True)
        answer = self.invoke(proc, {"op": "stat", "path": original["target_path"]})
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["data"], {"size": 3})
        self.assertEqual(answer["target_path"], original["target_path"])
        self.assertLess(answer["elapsed_ms"], 500)

    def test_remote_operation_error_is_not_transport_failure(self):
        original = {"ok": False, "op": "write", "host": "desktop", "target_path": "C:/fixture.txt",
                    "elapsed_ms": 0, "data": {}, "error_code": "BASE_SHA256_MISMATCH"}
        answer = self.invoke(FakeSSH(json.dumps(original).encode() + b"\n"),
                             {"op": "write", "path": original["target_path"]})
        self.assertEqual(answer["error_code"], "BASE_SHA256_MISMATCH")
        self.assertNotIn("outcome", answer)

    def test_file_timeout_or_protocol_failure_keeps_envelope_and_unknown(self):
        for frame in (None, b"invalid\n"):
            with self.subTest(frame=frame):
                answer = self.invoke(FakeSSH(frame, never_wait=True),
                                     {"op": "write", "path": "C:/fixture.txt"})
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["error_code"], "SSH_TRANSPORT_ERROR")
                self.assertEqual(answer["outcome"], "unknown")
                self.assertEqual((answer["op"], answer["host"], answer["target_path"]),
                                 ("write", "desktop", "C:/fixture.txt"))
                self.assertIsInstance(answer["elapsed_ms"], int)


if __name__ == "__main__":
    unittest.main()
