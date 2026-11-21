"""Check target scope, native status handling, and browser value privacy."""

import json
import unittest
from unittest.mock import patch

from src.adapters import surface_browser as browser


class BrowserSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.call = self.enterContext(patch.object(browser, "_call"))
        self.rows = json.dumps({"tabs": [
            {"tab": 7, "url": "https://example.org/a?q=private", "title": "https://example.org/a?q=private", "lent": True},
            {"tab": 8, "url": "https://example.org/b", "group": "xdev"},
            {"tab": 9, "title": "Unrelated", "group": "other-agent"},
            {"tab": 10, "url": "https://example.org/z?secret=private", "title": "example.org/z?secret=private", "lent": True},
        ]})

    def test_webmcp_catalog_only_lists_tools_on_authorized_tabs(self):
        catalog = {"available": True, "tools": [{"name": "describe_chat",
                   "inputSchema": {"properties": {"chat": {"type": "string"}}}, "description": "untrusted text"}]}
        self.call.side_effect = [self.rows, json.dumps(catalog)]
        result = browser.run("tools", target="10")
        self.assertEqual(result["tools"], [{"name": "describe_chat", "input_fields": ["chat"]}])
        self.assertEqual(self.call.call_args.args, ("webmcp", {"tab": 10, "action": "list", "timeout": 5000}))
        self.call.side_effect = [self.rows]
        self.assertEqual(browser.run("tools", target="9")["status"], "tab_not_available")

    def test_lists_only_lent_and_xdev_tabs_without_url_parameters(self):
        self.call.return_value = self.rows
        result = browser.run("targets")
        self.assertEqual([row["target"] for row in result["targets"]], ["7", "8", "10"])
        self.assertNotIn("private", str(result))
        self.assertFalse(browser.run("close", "9")["ok"])
        self.assertEqual([c.args[0] for c in self.call.call_args_list], ["tabs", "tabs"])

    def test_snapshot_redacts_url_lines_and_bounds_output(self):
        self.call.side_effect = [self.rows, '- link "x":\n  - /url: https://example.org/?ticket=private\n' + "x"*24500]
        result = browser.run("snapshot", 7)
        self.assertTrue(result["truncated"])
        self.assertNotIn("private", result["snapshot"])
        self.assertEqual(self.call.call_args.args[1]["mode"], "aria")

    def test_tab_id_collision_with_another_agent_is_rejected_before_input(self):
        self.call.return_value = json.dumps({"tabs": [
            {"tab": 17, "lent": True, "browser": "first"},
            {"tab": 17, "group": "other", "browser": "second"},
        ]})
        self.assertEqual(browser.run("targets")["targets"], [])
        self.assertEqual(browser.run("invoke", 17, "a4")["status"], "ambiguous_tab_id")
        self.assertTrue(all(c.args[0] == "tabs" for c in self.call.call_args_list))

    def test_explicit_return_and_stale_ref(self):
        self.call.side_effect = [self.rows, '{"closed":[7]}', self.rows,
                                 browser.BrowseError("stale_ref"), self.rows]
        self.assertTrue(browser.run("close", "7")["ok"])
        self.assertEqual(browser.run("invoke", "7", "f2a5")["status"], "stale_ref")
        self.assertEqual(browser.run("invoke", "7", "body")["status"], "invalid_ref")

    def test_open_preserves_tab_after_incomplete_or_http_error(self):
        self.call.side_effect = ['{"tab":32}', '{"tab":32,"pending":"https://example.org/"}',
                                 '{"tab":33}', '{"tab":33,"status":500}']
        self.assertEqual(browser.run("open", value="https://example.org/")["status"], "opened_pending")
        self.assertEqual(browser.run("open", value="https://example.org/")["target"], "33")
        self.assertFalse(browser.run("open", value="https://some:secret@example.org/")["ok"])
        self.assertFalse(browser.run("open", value="file:///local.txt")["ok"])

    def test_open_reports_exact_new_target_even_if_navigation_is_unconfirmed(self):
        self.call.side_effect = ['{"tab":32}', browser.BrowseError("browse_timeout", True)]
        result = browser.run("open", value="https://example.org/")
        self.assertEqual((result["target"], result["outcome"]), ("32", "unknown"))

    def test_native_mutation_requires_confirmation(self):
        self.call.side_effect = [self.rows, '{"warning":"uncertain"}', self.rows, "empty"]
        self.assertEqual(browser.run("invoke", 7, "a7").get("outcome"), "unknown")
        self.assertEqual(browser.run("close", 7).get("outcome"), "unknown")

    def test_fill_uses_only_fixed_program_and_returns_no_value(self):
        value = "\"}); return 'filled'; // \n 中文"
        self.call.side_effect = [self.rows, "tab: 7\n\nfilled", self.rows, "tab: 7\n\nsensitive_field"]
        result = browser.run("set-value", 7, "a2", value)
        self.assertTrue(result["ok"])
        self.assertNotIn(value, str(result))
        call = self.call.call_args
        self.assertEqual(call.args, ("execute", {"tab": 7}))
        self.assertIn(json.dumps(value).encode(), call.kwargs["script"])
        self.assertEqual(browser.run("set-value", 7, "a3", value)["status"], "sensitive_field")


if __name__ == "__main__":
    unittest.main()
