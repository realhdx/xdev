"""Addressed read evidence shared by local, paired SSH and private relay queries."""

import json
from pathlib import Path
import runpy
import tempfile
import unittest

from src.runtime import mail_control, mail_lifecycle, messages


class AddressedReadEvidence(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "config.json").write_text(json.dumps({
            "device": "workstation", "aliases": ["desk"], "peers": {"sender": {}},
        }))
        self.author = {"device": "sender", "client": "writer", "session": "source"}
        self.target = {"device": "workstation", "client": "reader", "session": "target"}
        messages.handle(self.root, "deliver", {
            "id": "letter", "from": "sender", "to": "workstation", "envelope_version": 2,
            "text": "private body", "sender_endpoint": self.author, "recipient_identity": self.target,
        })
        messages._receipt(self.root, {"id": "letter", "reader_endpoint": self.target})

    def observe(self, **actor_fields):
        result = mail_lifecycle.apply(self.root, {
            "op": "status", "id": "letter", "actor": {**self.author, **actor_fields},
        }, authenticated_peer="sender", expected_target=self.target)
        return mail_control.checked_result(result, "letter")

    def test_original_session_and_alias_match_but_neighbor_cannot_query(self):
        self.assertEqual(self.observe()["read_scope"], "session")
        with messages._database(self.root) as db:
            db.execute("UPDATE messages SET read_by_endpoint=? WHERE id='letter'",
                       (json.dumps({**self.target, "device": "desk"}),))
        self.assertEqual(self.observe()["read_scope"], "session")
        self.assertEqual(self.observe(session="neighbor")["status"], "unavailable")

    def test_old_other_or_malformed_reader_does_not_prove_original_chat_read(self):
        for stored, expected in (
            (None, "device"), (json.dumps({**self.target, "session": "neighbor"}), None),
            (json.dumps({**self.target, "client": "other"}), None),
            (json.dumps({**self.target, "device": "sender"}), None),
            ("broken", None), ("{}", None),
        ):
            with self.subTest(stored=stored), messages._database(self.root) as db:
                db.execute("UPDATE messages SET read_by_endpoint=? WHERE id='letter'", (stored,))
                db.commit()
                result = self.observe()
                self.assertTrue(result["read_at"])
                self.assertEqual(result["read_scope"], expected)

    def test_folder_control_carries_only_public_delivery_fields(self):
        gateway = Path(__file__).resolve().parents[1] / "deploy" / "paired-folder-mail.py"
        project = runpy.run_path(str(gateway))["observed_fields"]
        result = project({"delivery": {
            "state": "read", "retry": "never", "text": "private body",
            "evidence": ["recipient_read", {"raw": "private"}, "private", "recipient_read"],
        }})
        self.assertEqual(result, {"delivery": {
            "state": "read", "retry": "never", "evidence": ["recipient_read"],
        }})
        self.assertEqual(project({"delivery": {"retry": "private", "state": "private"}}), {"delivery": {}})


if __name__ == "__main__":
    unittest.main()
