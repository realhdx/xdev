"""Scoped receipt registration and native status calls never enroll other chats."""

from compression import zstd
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.adapters import dsh, dsh_host_tool, inprocess_cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import dsh_roots
sys.path.pop(0)


class ScopedReceiverTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sessions = self.base / "sessions"
        self.first, self.second, self.other = ["session-" + str(uuid4()) for _ in range(3)]

    def archive(self, session, **extra):
        path = self.sessions / "project" / session / "session.v4.jsonl.zstd"
        path.parent.mkdir(parents=True)
        header = {"type": "session", "version": 4, "id": session, **extra}
        path.write_bytes(zstd.compress((json.dumps(header) + "\n").encode()))
        return path

    def select(self, ids):
        return dsh_roots.original_sessions(self.sessions, dsh.canonical_session, ids)

    def test_only_named_original_headers_are_opened_and_duplicates_coalesce(self):
        self.archive(self.first)
        self.archive(self.second)
        self.archive(self.other).write_bytes(b"unrelated damaged archive")
        self.assertEqual(self.select([self.first, self.first, self.second[8:]]), {self.first, self.second})

    def test_subagent_or_mismatched_archive_fails_whole_selection(self):
        self.archive(self.first)
        self.archive(self.second, origin="subagent", delegationDepth=1)
        self.archive(self.other, id=self.first)
        for name in (self.second, self.other, "session-" + str(uuid4())):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "No original chat"):
                self.select([self.first, name])

    def test_scoped_update_keeps_old_bindings_and_config_only_changes_once(self):
        install = self.base / "install"
        bridge = self.base / "state/listed-roots"
        config = {"bridgeDirectory": str(bridge), "profile": bridge.name, "aclPython": "python",
                  "sessions": [self.first]}
        marker = "xdev-dsh-receiver-listed"
        template = ('- insert:\r\n    - id: ' + marker + '\r\n      name: ' + json.dumps(str(install / "index.js"))
                    + '\r\n      config: {}\r\n# keep unrelated user text\r\n')
        def plan(data, selected):
            return dsh_roots.planned_patch(data, selected, install, marker, bridge, "python", scoped=True)[0]
        original = template.replace("config: {}", "config: " + json.dumps(config)).encode()
        changed = plan(original, {self.second})
        self.assertEqual(changed, template.replace("config: {}", "config: " + json.dumps({**config, "sessions": sorted([self.first, self.second])})).encode())
        self.assertEqual(plan(changed, {self.second}), changed)
        self.assertEqual(plan(changed, {self.first, self.second}), changed)

    def test_diagnosis_distinguishes_absent_host_from_unlisted_without_leaking_details(self):
        with patch.dict(os.environ, {"XDEV_DSH_BRIDGE": str(self.base / "receiver")}):
            self.assertEqual(dsh.receiver_binding(self.base, self.first)["error_code"], "DSH_RECEIVER_NOT_LOADED")
            with patch.object(dsh, "_live_bindings", return_value=[{"sessions": [self.first], "token": "secret"}]):
                self.assertEqual(dsh.receiver_binding(self.base, self.first), {"bound": True, "session": self.first})
                other = dsh.inspect(self.base, self.second)
                self.assertEqual(other["error_code"], "DSH_SESSION_NOT_BOUND")
                self.assertIn("automatic", other["next_action"])
                self.assertIn("--session " + self.second + " --apply", other["optional_scoped_command"])
                self.assertNotIn(self.first, str(other))
                self.assertNotIn("secret", str(other))

    def test_native_status_dispatch_remains_bound_to_calling_chat(self):
        with patch.object(inprocess_cli, "invoke", return_value={"ok": True}) as invoke:
            result = dsh_host_tool.dispatch({"session": self.first, "action": "status", "arguments": ["original-id"]}, self.base)
        self.assertTrue(result["ok"])
        invoke.assert_called_once_with(["msg", "status", "original-id"], root=self.base)

    def test_live_root_discovery_keeps_explicit_receiver_and_dedup_profile(self):
        old = {"profile": "prior", "sessions": [self.first]}
        automatic = {"profile": "active-roots", "sessions": [], "discovery": "active_original"}
        with patch.object(dsh, "_live_bindings", return_value=[old, automatic]):
            self.assertEqual(dsh._binding(self.base, self.first), old)
            self.assertEqual(dsh._binding(self.base, self.second), automatic)
            self.assertEqual(dsh._binding(self.base, self.first, "active-roots"), automatic)

    def test_automatic_plugin_is_single_and_managed_rollback_keeps_user_edits(self):
        before = b'# user profile\r\n- insert:\r\n    - id: other\r\n      name: other\r\n'
        after = dsh_roots.automatic_receiver(before, self.base, dsh)
        self.assertEqual(dsh_roots.automatic_receiver(after, self.base, dsh), after)
        self.assertNotIn(self.first.encode(), after)
        self.assertIn(b'"sessions": []', after)
        unrelated = b'# later user preference\r\n'
        self.assertEqual(dsh_roots.restore_managed(before, after, after + unrelated), before + unrelated)
        edited = after.replace(b'"discovery": "active_original"', b'"discovery": "custom"')
        with self.assertRaisesRegex(ValueError, 'managed DSH setting'):
            dsh_roots.restore_managed(before, after, edited)

    @unittest.skipUnless(shutil.which("node"), "Node is required to exercise the native JS receiver")
    def test_native_resolver_never_claims_subagent_archived_or_other_writer(self):
        (self.base / "receiver.mjs").write_text(dsh.HOST_PLUGIN, encoding="utf-8")
        probe = self.base / "test.mjs"
        probe.write_text('''import assert from 'node:assert/strict';
import {resolveAddressedOriginal} from './receiver.mjs';
const id='session-00000000-0000-4000-8000-000000000091';
async function proof({header, archived=false, error, live=false}) {
  const active=new Map(); let loads=0, observations=0, disposals=0;
  const agent={session:{header:{id,cwd:'test',...header}}};
  if(live)active.set(id,agent);
  const services={workspaceRegistry:{archivedSessionIds:archived?[id]:[]},
    sessionQuery:{observeSession:async value=>{assert.equal(value,id); observations++;
      return {header:agent.session.header,[Symbol.dispose](){disposals++;}};}}};
  const ctx={agents:active,get:name=>services[name],sessionController:{resolveAgent:async value=>{
    assert.equal(value,id);loads++;if(error)return {error:{code:error}};active.set(id,agent);return {agent};}}};
  const result=await resolveAddressedOriginal(ctx,id);
  assert.equal(observations,disposals);
  return [result?.error_code??null,loads,observations];
}
assert.deepEqual(await proof({live:true}),[null,0,0]);
assert.deepEqual(await proof({}),[null,1,1]);
assert.deepEqual(await proof({header:{origin:'subagent'}}),['DSH_ORIGINAL_UNAVAILABLE',0,1]);
assert.deepEqual(await proof({header:{parentSession:'other'}}),['DSH_ORIGINAL_UNAVAILABLE',0,1]);
assert.deepEqual(await proof({archived:true}),['DSH_ORIGINAL_UNAVAILABLE',0,0]);
assert.deepEqual(await proof({error:'session/writer-held'}),['DSH_ORIGINAL_WRITER_HELD',1,1]);
''', encoding="utf-8")
        subprocess.run([shutil.which("node"), str(probe)], check=True, timeout=10, capture_output=True)
