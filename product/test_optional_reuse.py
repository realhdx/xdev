"""Upgrade an existing Python site without adopting the adjacent CodeBuddy CLI."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import install


OPTIONAL = install.load_at(Path(__file__).resolve().parents[1] / "deploy/optional.py")


class OptionalDependencyReuseTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="xdev-deps-reuse-")))
        self.root = self.work / "station"
        for directory in ("releases", "state", "deps/mcp"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / OPTIONAL.MARKER).write_text('{"app":"CrossDeviceCollab","schema":1}')
        self.archive = self.work / "bundle.zip"
        self.archive.write_bytes(b"fixture")
        self.checksum = OPTIONAL.digest(self.archive)
        self.site = self.root / "deps"
        self.module = self.site / "mcp/__init__.py"
        self.module.write_bytes(b"hello")
        self.record = {"schema": 1, "archive_sha256": self.checksum, "lock_sha256": OPTIONAL.PINNED[OPTIONAL.LOCK],
                       "packages": OPTIONAL.PACKAGES, "files": OPTIONAL.inventory(self.site)}
        self.receipt = self.site / OPTIONAL.RECEIPT
        self.receipt.write_text(json.dumps(self.record))
        self.node = self.site / "codebuddy-cli"
        self.node.mkdir()
        self.canary = self.node / "node_modules/codebuddy/example.js"
        self.canary.parent.mkdir(parents=True)
        self.canary.write_bytes(b"independent Node CLI")
        self.imports = self.enterContext(patch.object(OPTIONAL, "verify_imports"))

    def verify(self):
        return OPTIONAL.verify_existing(self.site, self.checksum)

    def test_two_reuses_preserve_node_files_and_never_hash_or_execute_them(self):
        receipt = self.receipt.read_bytes()
        with patch.object(OPTIONAL, "digest", wraps=OPTIONAL.digest) as digests:
            self.assertEqual(self.verify(), ["codebuddy-cli"])
            self.assertEqual(self.verify(), ["codebuddy-cli"])
        self.assertEqual([call.args for call in digests.call_args_list], [(self.module,), (self.module,)])
        self.assertEqual(self.imports.call_count, 2)
        self.assertEqual(self.canary.read_bytes(), b"independent Node CLI")
        self.assertEqual(self.receipt.read_bytes(), receipt)

    @unittest.skipUnless(os.name == "nt", "Windows deployment lock")
    def test_normal_install_reuses_instead_of_unpacking_or_rewriting(self):
        with patch.object(OPTIONAL, "extract", side_effect=AssertionError("must reuse")):
            for _ in range(2):
                result = OPTIONAL.install(self.root, self.archive, self.checksum)
                self.assertTrue(result["reused"])
                self.assertEqual(result["preserved_external_directories"], ["codebuddy-cli"])

    def test_new_top_level_names_and_nested_lookalikes_are_refused(self):
        for name in ("evil.pth", "codebuddy-cli.pth", "codebuddy-cli-extra", "mcp/codebuddy-cli", "other"):
            with self.subTest(name=name):
                path = self.site / name
                if name.endswith(".pth"):
                    path.touch()
                else:
                    path.mkdir()
                try:
                    with self.assertRaisesRegex(ValueError, "Unrecorded"):
                        self.verify()
                finally:
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
        self.imports.assert_not_called()

    def test_modified_removed_or_redirected_managed_file_does_not_import(self):
        self.module.write_text("different")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.verify()
        self.module.unlink()
        with self.assertRaisesRegex(ValueError, "changed|Unrecorded"):
            self.verify()
        os.link(self.canary, self.module)
        with self.assertRaisesRegex(ValueError, "Hard-linked"):
            self.verify()
        self.imports.assert_not_called()

    def test_record_cannot_add_a_companion_path_traversal_or_duplicate_spelling(self):
        managed = dict(self.record["files"])
        for name in ("codebuddy-cli/node_modules/mcp.py", "CodeBuddy-CLI/item", "../external", "/absolute",
                     "mcp/../external", "mcp//other", "mcp\\shadow", "mcp/name:side-stream"):
            with self.subTest(name=name):
                self.record["files"] = {**managed, name: "0" * 64}
                self.receipt.write_text(json.dumps(self.record))
                with self.assertRaisesRegex(ValueError, "Invalid managed dependency"):
                    self.verify()
        self.imports.assert_not_called()

    def test_links_including_nested_junctions_are_rejected_before_import(self):
        outside = self.work / "other-project"
        outside.mkdir()
        for location in (self.node / "node_modules" / "linked", self.site / "mcp" / "linked"):
            with self.subTest(location=location.name):
                if os.name == "nt":
                    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(location), str(outside)],
                                   check=True, capture_output=True)
                else:
                    location.symlink_to(outside, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(ValueError, "Linked"):
                        self.verify()
                finally:
                    if os.name == "nt":
                        location.rmdir()
                    else:
                        location.unlink()
        self.assertTrue(OPTIONAL.linked(SimpleNamespace(st_mode=stat.S_IFDIR,
                                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)))
        self.imports.assert_not_called()
