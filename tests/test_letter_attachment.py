import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from src.runtime import letter_attachment as letters


class LetterAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.author = {"device": "desktop", "client": "codex", "session": "11111111-2222-3333-4444-555555555555",
                       "codex_home": "D:/ChatProfiles/Original"}
        self.recipient = {"device": "laptop", "client": "reader-app", "session": "target"}
        self.message = {"id": "first", "from": "desktop", "to": "laptop", "kind": "reply", "reply_to": "previous",
                        "text": "逐字\r\nＡ，e\u0301 😀\n摘录\n", "sender_endpoint": self.author, "sender_identity_source": "cli_host",
                        "recipient_identity": self.recipient, "reply_endpoint": {"device": "laptop", "client": "other"},
                        "received_at": "irrelevant", "read_by_endpoint": "irrelevant", "read_at": "irrelevant",
                        "mailbox_path": "X:/private/mail.sqlite3"}

    def test_receiving_without_source_store_recovers_exact_letter_and_independent_sha(self):
        source = copy.deepcopy(self.message)
        captured = letters.make_attachment(source)
        source["text"] = "edited"
        source["sender_endpoint"]["session"] = "changed"
        wire = json.dumps(captured, ensure_ascii=False)
        verified = letters.validate_attachment(json.loads(wire))
        original = verified["original"]
        self.assertEqual((original["text"], original["reply_to"], original["sender_endpoint"], original["recipient_identity"]),
                         (self.message["text"], "previous", self.author, self.recipient))
        self.assertEqual(original["text_sha256"], sha256(self.message["text"].encode()).hexdigest())
        data = {k: v for k, v in verified.items() if k != "sha256"}
        canonical = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(verified["sha256"], sha256(canonical).hexdigest())
        self.assertIn("not_independently_signed", verified["integrity"])
        for field in ("received_at", "read_by_endpoint", "reply_endpoint", "mailbox_path", "read_at"):
            self.assertNotIn(field, original)
        verified["original"]["sender_endpoint"]["session"] = "mutated returned object"
        self.assertEqual(captured["original"]["sender_endpoint"], self.author)

    def test_default_quote_keeps_full_letter_exact_offset_and_existing_attachment(self):
        first = letters.make_attachment(self.message)
        middle = {**self.message, "id": "middle", "text": "二次说明\n二次说明", "letter_attachment": first}
        second = letters.make_attachment(middle, mode="quote", quote="次说明")
        self.assertEqual((second["scope"], second["quote_start"], second["original"]["text"]), ("full", 1, middle["text"]))
        forwarded = letters.make_attachment({**self.message, "id": "outer", "letter_attachment": second})
        chain = forwarded["original"]["letter_attachment"]["original"]["letter_attachment"]
        self.assertEqual(chain, first)
        self.assertEqual(chain["original"]["text"], self.message["text"])
        middle["letter_attachment"]["original"]["text"] = "later change"
        self.assertNotEqual(second["original"]["letter_attachment"]["original"]["text"], "later change")

    def test_excerpt_only_omits_source_text_and_ancestral_text_but_keeps_fingerprints(self):
        first = letters.make_attachment({**self.message, "text": "private ancestral letter"})
        result = letters.make_attachment({**self.message, "letter_attachment": first},
                                         mode="quote", quote="😀\n摘", include_full=False)
        self.assertEqual(result["original"]["attached_sha256"], first["sha256"])
        self.assertEqual(result["original"]["text_sha256"], sha256(self.message["text"].encode()).hexdigest())
        self.assertEqual(result["scope"], "partial")
        self.assertNotIn("text", result["original"])
        self.assertNotIn("private ancestral letter", json.dumps(result))
        self.assertEqual(letters.validate_attachment(result), result)

    def test_verbatim_checks_do_not_fold_case_unicode_or_line_endings(self):
        for quote in ("a", "é", "逐字\n", "not present", ""):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                letters.make_attachment(self.message, mode="quote", quote=quote)
        for opts in ({"mode": "forward", "include_full": False}, {"quote": "逐字"}, {"mode": "other"}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                letters.make_attachment(self.message, **opts)

    def test_only_a_route_to_the_actual_author_can_be_carried(self):
        for client, adapter in (("claude", "claude-channel"), ("workbuddy", "codebuddy-channel")):
            actor = {k: self.author[k] for k in ("device", "session")}
            actor["client"] = client
            route = {**actor, "client": adapter, "delivery": "queue"}
            captured = letters.make_attachment({**self.message, "sender_endpoint": actor, "reply_endpoint": route})
            self.assertEqual(letters.validate_attachment(captured)["original"]["author_route"], route)
            for redirected in ({**route, "device": "laptop"}, {**route, "session": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                               {"device": "desktop", "channel": "tool_result", "request_id": "first"}, actor):
                other = letters.make_attachment({**self.message, "sender_endpoint": actor, "reply_endpoint": redirected})
                self.assertNotIn("author_route", other["original"])
            wrong = copy.deepcopy(captured)
            wrong["original"]["author_route"]["session"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            wrong["sha256"] = letters._digest({k: v for k, v in wrong.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                letters.validate_attachment(wrong)

    def test_callback_profile_must_match_and_only_immutable_routes_affect_the_copy(self):
        queued = {**self.author, "delivery": "queue"}
        captured = letters.make_attachment({**self.message, "reply_endpoint": {**queued, "codex_home": "d:/chatprofiles/ORIGINAL"}})
        self.assertEqual(captured["original"]["author_route"]["delivery"], "queue")
        for candidate in ({**queued, "codex_home": "D:/Different"}, {k:v for k,v in queued.items() if k != "codex_home"}):
            self.assertNotIn("author_route", letters.make_attachment({**self.message, "reply_endpoint": candidate})["original"])
        later = {**self.message, "reply_endpoint": queued, "reply_endpoint_source": "explicit_local_annotation"}
        self.assertNotIn("author_route", letters.make_attachment(later)["original"])

    def test_summary_only_input_requires_source_read_and_subject_is_optional_for_legacy_digests(self):
        old = letters.make_attachment({**self.message, "subject_preview": "dynamic preview"})
        self.assertEqual(old["sha256"], "45e7d00ea327e9f8df9570d759396cd6c0aadc701ec747dbf052bfb927ae3e47")
        self.assertNotIn("subject", letters.validate_attachment(old)["original"])
        current = letters.make_attachment({**self.message, "subject": "测试   标题"})
        self.assertEqual(letters.validate_attachment(current)["original"]["subject"], "测试 标题")
        self.assertNotEqual(old["sha256"], current["sha256"])
        with self.assertRaises(ValueError):
            letters.make_attachment({**self.message, "subject": "标题\n伪装另一行"})
        with self.assertRaisesRegex(ValueError, "by ID"):
            letters.make_attachment({**self.message, "letter_attachment_summary": {"sha256": old["sha256"]}})

    def test_legacy_unknown_sender_does_not_inherit_callback_and_malformed_data_fails_closed(self):
        unknown = {k: self.message[k] for k in ("from", "to", "id", "text", "reply_endpoint")}
        result = letters.make_attachment(unknown)
        self.assertIsNone(result["original"]["sender_endpoint"])
        self.assertEqual(result["original"]["sender_identity_source"], "unknown")
        for change in ({"sender_endpoint_error": "corrupt"}, {"sender_identity_source": "unknown"},
                       {"to": "desktop"}, {"kind": "receipt", "reply_to": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                letters.make_attachment({**self.message, **change})

    def test_outer_nested_and_partial_tampering_are_rejected(self):
        capture = letters.make_attachment(self.message, mode="quote", quote="摘录")
        for key, change in (("sha256", "a" * 64), ("quote", "逐字"), ("quote_start", True), ("extra", "bad")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                letters.validate_attachment({**capture, key: change})
        for key, change in (("text", "changed"), ("id", "other"), ("read_at", "invented")):
            modified = copy.deepcopy(capture)
            modified["original"][key] = change
            with self.subTest(key=key), self.assertRaises(ValueError):
                letters.validate_attachment(modified)
        nested = letters.make_attachment({**self.message, "letter_attachment": capture})
        nested["original"]["letter_attachment"]["original"]["sender_endpoint"]["session"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with self.assertRaises(ValueError):
            letters.validate_attachment(nested)

    def test_independent_destination_mailbox_reads_without_access_to_original_letter(self):
        from src.runtime import messages
        attachment = letters.make_attachment(self.message, mode="quote", quote="😀")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            intermediary = {"device": "laptop", "client": "reader-app", "session": "target"}
            final = {"device": "desktop", "client": "other-app", "session": "chat-c"}
            envelope = {"id": "forwarded-b-to-c", "from": "laptop", "to": "desktop", "text": "B's own comment",
                        "sender_endpoint": intermediary, "recipient_identity": final, "letter_attachment": attachment}
            delivered = messages.handle(target, "deliver", json.loads(json.dumps(envelope)))
            self.assertEqual(delivered["status"], "received")
            read = messages.handle(target, "receipt", {"id": envelope["id"], "reader_endpoint": final})["message"]
            self.assertEqual(read["sender_endpoint"], intermediary)
            self.assertEqual(read["text"], envelope["text"])
            self.assertEqual(read["letter_attachment"]["original"]["text"], self.message["text"])
            self.assertEqual(read["letter_attachment"]["original"]["sender_endpoint"], self.author)
            self.assertEqual(messages.handle(target, "wait", {"id": self.message["id"], "timeout": 0})["message"], None)

    def test_depth_and_total_size_fail_without_silent_truncation(self):
        letter = letters.make_attachment(self.message)
        for i in range(letters.MAX_DEPTH - 1):
            letter = letters.make_attachment({**self.message, "id": str(i), "letter_attachment": letter})
        with self.assertRaisesRegex(ValueError, "levels"):
            letters.make_attachment({**self.message, "letter_attachment": letter})
        self.assertEqual(letters.make_attachment({**self.message, "text": "中" * 65536})["original"]["text"], "中" * 65536)
        with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
            letters.make_attachment({**self.message, "text": "😀" * 65536})
        with self.assertRaises(ValueError):
            letters.make_attachment({**self.message, "text": "x" * 65537})


if __name__ == "__main__":
    unittest.main()
