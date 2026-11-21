import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.adapters import antigravity, mcp_stdio


FIRST = "c8a2e7f3-66ad-430a-a21d-387ba4d02dda"
SECOND = "ab676e4b-b761-41bb-8ba8-aeb9ce29549c"


def native_metadata(chat, **changes):
    record = {"rootConversationId": chat, "parentConversationId": "", "nestingDepth": 0, "projectId": "selected"}
    return {"response": {"conversationMetadata": {"metadata": {**record, **changes}}}}


def notification(chat):
    marker = "wake-xdev-" + hashlib.sha256(f"{chat}\nletter".encode()).hexdigest()[:24]
    return {"op": "notify", "session": chat, "message_id": "letter", "notice_id": marker, "notice": marker}


class AntigravityProjectTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.executable = self.directory / "language_server.exe"

    def handle(self, payload):
        return antigravity._handle(self.directory, self.executable, [], payload, project="selected")

    def test_two_root_chats_need_one_project_and_duplicates_do_not_resend(self):
        def native(exe, method, *args):
            if method == "get-conversation-metadata":
                return {"response": json.dumps(native_metadata(args[0])["response"])}
            return {"response": {"accepted": True}}

        with patch.object(antigravity, "_native", side_effect=native) as calls, patch.object(
                antigravity, "_trace_notice", return_value={"kind": "native_original_session_notice"}):
            for chat in (FIRST, SECOND):
                inspected = self.handle({"op": "inspect", "session": chat})
                self.assertTrue(inspected["ok"])
                self.assertEqual(inspected["model_calls"], 0)
                result = self.handle(notification(chat))
                self.assertEqual(result["status"], "admitted")
                self.assertEqual(result["placement"], "unverified")
                self.assertEqual(self.handle(notification(chat))["status"], "already_admitted")
        written = [item.args[3] for item in calls.call_args_list if item.args[1] == "send-message"]
        self.assertEqual(written, [FIRST, SECOND])

    def test_wrong_project_child_missing_or_mismatched_identity_never_submits(self):
        for result in (native_metadata(FIRST, projectId="other"), native_metadata(SECOND),
                       native_metadata(FIRST, parentConversationId=SECOND, nestingDepth=1),
                       native_metadata(FIRST, nestingDepth=False), {"response": {}},
                       {"response": {"conversationMetadata": {"metadata": {"projectId": "selected"}}}}):
            with self.subTest(result=result), patch.object(antigravity, "_native", return_value=result) as native:
                self.assertFalse(self.handle({"op": "inspect", "session": FIRST})["ok"])
                result = self.handle(notification(FIRST))
                self.assertEqual(result["status"], "out_of_scope")
                self.assertFalse(result["native_write_attempted"])
                self.assertTrue(all(call.args[1] == "get-conversation-metadata" for call in native.call_args_list))
                self.assertFalse((self.directory / "admissions.sqlite3").exists())

    def test_failed_metadata_read_can_retry_and_fixed_mode_still_restricts(self):
        with patch.object(antigravity, "_native", side_effect=TimeoutError()) as native:
            self.assertEqual(self.handle(notification(FIRST))["status"], "requires_activation")
            self.assertFalse((self.directory / "admissions.sqlite3").exists())
            with self.assertRaisesRegex(ValueError, "explicitly bound"):
                antigravity._handle(self.directory, self.executable, [SECOND], notification(FIRST))
            native.assert_called_once_with(self.executable, "get-conversation-metadata", FIRST)

    def test_a_restart_cannot_change_the_project_of_an_existing_grant(self):
        with patch.object(antigravity, "_native", side_effect=[native_metadata(FIRST), native_metadata(FIRST),
                          native_metadata(FIRST, projectId="another"), native_metadata(FIRST)]):
            self.assertEqual(antigravity._bind_project(self.directory, self.executable, FIRST), "selected")
            self.assertEqual(antigravity._bind_project(self.directory, self.executable, FIRST), "selected")
            with self.assertRaisesRegex(ValueError, "changed projects"):
                antigravity._bind_project(self.directory, self.executable, FIRST)
            self.assertEqual(antigravity._bind_project(self.directory, self.executable, FIRST), "selected")

    def test_first_connection_uses_its_install_root_and_keeps_unowned_paths_intact(self):
        state = self.directory / "state"
        state.mkdir()
        bridge = state / "antigravity-native"
        with self.assertRaises(OSError):
            antigravity._prepare_bridge(bridge)
        (self.directory / ".xdev-install.json").write_text('{"app":"CrossDeviceCollab","schema":1}')
        antigravity._prepare_bridge(bridge)
        antigravity._prepare_bridge(bridge)
        (bridge / ".xdev-owned.json").unlink()
        with self.assertRaisesRegex(ValueError, "ownership"):
            antigravity._prepare_bridge(bridge)
        self.assertEqual(list(bridge.iterdir()), [])


class MailToolRegistrationTests(unittest.TestCase):
    def test_only_an_identified_codex_chat_gets_codex_registration_diagnostics(self):
        tools = {}

        def decorator(**options):
            def record(fn):
                tools[options.get("name") or fn.__name__] = fn
                return fn
            return record

        fake = SimpleNamespace(FastMCP=lambda *args, **kwargs: SimpleNamespace(tool=decorator, run=lambda **k: None),
                               Context=object)
        with patch.dict(sys.modules, {"mcp.server.fastmcp": fake}), patch.object(
                mcp_stdio.inprocess_cli, "runtime_metadata", return_value={}), patch.dict(
                mcp_stdio.os.environ, {"XDEV_DSH_SESSION_TOOL": ""}):
            mcp_stdio.run()
        with patch.object(mcp_stdio, "_tool_result", side_effect=lambda x: x), patch.object(
                mcp_stdio, "_dispatch", new=AsyncMock(side_effect=lambda *a: {"ok": True})), patch(
                "src.cli.main._register_caller", return_value={"ok": True, "status": "registered"}) as register:
            for meta in ({}, {"ai.opencode/sessionID": "ses_x"}, {"xdev/session": {"client": "editor", "session": "A"}}):
                result = asyncio.run(tools["xdev_msg"]("whoami", [], SimpleNamespace(request_context=SimpleNamespace(meta=meta))))
                self.assertNotIn("native_receiver", result)
            register.assert_not_called()
            ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": FIRST}))
            self.assertEqual(asyncio.run(tools["xdev_msg"]("whoami", [], ctx))["native_receiver"]["status"], "registered")
            register.assert_called_once()
