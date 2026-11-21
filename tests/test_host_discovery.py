"""End-to-end discovery of a declared catalog and two native, unmodified MCP tool schemas."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.adapters import host_catalog, inprocess_cli, sessions
from src.runtime import messages


class Discover(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "calls.jsonl"
        self.client = "previously-unknown"
        self.sid = "group/42"

    def configure(self, catalogs, **extra):
        (self.root / "config.json").write_text(
            json.dumps({"device": "local-workPC", "session_host_catalogs": catalogs, **extra}), encoding="utf8"
        )

    def mcp(self, kind, reply="ok"):
        alpha = kind == "alpha"
        return {
            "type": "mcp-stdio",
            "argv": [
                sys.executable,
                str(Path(__file__).with_name("host_catalog_fixture.py").resolve()),
                kind,
                str(self.log),
                reply,
            ],
            "client": self.client,
            "server_name": "Alpha Desk" if alpha else "Beta Desk",
            "placements": ["current_turn"] if alpha else ["next_turn"],
            "roles": {
                "resolve": {
                    "tool": "lookup" if alpha else "describe_room",
                    "inputs": {"session": "chat" if alpha else "room"},
                    "outputs": {
                        "session": "chat" if alpha else "roomId",
                        "original": "original" if alpha else "valid",
                        "instance": "boot" if alpha else "process",
                    },
                },
                "submit": {
                    "tool": "interrupt" if alpha else "enqueue_to_room",
                    "inputs": {
                        "session": "chat" if alpha else "dest",
                        "notice_id": "correlation" if alpha else "key",
                        "text": "payload" if alpha else "prompt",
                    },
                    "outputs": {
                        "session": "chat" if alpha else "target",
                        "notice_id": "correlation" if alpha else "key",
                        "accepted": "received" if alpha else "placed",
                        "placement": "where" if alpha else "scope",
                    },
                },
            },
        }

    def deliver(self, mid, queue=False):
        ep = {"device": "local-workPC", "client": self.client, "session": self.sid}
        messages.handle(
            self.root,
            "deliver",
            {
                "from": "other",
                "to": ep["device"],
                "subject": "quiet",
                "text": "private content stays in the inbox",
                "id": mid,
                "recipient_identity": ep,
                "recipient_endpoint": ep,
            },
        )
        return sessions.submit(self.root, mid, self.sid, self.client, force_queue=queue)

    def commands(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP package required")
    def test_mcp_native_different_host_names_fields_and_placements(self):
        for kind, queued in (("alpha", False), ("beta", True)):
            with self.subTest(kind=kind):
                self.configure({"authorized": self.mcp(kind)})
                overview = inprocess_cli.invoke(["ai", "--discover", "--client", self.client], root=self.root)
                self.assertTrue(overview["providers"][0]["clients"][self.client]["supported"], overview)
                self.assertEqual(self.commands(), [])
                matched = sessions.discover(self.root, self.client, self.sid)
                self.assertEqual(matched["status"], "resolved", matched)
                before = len([1 for op, _ in self.commands() if op == "send"])
                if queued:
                    self.assertEqual(self.deliver(kind + "-immediate")["error_code"], "HOST_PLACEMENT_UNAVAILABLE")
                receipt = self.deliver(kind, queued)
                self.assertEqual(receipt["status"], "queued" if queued else "admitted", receipt)
                self.assertEqual(receipt["host_placement"], "next_turn" if queued else "current_turn")
                self.assertEqual(
                    self.deliver(kind, queued)["status"], "already_queued" if queued else "already_admitted"
                )
                writes = [args for op, args in self.commands() if op == "send"]
                self.assertEqual(len(writes), before + 1)
                self.assertNotIn("private content", str(writes))
                self.log.unlink()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP package required")
    def test_mcp_unknown_effect_readonly_wrong_session_and_ambiguous_ack(self):
        native = self.mcp("beta", "read-only")
        native.pop("roles")
        self.configure({"native": native})
        result = sessions.discover(self.root, self.client, self.sid)
        self.assertEqual(result["providers"][0]["clients"][self.client]["reason"], "read_only_catalog")
        self.assertEqual(self.commands(), [])
        native = self.mcp("alpha", "wrong")
        self.configure({"native": native})
        self.assertEqual(sessions.discover(self.root, self.client, "missing")["status"], "unresolved")
        receipt = self.deliver("wrong")
        self.assertEqual(receipt["status"], "unknown", receipt)
        self.assertEqual(self.deliver("wrong")["status"], "unknown")
        self.assertEqual(sum(op == "send" for op, _ in self.commands()), 1)
        native["roles"]["resolve"]["tool"] = "interrupt"
        self.configure({"native": native})
        result = sessions.discover(self.root, self.client, self.sid)
        self.assertEqual(result["providers"][0]["clients"][self.client]["reason"], "tool_effect_mismatch")
        self.assertEqual(sum(op == "send" for op, _ in self.commands()), 1)

    def test_opt_in_catalog_real_stdio_and_read_only_ambiguity(self):
        script = self.root / "provider.py"
        script.write_text(
            """import json,sys
