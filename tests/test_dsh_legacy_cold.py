"""Reuse a legacy DSH address for a cold original on the same native Host."""

from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.adapters import dsh


class LegacyColdTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "config.json").write_text(json.dumps({"device": "local-pc"}))
        self.session = "session-" + str(uuid4())
        self.target = {"client": "dsh", "session": self.session, "device": "local-pc"}
        self.letter = {"id": "old-letter", "from": "local-pc", "to": "local-pc", "recipient_endpoint": self.target}
        self.enterContext(patch.object(dsh.messages, "handle", return_value={"status": "received", "message": self.letter}))
        self.legacy = {"profile": "listed", "sessions": [self.session], "pid": 33,
                       "directory": self.root / "listed", "features": ["steer", "promote"]}
        self.auto = {"profile": "discovery", "sessions": [], "discovery": "active_original", "pid": 33,
                     "directory": self.root / "new", "features": ["steer", "promote"]}
        self.profiles = [self.legacy, self.auto]
        self.enterContext(patch.object(dsh, "_live_bindings", return_value=self.profiles))
        self.calls = []
        self.enterContext(patch.object(dsh, "_exchange", side_effect=self.exchange))
        self.inactive = {"status": "requires_activation", "active": False, "submitted": False}
        self.ready = {"status": "ready", "active": True, "running": False}
        self.inspect = self.inactive
        self.resolved = self.ready
        self.failure = False

    def exchange(self, binding, frame, call_id):
        self.assertEqual(frame["session"], self.session)
        self.calls.append((binding["profile"], frame["op"], frame["request_id"], call_id))
        if frame["op"] == "inspect":
            return self.inspect
        if frame["op"] == "resolve_original":
            self.inspect = self.resolved
            return self.resolved
        self.assertEqual(binding["profile"], "listed")
        if self.failure:
            raise TimeoutError("unconfirmed")
        return {"accepted": True, "api": "sessionController.prompt"}

    def submit(self):
        return dsh.submit(self.root, self.letter["id"], self.session)

    def test_recovers_and_notifies_via_same_legacy_id_only_once(self):
        self.assertEqual(self.submit()["status"], "admitted")
        self.assertEqual(self.submit()["status"], "already_admitted")
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("listed", "inspect"), ("discovery", "resolve_original"),
                                                              ("listed", "inspect"), ("listed", "notify")])
        self.assertEqual(len({c[2] for c in self.calls}), 1)
        with closing(dsh._admissions(self.root)) as db:
            self.assertEqual(db.execute("select profile, admitted from admissions").fetchall(), [("listed", 1)])

    def test_existing_unverified_admission_reuses_same_call_and_never_reselects(self):
        self.failure = True
        self.assertEqual(self.submit()["status"], "unknown")
        first_notify = self.calls[-1]
        self.failure = False
        self.inspect = self.inactive
        self.calls.clear()
        self.assertEqual(self.submit()["status"], "admitted")
        self.assertEqual(self.calls, [first_notify])

    def test_ambiguous_unknown_or_unconfirmed_static_state_never_triggers_resolution(self):
        for observation in ({"status": "unknown"}, {"status": "requires_activation", "active": False},
                            {"status": "requires_activation", "active": False, "submitted": True}):
            self.calls.clear()
            self.inspect = observation
            self.assertNotEqual(self.submit()["status"], "admitted")
            self.assertEqual([c[1] for c in self.calls], ["inspect"])
        self.inspect = self.inactive
        self.profiles.append({**self.auto, "profile": "other-auto"})
        self.assertEqual(self.submit()["error_code"], "DSH_AUTOMATIC_RECEIVER_AMBIGUOUS")

    def test_requires_exact_address_and_same_host_and_respects_writer_owner(self):
        self.target["session"] = "another-chat"
        self.assertEqual(self.submit()["status"], "requires_activation")
        self.target["session"] = self.session
        self.auto["pid"] += 1
        self.assertEqual(self.submit()["status"], "requires_activation")
        self.assertEqual([c[1] for c in self.calls], ["inspect", "inspect"])
        self.auto["pid"] = self.legacy["pid"]
        self.resolved = {**self.inactive, "error_code": "DSH_ORIGINAL_WRITER_HELD"}
        self.assertEqual(self.submit()["error_code"], "DSH_ORIGINAL_WRITER_HELD")
        self.assertNotIn("notify", [c[1] for c in self.calls])
