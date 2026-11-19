"""Exercise actual local entry points and a self-contained Windows file receiver."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.file_layer import remote_fs_helper, transfer, xdev_file
from src.runtime import device_names, transport


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"COMPUTERNAME": "Studio-A", "XDEV_HOME": str(self.root)}))
        self.config = {"device": "研究室", "machine_name": "Studio-A", "aliases": ["laptop"],
                       "peers": {"分析机": {"ssh": "test.peer", "aliases": ["desktop", "Studio-B"]}}}

    def save(self):
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def test_fresh_native_name_and_legacy_pair(self):
        self.assertEqual(transport.directory(self.root).describe(), {
            "device": "Studio-A", "machine_name": "Studio-A", "identity_key": "studio-a", "peers": [], "aliases": [],
            "legacy_clients": False})
        with self.assertRaises(ValueError):
            transport.ssh_target(self.root, "desktop")
        self.assertIsNone(transport.ssh_target(self.root, "studio-a"))
        legacy = device_names.DeviceDirectory({"device": "laptop", "nats": {}})
        self.assertEqual(legacy.node, "laptop")
        self.assertEqual(legacy.resolve("Studio-A"), "laptop")
        self.assertEqual(legacy.ssh_target("desktop"), "desktop")

    def test_aliases_labels_and_ssh_namespace(self):
        self.save()
        directory = transport.directory(self.root)
        for alias in ("Studio-A", "local", "LOCAL", "LAPTOP", "研究室"):
            self.assertEqual(directory.resolve(alias), "研究室")
            self.assertIsNone(directory.ssh_target(alias))
        for alias in ("分析机", "STUDIO-b", "Desktop", "TEST.PEER"):
            self.assertEqual(directory.resolve(alias), "分析机")
            self.assertEqual(directory.ssh_target(alias), "test.peer")
        self.assertEqual(directory.describe(), {"device": "研究室", "machine_name": "Studio-A", "identity_key": "laptop",
                         "aliases": ["Studio-A", "laptop"], "peers": ["分析机"], "legacy_clients": False})
        self.assertTrue(directory.same("desktop", "分析机"))
        self.assertTrue(directory.same("Unknown", "unknown"))
        self.assertNotIn("test.peer", directory.names_for("分析机"))
        with self.assertRaises(ValueError):
            directory.resolve("unknown")
        for bad in ("-p", "local", "remote.*", "a/b", "user@peer", ""):
            self.assertFalse(device_names.valid_name(bad))
            self.assertFalse(device_names.valid_ssh(bad))
        for peer in ("studio-A", "LaPtoP"):
            with self.assertRaises(ValueError):
                device_names.DeviceDirectory({**self.config, "peers": {peer: {}}})
        with self.assertRaises(ValueError):
            device_names.DeviceDirectory({**self.config, "peers": {"第二台": {}}})
        with patch.dict(os.environ, {"COMPUTERNAME": "Unrelated-C"}), self.assertRaisesRegex(ValueError, "bound"):
            transport.node(self.root)

    def test_stable_identity_across_rename_later_alias_and_alias_conflicts(self):
        cfg = {"device": "实验间", "identity_key": "Studio-A", "aliases": ["Studio-A"],
               "peers": {"新工位": {"aliases": ["旧工位"], "identity_key": "旧工位", "ssh": "lab-b"}}}
        self.assertEqual(device_names.DeviceDirectory(cfg).identity_key("实验间"), "studio-a")
        cfg["aliases"].append("laptop")
        cfg["peers"]["新工位"]["aliases"].append("desktop")
        directory = device_names.DeviceDirectory(cfg)
        self.assertEqual(directory.identity_key("laptop"), "studio-a")
        self.assertEqual(directory.identity_key("desktop"), "旧工位")
        for unknown in ("未登记", "新工位", "local"):
            with self.assertRaises(ValueError):
                device_names.DeviceDirectory({**cfg, "identity_key": unknown})

    def test_only_historical_role_pair_uses_legacy_mail_names_when_enabled(self):
        settings = {**self.config, "device": "laptop", "primary_name": "研究室", "legacy_clients": True}
        settings["peers"] = {**self.config["peers"], "Side-PC": {"ssh": "extra-host"}}
        directory = device_names.DeviceDirectory(settings)
        self.assertEqual(directory.describe()["device"], "研究室")
        self.assertTrue(directory.legacy_clients)
        self.assertEqual([directory.mail_device(x) for x in ("local", "研究室", "desktop", "分析机", "extra-host")],
                         ["laptop", "laptop", "desktop", "desktop", "Side-PC"])
        self.assertEqual(device_names.DeviceDirectory(self.config).mail_device("desktop"), "分析机")
        with self.assertRaises(ValueError):
            directory.mail_device("unregistered")
        for change in ({"primary_name": None}, {"device": "Studio-A"}, {"peers": {}},
                       {"identity_key": "Studio-A"}, {"legacy_clients": "yes"}):
            with self.assertRaises(ValueError):
                device_names.DeviceDirectory({**settings, **change})

    def test_local_aliases_use_physical_files_and_unknown_never_starts_ssh(self):
        self.save()
        path = self.root / "来信.txt"
        copied = self.root / "已转交.txt"
        with patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected remote process")):
            for payload in ({"op": "create", "host": "studio-A", "path": str(path), "content": "主题"},
                            {"op": "push", "host": "LAPTOP", "local": str(path), "remote": str(copied)},
                            {"op": "read", "host": "研究室", "path": str(copied)}):
                result = xdev_file.run_file_op(payload, root=self.root)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["host"], "研究室")
            self.assertEqual(copied.read_text(encoding="utf-8"), "主题")
            self.assertFalse(xdev_file.run_file_op({"op": "stat", "host": "unregistered", "path": str(path)})["ok"])
            with self.assertRaises(ValueError):
                transport.peer_request("unregistered", {}, 1, root=self.root)

    def test_wire_uses_ssh_alias_and_expected_canonical_peer(self):
        self.save()
        with patch.object(transport, "_json_request", return_value={"ok": True}) as rpc:
            transport.peer_request("desktop", {"service": "system"}, 1, root=self.root)
            self.assertIn("test.peer", rpc.call_args.args[0])
        with patch.object(remote_fs_helper, "_json_request", return_value={"ok": True}) as exchange:
            payload = {"op": "stat", "host": "Studio-B", "path": "sample", "remote_xdev_home": "C:/staging"}
            xdev_file.run_file_op(payload, root=self.root)
            argv, wire, _timeout = exchange.call_args.args
            self.assertIn("test.peer", argv)
            body = json.loads(wire.splitlines()[1])
            self.assertEqual((body["host"], body["expected_device"], body["xdev_home"]), ("分析机", "分析机", "C:/staging"))
        local = self.root / "data.txt"
        local.write_bytes(b"sample")
        for op in ("push", "pull"):
            proc = Mock()
            proc.stdin = io.StringIO()
            with patch.object(transfer, "_open_remote_helper_session", return_value=proc) as open_session, \
                 patch.object(transfer, "_readline_with_timeout", return_value='{"ok":false,"error_code":"PROBE"}'), \
                 patch.object(transfer, "_close_helper_proc"), patch.object(transfer, "_run_sftp_batch") as sftp:
                result = xdev_file.run_file_op({"op": op, "host": "desktop", "local": str(local), "remote": "C:/data",
                                                "remote_xdev_home": "C:/scratch"})
                self.assertEqual(result["error_code"], "PROBE")
                ssh, request = open_session.call_args.args
                self.assertEqual((ssh, request["expected_device"], request["xdev_home"]), ("test.peer", "分析机", "C:/scratch"))
                sftp.assert_not_called()

    def test_actual_receiver_checks_machine_before_files_or_transfer_locks(self):
        scratch = self.root / "remote-home"
        scratch.mkdir()
        default = self.root / "remote-account" / "CrossDeviceCollab"
        default.mkdir(parents=True)
        target = self.root / "remote-output"
        config = {"machine_name": "Studio-B", "device": "分析机", "aliases": ["desktop"]}
        (default / "config.json").write_text(json.dumps(config), encoding="utf-8")
        environment = {**os.environ, "COMPUTERNAME": "Studio-B", "LOCALAPPDATA": str(default.parent)}
        # Use the same bootstrap and JSON/UTF-8 stdin as the SSH target, on an isolated scratch root.
        code = "import sys,base64;exec(base64.b64decode(sys.stdin.readline()).decode())"
        bootstrap = remote_fs_helper.get_remote_bootstrap_b64()
        def invoke(op, expected):
            req = {"op": op, "path": str(target), "content": "文件内容", "expected_device": expected, "xdev_home": str(scratch)}
            result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", code], env=environment,
                                    input=bootstrap + "\n" + json.dumps(req) + "\n", text=True, encoding="utf-8",
                                    capture_output=True, check=True, timeout=8)
            return json.loads(result.stdout)
        for operation in ("create", "_push_lock_session", "_pull_source_session"):
            self.assertEqual(invoke(operation, "Wrong-C")["error_code"], "DEVICE_IDENTITY_MISMATCH")
            self.assertFalse(target.exists())
            self.assertFalse((scratch / "state").exists())
        self.assertTrue(invoke("create", "分析机")["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "文件内容")
        self.assertTrue(invoke("stat", "desktop")["ok"])
        (scratch / "config.json").write_text(json.dumps({**config, "machine_name": "Wrong-C"}), encoding="utf-8")
        self.assertEqual(invoke("stat", "分析机")["error_code"], "DEVICE_IDENTITY_MISMATCH")


if __name__ == "__main__":
    unittest.main()