from pathlib import Path
f=json.load(sys.stdin);resp={k:f[k] for k in ('protocol','request_id','provider','op','instance','session','client') if k in f}
op=f['op'];r={'status':'ok','instance':'same-local-host','clients':[{'client':'previously-unknown','template':'xdev.receiver.v1','placements':['current_turn']},{'client':'not-mail','template':'new-thing'}]} if op=='catalog' else {'status':'ready','original':True}
if op=='submit':
 n=f['notice'];r={'ack':{**{k:n[k] for k in ('protocol','message_id','notice_id','session','client')},'status':'accepted','placement':'current_turn'}}
with Path(sys.argv[1]).open('a') as log: log.write(json.dumps([op, str(f)])+'\\n')
print(json.dumps({**resp,**r}))
""",
            encoding="utf8",
        )
        config = {"argv": [sys.executable, str(script), str(self.log)]}
        self.configure({"first": config, "second": config})
        public = sessions.discover(self.root, self.client, self.sid)
        self.assertEqual(public["status"], "ambiguous")
        self.assertNotIn("submit", [op for op, _ in self.commands()])
        self.assertEqual(self.deliver("ambiguous")["discovery_status"], "ambiguous")
        self.configure({"first": config})
        self.assertTrue(sessions.receiver_bound(self.root, self.client, self.sid))
        self.assertEqual(self.deliver("fresh")["status"], "admitted")
        self.assertEqual(self.deliver("fresh")["status"], "already_admitted")
        self.assertEqual(sum(op == "submit" for op, _ in self.commands()), 1)
        self.assertNotIn("private content", repr(self.commands()))
        self.assertEqual(sessions.discover(self.root, "not-mail", "Q")["status"], "unresolved")

    def test_known_builtin_and_configured_exact_stay_authoritative(self):
        self.configure({}, session_receivers={self.client: {self.sid: {"argv": [sys.executable]}}})
        with patch.object(
            host_catalog, "discover", side_effect=AssertionError("other providers should not be probed")
        ):
            self.assertEqual(sessions.discover(self.root, "codex")["status"], "native_adapter_declared")
            self.assertEqual(sessions.discover(self.root, self.client, self.sid)["status"], "configured_binding")
            self.assertTrue(sessions.receiver_bound(self.root, self.client, self.sid))

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP package required")
    def test_opt_in_provider_metadata_without_local_tool_or_parameter_names(self):
        native = self.mcp("beta")
        native.pop("roles")
        native.pop("placements")
        self.configure({"beta": native})
        ignored = sessions.discover(self.root, self.client, self.sid)
        self.assertEqual(ignored["providers"][0]["clients"][self.client]["reason"], "no_semantic_mapping")
        self.assertEqual(self.commands(), [])
        self.configure({"beta": {**native, "accept_declared_roles": True}})
        self.assertEqual(self.deliver("declared", True)["status"], "queued")
        self.assertEqual(sum(op == "send" for op, _ in self.commands()), 1)

    def test_resident_unavailable_only_falls_back_before_commit(self):
        from src.adapters import portable, resident

        self.configure({}, session_hosts={self.client: {"resident": {}, "argv": [sys.executable]}})

        def accepted(argv, n, root):
            return {
                **{k: n[k] for k in ("protocol", "client", "session", "message_id", "notice_id")},
                "status": "accepted",
                "placement": "current_turn",
            }

        with patch.object(resident, "validate"), patch.object(portable, "_exchange", side_effect=accepted) as other:
            with patch.object(resident, "exchange", return_value={"status": "unavailable_before_submit"}):
                self.assertEqual(self.deliver("once")["status"], "admitted")
                self.assertEqual(self.deliver("once")["status"], "already_admitted")
            with patch.object(resident, "exchange", side_effect=ValueError("failed peer validation")):
                self.assertEqual(self.deliver("suspect")["status"], "unknown")
                self.assertTrue(self.deliver("suspect")["duplicate"])
            self.assertEqual(other.call_count, 1)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP package required")
    def test_reject_external_schema_and_respect_native_argument_constraints(self):
        from types import SimpleNamespace

        from src.adapters import mcp_catalog

        schema = {
            "type": "object",
            "properties": {"room": {"type": "string", "enum": ["permitted"]}},
            "required": ["room"],
        }
        tool = SimpleNamespace(inputSchema=schema, outputSchema=None, annotations=SimpleNamespace(readOnlyHint=True))
        role = {"inputs": {"session": "room"}}
        self.assertIsNone(mcp_catalog._checked(role, tool, "resolve"))
        self.assertTrue(mcp_catalog._accepts(schema, {"room": "permitted"}))
        self.assertFalse(mcp_catalog._accepts(schema, {"room": "other"}))
        tool.outputSchema = {"type": "object", "properties": {"external": {"$ref": "https://example.invalid/schema"}}}
        self.assertEqual(mcp_catalog._checked(role, tool, "resolve"), "external_schema_reference")
