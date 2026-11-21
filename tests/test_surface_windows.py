"""Exercise UI Automation against an isolated, non-activating window owned by this test."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import time
import unittest
from uuid import uuid4

from src.adapters import surface_windows


class InputTests(unittest.TestCase):
    def test_invalid_target_or_ref_is_rejected_without_opening_a_window(self):
        self.assertEqual(surface_windows.run("snapshot", target="active")["error_code"], "INVALID_TARGET")
        self.assertEqual(surface_windows.run("invoke", target="win:1:2:3:4", ref="0")["error_code"], "INVALID_REF")


@unittest.skipUnless(os.name == "nt", "Windows desktop required")
class FixtureTests(unittest.TestCase):
    def test_snapshot_edit_invoke_and_stale_references(self):
        # Test the same ordinary local-file layout as the installed application.
        # A development checkout can itself be in a Windows Internet-zone folder.
        staging = Path(self.enterContext(tempfile.TemporaryDirectory()))
        module = Path(surface_windows.__file__)
        for path in (module, module.with_suffix(".ps1")):
            shutil.copyfile(path, staging / path.name)
        spec = importlib.util.spec_from_file_location("fixture_windows_backend", staging / module.name)
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        preflight = backend.run("targets", app="xdev-no-such-fixture-app")
        if preflight.get("error_code") in {"NO_INTERACTIVE_DESKTOP", "UI_AUTOMATION_UNAVAILABLE"}:
            self.skipTest(preflight["error_code"])
        self.assertTrue(preflight["ok"], preflight)
        title = "xdev UI test " + uuid4().hex[:12]
        fixture = r"""
        $ErrorActionPreference='Stop'
        Add-Type -AssemblyName PresentationFramework
        $window=[Windows.Window]::new()
        $window.Title='TITLE'
        $window.Width=225; $window.Height=140; $window.ShowInTaskbar=$false
        $window.ShowActivated=$false; $window.Left=-10000; $window.Top=-10000
        $panel=[Windows.Controls.StackPanel]::new(); $window.Content=$panel
        $edit=[Windows.Controls.TextBox]::new(); $edit.Focusable=$false
        [Windows.Automation.AutomationProperties]::SetName($edit,'Draft field')
        $hide=[Windows.Controls.PasswordBox]::new(); $hide.Password='private-placeholder'
        [Windows.Automation.AutomationProperties]::SetName($hide,'Hidden fixture field')
        $button=[Windows.Controls.Button]::new(); $button.Content='Apply draft'
        $label=[Windows.Controls.TextBlock]::new(); $label.Text='Ready'
        $document=[Windows.Controls.RichTextBox]::new(); $document.IsReadOnly=$true
        $document.AppendText('First public paragraph in the read-only rich text fixture.')
        $composite=[Windows.Controls.RichTextBox]::new(); $composite.IsReadOnly=$true
        [Windows.Automation.AutomationProperties]::SetName($composite,'Composite document')
        $innerField=[Windows.Controls.PasswordBox]::new(); $innerField.Password='embedded-placeholder'
        $paragraph=[Windows.Documents.Paragraph]::new()
        $paragraph.Inlines.Add([Windows.Documents.Run]::new('Composite content'))
        $paragraph.Inlines.Add([Windows.Documents.InlineUIContainer]::new($innerField))
        $composite.Document.Blocks.Add($paragraph)
        $draftDoc=[Windows.Controls.RichTextBox]::new()
        [Windows.Automation.AutomationProperties]::SetName($draftDoc,'Editable document')
        $draftDoc.AppendText('unsubmitted-placeholder')
        $button.Add_Click({$label.Text='Applied '+$edit.Text; $button.Content='Already applied'})
        foreach($control in @($edit,$hide,$button,$label,$document,$composite,$draftDoc)){[void]$panel.Children.Add($control)}
        # A modal WPF dialog can receive desktop input despite ShowActivated=false.
        $window.Show()
        [Windows.Threading.Dispatcher]::Run()
        """.replace("TITLE", title)
        command = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        encoded = base64.b64encode(fixture.encode("utf-16-le")).decode("ascii")
        child = subprocess.Popen(
            [str(command), "-NoProfile", "-STA", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.terminate)
        deadline = time.monotonic() + 24
        item = None
        while item is None and time.monotonic() < deadline:
            listing = backend.run("targets", app="powershell.exe")
            if listing.get("error_code") == "NO_INTERACTIVE_DESKTOP":
                self.skipTest("No access to the interactive input desktop")
            self.assertTrue(listing["ok"], listing)
            item = next((row for row in listing["targets"] if row["title"] == title), None)
            if not item:
                self.assertIsNone(child.poll(), "The fixture closed before its window was visible")
        self.assertIsNotNone(item, "Fixture window was not found")
        target = item["target"]
        snapshot = backend.run("snapshot", target=target)
        self.assertTrue(snapshot["ok"], snapshot)
        self.assertFalse(snapshot["truncated"])
        public = json.dumps(snapshot)
        self.assertNotIn("Hidden fixture field", public)
        self.assertNotIn("private-placeholder", public)
        self.assertNotIn("embedded-placeholder", public)
        self.assertNotIn("unsubmitted-placeholder", public)
        documents = [node for node in snapshot["nodes"] if node["role"] == "Document"]
        self.assertTrue(any("First public paragraph" in node.get("text", "") for node in documents), documents)
        for name in ("Composite document", "Editable document"):
            self.assertNotIn("text", next(node for node in documents if node["name"] == name))
        controls = {(node["role"], node["name"]): node for node in snapshot["nodes"]}
        edit, button = controls["Edit", "Draft field"], controls["Button", "Apply draft"]
        self.assertIn("set-value", edit["actions"])
        self.assertIn("invoke", button["actions"])
        written = backend.run("set-value", target=target, ref=edit["ref"], value="semantic check")
        self.assertTrue(written["ok"], written)
        invoked = backend.run("invoke", target=target, ref=button["ref"])
        self.assertTrue(invoked["ok"], invoked)
        after = backend.run("snapshot", target=target)
        self.assertIn("Applied semantic check", {node["name"] for node in after["nodes"]})
        stale = backend.run("invoke", target=target, ref=button["ref"])
        self.assertEqual(stale.get("error_code"), "STALE_REF", stale)
        parts = target.split(":")
        parts[3] = str(int(parts[3]) + 1)
        self.assertEqual(backend.run("snapshot", target=":".join(parts))["error_code"], "STALE_TARGET")
