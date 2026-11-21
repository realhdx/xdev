import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.adapters import inprocess_cli
from src.runtime import surfaces


class SurfaceDispatchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_help_and_normal_entry(self):
        self.assertIn("--provider", inprocess_cli.help_text("surface", "targets"))
        with patch("src.adapters.surface_windows.run", return_value={"ok": True, "targets": []}) as run:
            result = inprocess_cli.invoke(["surface", "targets", "--provider", "windows", "--app", "Demo"], self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "windows")
        run.assert_called_once_with("targets", app="Demo")

    def test_slow_open_returns_a_runnable_read_for_the_original_peer(self):
        reply = {"ok": True, "status": "opened_pending", "target": "17"}
        (self.root / "config.json").write_text(json.dumps({"device": "lab", "peers": {"study": {"ssh": "study"}}}))
        with patch("src.cli.main._call", return_value=reply):
            result = inprocess_cli.invoke(["surface", "open", "--provider", "browser", "--host", "study",
                                           "--value", "https://example.org"], self.root)
        self.assertEqual(result["next_call"], {"action": "snapshot", "provider": "browser",
                                               "host": "study", "target": "17"})
        self.assertGreater(result["suggested_wait_seconds"], 0)
        help_text = inprocess_cli.help_text("surface", "open")
        self.assertIn("opened_pending", help_text)
        self.assertNotIn("{browser,windows}", help_text)

    def test_uncertain_actions_and_transient_reads_have_read_only_guidance(self):
        for action, target, expected in (("open", None, "targets"), ("invoke", "5", "snapshot"),
                                         ("close", "5", "targets")):
            result = surfaces.guide({"ok": False, "outcome": "unknown"}, action, "browser", "local", target)
            self.assertEqual(result["next_call"]["action"], expected)
        result = surfaces.guide({"ok": False, "status": "browse_command_failed"}, "snapshot", "browser", "lab", "5")
        self.assertEqual(result["next_call"]["target"], "5")
        failure = {"ok": False, "status": "browser_disconnected"}
        self.assertNotIn("next_call", surfaces.guide(failure, "snapshot", "browser", "lab", "5"))
        dispatched = surfaces.guide({"ok": True, "status": "dispatched"}, "invoke", "windows", "local", "w")
        self.assertEqual(dispatched["next_call"]["action"], "snapshot")

    def test_remote_access_is_selected_by_receiver_only(self):
        data = {"provider": "windows", "target": "demo"}
        with patch("src.adapters.surface_windows.run", return_value={"ok": True}) as run:
            result = surfaces.handle(self.root, "snapshot", {**data, "remote": "interact"}, remote=True)
            self.assertEqual(result["error_code"], "SURFACE_REMOTE_DISABLED")
            run.assert_not_called()
            (self.root / "config.json").write_text(json.dumps({"surface_remote": {"windows": "read"}}))
            self.assertTrue(surfaces.handle(self.root, "snapshot", data, remote=True)["ok"])
            result = surfaces.handle(self.root, "invoke", {**data, "ref": "x"}, remote=True)
            self.assertEqual(result["error_code"], "SURFACE_REMOTE_DISABLED")
            self.assertEqual(run.call_count, 1)

    def test_rpc_rejects_script_or_unscoped_operation(self):
        with self.assertRaises(ValueError):
            surfaces.handle(self.root, "snapshot", {"provider": "browser", "code": "x"})
        with self.assertRaises(ValueError):
            surfaces.handle(self.root, "invoke", {"provider": "windows", "ref": "x"})
