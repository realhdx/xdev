"""Isolated registration/undo checks; no real client or MCP server is started."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import clients


class ClientRegistration(unittest.TestCase):
    def test_antigravity_zero_byte_profile_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.py").touch()
            (root / "state").mkdir()
            path = root / "mcp_config.json"
            path.touch()
            plan = clients.inspect("antigravity", path, "json", root)
            self.assertEqual(plan["server_names"], [])
            self.assertEqual(plan["sha256"], clients.sha(b""))
            receipt = clients.apply("antigravity", path, "json", root, plan["sha256"])
            self.assertEqual(list(json.loads(path.read_bytes())["mcpServers"]), ["xdev"])
            repeat = clients.apply("antigravity", path, "json", root, receipt["after_sha256"])
            self.assertFalse(repeat["changed"])
            clients.restore(Path(receipt["receipt_path"]))
            self.assertEqual(path.read_bytes(), b"")
            for invalid in (b" ", b"{", b"// user settings"):
                path.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    clients.apply("antigravity", path, "json", root, clients.sha(invalid))
                self.assertEqual(path.read_bytes(), invalid)

    @unittest.skipUnless(os.name == "nt", "Windows file sharing")
    def test_short_windows_lock_retries_only_while_original_still_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "mcp.json"
            original, new, edited = b"original", b"new", b"user edit"
            for edit_during_wait in (False, True):
                with self.subTest(edit_during_wait=edit_during_wait):
                    target.write_bytes(original)
                    busy = OSError("busy")
                    busy.winerror = 1175
                    calls = []

                    def replace(path, staged):
                        calls.append(path)
                        if len(calls) == 1:
                            raise busy
                        os.replace(staged, path)

                    def release(_delay):
                        if edit_during_wait:
                            target.write_bytes(edited)

                    with patch.object(clients, "replace_windows", replace), patch.object(clients.time, "sleep", release):
                        if edit_during_wait:
                            with self.assertRaisesRegex(ValueError, "changed while staging"):
                                clients.replace_cas(target, new, clients.sha(original))
                            self.assertEqual(target.read_bytes(), edited)
                            self.assertEqual(len(calls), 1)
                        else:
                            clients.replace_cas(target, new, clients.sha(original))
                            self.assertEqual(target.read_bytes(), new)
                            self.assertEqual(len(calls), 2)

    def test_new_client_files_have_reversible_schema_specific_entries(self):
        products = {"copilot": "mcpServers", "codebuddy": "mcpServers", "gemini": "mcpServers",
                    "kimi-code": "mcpServers", "cursor": "mcpServers", "vscode": "servers"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.py").touch()
            (root / "state").mkdir()
            for product, section in products.items():
                with self.subTest(product=product):
                    path = root / (product + ".json")
                    original = {"other": {"setting": 42}, section: {"existing": {"command": "keep"}}}
                    before = json.dumps(original).encode()
                    path.write_bytes(before)
                    kind = clients.PRODUCTS[product][1]
                    receipt = clients.apply(product, path, kind, root, clients.sha(before))
                    actual = json.loads(path.read_bytes())
                    entry = actual[section].pop("xdev")
                    self.assertEqual(actual, original)
                    self.assertEqual(entry.get("type"), None if product in {"gemini", "kimi-code"} else "stdio")
                    self.assertEqual("tools" in entry, product == "copilot")
                    if product == "copilot":
                        self.assertEqual(len(entry["tools"]), 6)
                    current = clients.inspect(product, path, kind, root)
                    self.assertTrue(current["xdev_exact"])
                    self.assertFalse(clients.apply(product, path, kind, root, current["sha256"])["changed"])
                    clients.restore(Path(receipt["receipt_path"]))
                    self.assertEqual(path.read_bytes(), before)

    def test_missing_file_missing_section_and_jsonc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.py").touch()
            (root / "state").mkdir()
            path = root / "settings.json"
            with self.assertRaisesRegex(ValueError, "existing physical"):
                clients.inspect("gemini", path, "json", root)
            self.assertFalse(path.exists())
            path.write_bytes(b'{"other":true}')
            r = clients.apply("gemini", path, "json", root, clients.sha(path.read_bytes()))
            self.assertIn("xdev", json.loads(path.read_bytes())["mcpServers"])
            clients.restore(Path(r["receipt_path"]))
            self.assertEqual(path.read_bytes(), b'{"other":true}')
            text = b'{/*preserve this*/ "servers":{},}'
            path.write_bytes(text)
            with self.assertRaisesRegex(ValueError, "JSONC"):
                clients.apply("vscode", path, "json-servers", root, clients.sha(text))
            self.assertEqual(path.read_bytes(), text)

    def test_copilot_catalog_tracks_active_release_and_preserves_custom_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.py").touch()
            (root / "state").mkdir()
            digest = "a" * 64
            (root / "current.txt").write_text(digest + "\n", encoding="ascii")
            identity = root / "releases" / digest / "src/product.json"
            identity.parent.mkdir(parents=True)
            identity.write_text(json.dumps({"product": "xdev", "version": "1.0.3"}))
            config = root / "copilot.json"
            original = clients.expected_entry(root, "copilot")
            self.assertEqual(original["tools"], list(clients.BASE_TOOLS))
            (root / "current.txt").write_text(digest + "\n" + "b" * 64 + "\n", encoding="ascii")
            self.assertEqual(clients.installed_tools(root), clients.BASE_TOOLS)
            data = json.dumps({"mcpServers": {"xdev": original, "keep": {"command": "keep"}}}).encode()
            config.write_bytes(data)
            identity.write_text(json.dumps({"product": "xdev", "version": "1.1.0-preview.1"}))
            report = clients.inspect("copilot", config, "json", root)
            self.assertFalse(report["xdev_exact"])
            self.assertEqual(report["server_names"], ["xdev", "keep"])
            receipt = clients.apply("copilot", config, "json", root, report["sha256"])
            actual = json.loads(config.read_bytes())["mcpServers"]
            self.assertEqual(actual["xdev"]["tools"], [*original["tools"], "xdev_surface"])
            self.assertEqual(actual["keep"], {"command": "keep"})
            clients.restore(Path(receipt["receipt_path"]))
            self.assertEqual(config.read_bytes(), data)
            original["tools"] = ["xdev_who"]
            config.write_text(json.dumps({"mcpServers": {"xdev": original}}))
            with self.assertRaisesRegex(ValueError, "unlike"):
                clients.inspect("copilot", config, "json", root)

    def test_config_roots_and_codebuddy_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            custom = home / "custom"
            env = {key: str(custom) for key in ("COPILOT_HOME", "KIMI_CODE_HOME", "APPDATA")}
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, env):
                self.assertEqual(clients.client_config("copilot")[0], custom / "mcp-config.json")
                self.assertEqual(clients.client_config("kimi-code")[0], custom / "mcp.json")
                self.assertEqual(clients.client_config("vscode")[0], custom / "Code/User/mcp.json")
                directory = home / ".codebuddy"
                directory.mkdir()
                for path in (home / ".codebuddy.json", directory / "mcp.json", directory / ".mcp.json"):
                    path.write_text("{}")
                    self.assertEqual(clients.client_config("codebuddy")[0], path)

    def test_json_roundtrip_restores_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.py").write_text("# synthetic\n")
            (root / "state").mkdir()
            path = root / "mcp.json"
            original = b'\xef\xbb\xbf{\r\n  "mcpServers": {"browse": {"env": {"FAKE": "sentinel"}}},\r\n  "other": [1, false, "unchanged"]\r\n}\r\n'
            path.write_bytes(original)
            receipt = clients.apply("synthetic", path, "json", root, clients.sha(original))
            self.assertEqual(Path(receipt["backup_path"]).read_bytes(), original)
            self.assertTrue(clients.inspect("synthetic", path, "json", root)["xdev_exact"])
            self.assertFalse(clients.apply("synthetic", path, "json", root,
                                          clients.sha(path.read_bytes()))["changed"])
            result = clients.restore(Path(receipt["receipt_path"]))
            self.assertTrue(result["restored"])
            self.assertEqual(path.read_bytes(), original)

    def test_restore_does_not_overwrite_later_client_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.py").write_text("# synthetic\n")
            (root / "state").mkdir()
            path = root / "mcp.json"
            original = b'{"mcpServers": {}}'
            path.write_bytes(original)
            receipt = clients.apply("synthetic", path, "json", root, clients.sha(original))
            edited = path.read_bytes() + b"\n"
            path.write_bytes(edited)
            with self.assertRaisesRegex(ValueError, "compare-and-swap"):
                clients.restore(Path(receipt["receipt_path"]))
            self.assertEqual(path.read_bytes(), edited)

    def test_stale_apply_fails_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "mcp.json"
            path.write_bytes(b'{"mcpServers": {}}')
            with self.assertRaisesRegex(ValueError, "reviewed SHA"):
                clients.apply("synthetic", path, "json", root, "0" * 64)
            self.assertEqual(list(root.iterdir()), [path])

    def test_duplicate_or_conflicting_json_rejected(self):
        entry = {"command": "native-python", "args": ["mcp"]}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            clients.transform(b'{"mcpServers":{},"mcpServers":{}}', "json", entry)
        with self.assertRaisesRegex(ValueError, "unlike"):
            clients.transform(b'{"mcpServers":{"xdev":{"command":"other"}}}', "json", entry)

    def test_dsh_append_keeps_every_original_byte(self):
        original = b"# existing configuration\r\n- insert:\r\n    - id: browse\r\n      name: '@deepseek-ai/dsh-mcp-client'\r\n      config:\r\n        serverName: browse\r\n        transport: stdio\r\n        command: old\r\n        args: [mcp]\r\n"
        entry = {"command": "C:\\Python314\\python.exe", "args": ["-I", "mcp"]}
        updated, names, changed = clients.transform(original, "yaml-patch", entry)
        self.assertTrue(changed)
        self.assertTrue(updated.startswith(original))
        self.assertEqual(names, ["browse", "xdev"])
        self.assertEqual(clients.transform(updated, "yaml-patch", entry),
                         (updated, ["browse", "xdev"], False))
        with self.assertRaisesRegex(ValueError, "unlike"):
            clients.transform(updated, "yaml-patch", {**entry, "command": "different"})


if __name__ == "__main__":
    unittest.main()
