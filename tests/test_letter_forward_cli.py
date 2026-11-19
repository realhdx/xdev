"""A, B, C and D use normal CLI entrypoints on two isolated Windows-device mailboxes.

Only peer transport and native admission are simulated. Letter creation, reading,
routing, deduplication, correlation and storage use the production dispatcher.
"""

import json
from pathlib import Path
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

from src.adapters import caller_context, inprocess_cli, sessions
from src.cli import main
from src.runtime import letter_attachment, messages


A = {"device": "laptop", "client": "codex", "session": "00000000-0000-4000-8000-000000000001"}
B = {"device": "desktop", "client": "dsh", "session": "session-00000000-0000-4000-8000-000000000002"}
C = {"device": "laptop", "client": "opencode", "session": "ses_forward_3"}
D = {"device": "desktop", "client": "codex", "session": "00000000-0000-4000-8000-000000000004"}
ORIGINAL = "第一句是 A 的原话。\n第二行保留数值 17.5 和单位 μm。\n最后一行。"
QUOTE = "第二行保留数值 17.5 和单位 μm。"
NOTE = "B 的意见：请 C 核实单位。"


def target(endpoint):
    return ["--to", endpoint["device"], "--client", endpoint["client"], "--session", endpoint["session"]]


def also(endpoint):
    return ["--also", endpoint["device"], endpoint["client"], endpoint["session"]]


class ForwardThroughCli(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="xdev-forward-cli-")))
        self.roots = {name: directory / name for name in ("laptop", "desktop")}
        for name, root in self.roots.items():
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"device": name}), encoding="utf-8")

        def transport(host, wire, timeout, *, root=None):
            return main._request(self.roots[host], wire["service"], wire["action"], wire["payload"])
        self.enterContext(patch.object(main.transport, "peer_request", side_effect=transport))
        self.admitted = self.enterContext(patch.object(sessions, "notify", return_value={
            "ok": True, "status": "admitted", "delivery_mode": "send_now",
            "admission_evidence": "native_send_now_accepted"}))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("No real providers")))

    def call(self, who, *args):
        if who["client"] == "dsh":
            binding = caller_context.bind_dsh(who["session"])
        else:
            key = "threadId" if who["client"] == "codex" else "ai.opencode/sessionID"
            binding = caller_context.bind({key: who["session"]})
        with binding:
            return inprocess_cli.invoke(["msg", *args], self.roots[who["device"]])

    def ok(self, who, *args):
        result = self.call(who, *args)
        self.assertTrue(result.get("ok"), result)
        return result

    def addressed(self, actor, text):
        return [message for message in self.ok(actor, "inbox")["messages"] if message["text"] == text]

    def read_item(self, actor, item):
        return self.ok(actor, "read", item["id"])["message"]

    def enable_old_readers(self):
        aliases = {"laptop": "移动站", "desktop": "研究室"}
        for role, peer in (("laptop", "desktop"), ("desktop", "laptop")):
            root = self.roots[role]
            config = {"device": role, "primary_name": aliases[role], "legacy_clients": True,
                      "identity_key": role, "peers": {aliases[peer]: {"identity_key": peer,
                      "ssh": peer, "aliases": [peer]}}}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            self.roots[aliases[role]] = root
        return aliases

    def test_coexisting_readers_get_role_wire_and_new_named_receipts(self):
        names = self.enable_old_readers()
        address_b = {**B, "device": names["desktop"]}
        sent = self.ok(A, "send", *target(address_b), "--id", "mixed", "--text", ORIGINAL)
        self.assertEqual(sent["host"], names["desktop"])
        self.assertEqual(sent["sender_endpoint"]["device"], names["laptop"])
        self.assertEqual(sent["recipient_endpoint"], address_b)
        stored = sent["inbox_delivery"]["message"]
        self.assertEqual((stored["sender_endpoint"], stored["reply_endpoint"], stored["recipient_identity"]), (A, A, B))
        self.assertEqual(stored["canonical_addresses"], {"from": names["laptop"], "to": names["desktop"]})
        self.assertFalse(self.call(D, "read", "mixed")["ok"])
        read = self.ok(B, "read", "mixed")
        self.assertEqual(read["read_by_endpoint"], address_b)
        self.assertEqual(read["message"]["read_by_endpoint"], B)
        self.assertEqual(read["reply_choices"][0]["destination"]["device"], names["laptop"])
        replay = self.ok(A, "send", *target(B), "--id", "mixed", "--text", ORIGINAL)
        self.assertEqual(replay["inbox_status"], "duplicate")
        response = self.ok(B, "reply", "mixed", "--text", "已核查")
        self.assertEqual(response["host"], names["laptop"])
        self.assertEqual(self.addressed(A, "已核查")[0]["reply_to"], "mixed")

    def test_mixed_group_forward_keeps_copies_and_bytes_on_retry(self):
        names = self.enable_old_readers()
        self.ok(A, "send", *target(B), "--id", "mixed-source", "--text", ORIGINAL)
        source = self.ok(B, "read", "mixed-source")["message"]
        original = letter_attachment.make_attachment(source, mode="quote", quote=QUOTE)
        args = ["forward", "mixed-source", "--id", "mixed-batch", "--quote", QUOTE, "--text", NOTE]
        first = self.ok(B, *args, *target({**C, "device": names["laptop"]}), *also(D))
        again = self.ok(B, *args, *target(D), *also(C))
        self.assertEqual({r["host"] for r in again["deliveries"]}, set(names.values()))
        self.assertTrue(all(r["previously_accepted"] for r in again["deliveries"]))
        self.assertEqual({r["id"] for r in first["deliveries"]}, {r["id"] for r in again["deliveries"]})
        for actor in (C, D):
            rows = self.addressed(actor, NOTE)
            self.assertEqual(len(rows), 1)
            letter = self.read_item(actor, rows[0])
            self.assertEqual(letter["letter_attachment"], original)
            self.assertEqual({m["device"] for m in letter["group"]["members"]}, {"laptop", "desktop"})
        self.ok(C, "reply", self.addressed(C, NOTE)[0]["id"], "--audience", "all", "--text", "已核读原件")
        self.assertEqual(len(self.addressed(B, "已核读原件")), 1)
        self.assertEqual(len(self.addressed(D, "已核读原件")), 1)

    def test_signed_custom_name_original_stays_intact_for_upgraded_recipient(self):
        names = self.enable_old_readers()
        prior = {"id": "pre-upgrade", "from": names["laptop"], "to": names["desktop"], "text": ORIGINAL,
                 "sender_endpoint": {**A, "device": names["laptop"]}, "recipient_identity": {**B, "device": names["desktop"]},
                 "sender_identity_source": "explicit"}
        saved = letter_attachment.make_attachment(prior, mode="quote", quote=QUOTE, include_full=False)
        current = {"id": "copied", "from": names["desktop"], "to": names["laptop"], "text": NOTE,
                   "sender_endpoint": {**B, "device": names["desktop"]},
                   "recipient_identity": {**C, "device": names["laptop"]}, "letter_attachment": saved}
        received = main._request(self.roots["laptop"], "msg", "deliver", current)
        self.assertEqual(received["message"]["from"], "desktop")
        copy = self.read_item(C, received["message"])["letter_attachment"]
        self.assertEqual(copy, saved)
        self.assertEqual(copy["original"]["sender_endpoint"]["device"], names["laptop"])
        wire = messages.mail_payload(self.roots["laptop"], current)
        self.assertIs(wire["letter_attachment"], saved)

    def original(self):
        self.ok(A, "send", "--id", "from-A", *target(B), "--text", ORIGINAL)
        item = self.addressed(B, ORIGINAL)[0]
        self.assertEqual(item["sender_endpoint"], A)
        self.assertTrue(item["subject"].strip())

    def test_subject_persists_through_forward_and_reply_without_grouping_unrelated_letters(self):
        topic = "单位核对"
        self.ok(A, "send", "--id", "topic-a", *target(B), "--subject", topic, "--text", ORIGINAL)
        self.ok(C, "send", "--id", "topic-c", *target(B), "--subject", topic, "--text", "C 的独立问题")
        items = self.ok(B, "inbox", "--subject", topic)["messages"]
        self.assertCountEqual([(m["id"], m["sender_endpoint"]) for m in items],
                              [("topic-a", A), ("topic-c", C)])
        self.assertEqual(self.ok(D, "inbox", "--subject", topic)["messages"], [])
        self.ok(B, "reply", "topic-a", "--text", "只答 A")
        self.assertEqual(self.addressed(A, "只答 A")[0]["subject"], topic)
        self.assertEqual(self.addressed(C, "只答 A"), [])
        self.ok(B, "forward", "topic-a", *target(C), "--text", "沿用题目")
        self.assertEqual(self.addressed(C, "沿用题目")[0]["subject"], topic)
        self.ok(B, "forward", "topic-a", *target(C), "--text", "转寄换题", "--subject", "请 C 复核")
        changed = self.read_item(C, self.addressed(C, "转寄换题")[0])
        self.assertEqual(changed["subject"], "请 C 复核")
        self.assertEqual(changed["letter_attachment"]["original"]["subject"], topic)
        self.ok(C, "reply", changed["id"], "--text", "回 B 新题")
        self.assertEqual(self.addressed(B, "回 B 新题")[0]["subject"], "请 C 复核")

    def test_forward_preserves_full_original_and_b_can_choose_a_different_quote(self):
        self.original()
        options = [*target(C), "--text", NOTE]
        self.ok(B, "forward", "from-A", *options)
        self.ok(B, "forward", "from-A", *options)
        items = self.addressed(C, NOTE)
        self.assertEqual(len(items), 1, "An identical no-ID retry should not produce another letter")
        self.assertIn("letter_attachment_summary", items[0])
        self.assertNotIn("letter_attachment", items[0])
        self.assertNotIn(ORIGINAL.splitlines()[0], json.dumps(items[0], ensure_ascii=False))
        forwarded = self.ok(C, "read", items[0]["id"])["message"]
        self.assertEqual(forwarded["sender_endpoint"], B)
        self.assertEqual(forwarded["recipient_identity"], C)
        self.assertEqual(forwarded["reply_endpoint"], B)
        original = forwarded["letter_attachment"]["original"]
        self.assertEqual((original["id"], original["text"], original["sender_endpoint"]),
                         ("from-A", ORIGINAL, A))
        self.assertEqual((original["from"], original["to"]), ("laptop", "desktop"))
        self.assertEqual(forwarded["letter_attachment"]["mode"], "forward")
        self.assertEqual(original["sender_identity_source"], "mcp_metadata")
        self.assertEqual(self.addressed(A, NOTE), [])
        self.ok(B, "forward", "from-A", *options, "--quote", QUOTE)
        self.assertEqual(len(self.addressed(C, NOTE)), 2)
        quoted = next(item for item in self.addressed(C, NOTE) if item["id"] != forwarded["id"])
        attachment = self.read_item(C, quoted)["letter_attachment"]
        self.assertEqual(attachment["quote"], QUOTE)
        self.assertEqual(attachment["scope"], "full")
        self.assertEqual(attachment["original"]["text"], ORIGINAL)
        self.ok(B, "forward", "from-A", *target(C), "--text", "B 只摘引", "--quote", QUOTE, "--excerpt-only")
        excerpt = self.read_item(C, self.addressed(C, "B 只摘引")[0])["letter_attachment"]
        self.assertEqual((excerpt["quote"], excerpt["scope"]), (QUOTE, "partial"))
        self.assertNotIn("text", excerpt["original"])

    def test_misaddressed_forward_and_fabricated_quote_do_not_reach_c(self):
        self.original()
        self.admitted.reset_mock()
        options = [*target(C), "--text", NOTE]
        self.assertFalse(self.call(D, "forward", "from-A", *options)["ok"])
        self.assertFalse(self.call(B, "forward", "from-A", *options, "--quote", "第二行保留数值 1750。")["ok"])
        self.assertEqual(self.addressed(C, NOTE), [])
        self.admitted.assert_not_called()

    def test_existing_send_action_forwards_with_identical_attribution_and_retry_id(self):
        self.original()
        via = self.ok(B, "read", "from-A")["forward_via"]
        self.assertEqual(via, {"action": "send", "source_arguments": ["--forward-of", "from-A"]})
        options = [*target(C), "--quote", QUOTE, "--subject", "兼容端转寄", "--text", NOTE]
        modern = self.ok(B, "forward", "from-A", *options)
        compatible = self.ok(B, via["action"], *via["source_arguments"], *options)
        self.assertEqual(compatible["id"], modern["id"])
        items = self.addressed(C, NOTE)
        self.assertEqual(len(items), 1)
        got = self.read_item(C, items[0])
        self.assertEqual((got["sender_endpoint"], got["subject"]), (B, "兼容端转寄"))
        self.assertEqual(got["letter_attachment"]["quote"], QUOTE)
        self.assertEqual(got["letter_attachment"]["original"]["sender_endpoint"], A)
        self.assertEqual(got["letter_attachment"]["original"]["text"], ORIGINAL)
        self.assertFalse(self.call(D, "send", "--forward-of", "from-A", *options)["ok"])

    def test_reused_explicit_id_cannot_replace_already_forwarded_content(self):
        self.original()
        options = ["forward", "from-A", *target(C), "--id", "b-forward-once"]
        self.ok(B, *options, "--text", NOTE)
        self.ok(B, *options, "--text", NOTE)
        self.assertFalse(self.call(B, *options, "--text", "企图把同一封转寄改掉")["ok"])
        items = self.addressed(C, NOTE)
        self.assertEqual(len(items), 1)
        self.assertEqual(self.read_item(C, items[0])["letter_attachment"]["original"]["text"], ORIGINAL)

    def test_recipient_can_reply_to_forwarder_or_separately_to_original_author(self):
        self.original()
        self.ok(B, "forward", "from-A", *target(C), "--text", NOTE)
        forwarded = self.addressed(C, NOTE)[0]["id"]
        self.ok(C, "reply", forwarded, "--text", "C 回 B")
        self.ok(C, "reply", forwarded, "--audience", "original-author", "--text", "C 另问 A")
        to_b = self.addressed(B, "C 回 B")[0]
        self.assertEqual((to_b["sender_endpoint"], to_b["recipient_identity"]), (C, B))
        self.assertEqual(to_b["reply_to"], forwarded)
        self.assertEqual(self.addressed(A, "C 回 B"), [])
        to_a = self.read_item(A, self.addressed(A, "C 另问 A")[0])
        self.assertEqual((to_a["sender_endpoint"], to_a["recipient_identity"]), (C, A))
        self.assertEqual(self.addressed(B, "C 另问 A"), [])
        linkage = json.dumps(to_a, ensure_ascii=False)
        self.assertIn("from-A", linkage)
        self.assertIn(forwarded, linkage)

    def test_ungrouped_device_forward_does_not_notify_every_chat_on_that_device(self):
        self.original()
        self.admitted.reset_mock()
        self.ok(B, "forward", "from-A", "--to", "laptop", "--text", NOTE)
        self.assertEqual(self.addressed(A, NOTE), [])
        self.assertEqual(self.addressed(C, NOTE), [])
        items = self.ok(A, "list", "--device-inbox")["messages"]
        letter = next(item for item in items if item["text"] == NOTE)
        self.assertEqual(letter["sender_endpoint"], B)
        self.assertIsNone(letter["recipient_identity"])
        self.assertEqual(self.read_item(A, letter)["letter_attachment"]["original"]["text"], ORIGINAL)
        self.admitted.assert_not_called()

    def test_original_author_mode_cannot_infer_an_author_from_a_third_party_callback(self):
        with caller_context.bind(None):
            value = inprocess_cli.invoke(["msg", "send", *target(B), "--id", "device-mail",
                                         "--text", ORIGINAL, "--as-device", "--return-client", A["client"],
                                         "--return-session", A["session"]], self.roots["laptop"])
        self.assertTrue(value.get("ok"), value)
        self.ok(B, "forward", "device-mail", *target(C), "--text", NOTE)
        forwarded = self.read_item(C, self.addressed(C, NOTE)[0])
        self.assertIsNone(forwarded["letter_attachment"]["original"]["sender_endpoint"])
        self.admitted.reset_mock()
        reply = self.call(C, "reply", forwarded["id"], "--audience", "original-author", "--text", "请求联系原作者")
        self.assertFalse(reply["ok"])
        self.admitted.assert_not_called()

    def test_forward_to_group_then_reply_all_targets_each_other_member_once(self):
        self.original()
        options = [*target(C), *also(D), *also(A), "--text", NOTE]
        self.ok(B, "forward", "from-A", *options)
        self.ok(B, "forward", "from-A", *target(C), *also(A), *also(D), "--text", NOTE)
        letters = {}
        for person in (C, D, A):
            received = self.addressed(person, NOTE)
            self.assertEqual(len(received), 1)
            letters[person["session"]] = received[0]
            self.assertEqual(received[0]["sender_endpoint"], B)
        c_copy = letters[C["session"]]
        self.assertEqual(len({item["group"]["id"] for item in letters.values()}), 1)
        self.ok(C, "reply", c_copy["id"], "--audience", "all", "--text", "C 答复全体")
        for intended in (B, A, D):
            recipients = self.addressed(intended, "C 答复全体")
            self.assertEqual(len(recipients), 1)
            self.assertEqual(recipients[0]["sender_endpoint"], C)
        self.assertEqual(self.addressed(C, "C 答复全体"), [])

    def test_failed_group_member_can_recover_without_waking_an_already_accepted_chat(self):
        self.original()
        attempts = Counter()
        accepted = self.admitted.return_value
        def notify(root, payload):
            session = payload["session"]
            attempts[session] += 1
            if session == D["session"] and attempts[session] == 1:
                return {"ok": False, "status": "requires_activation", "error_code": "TEST_OFFLINE",
                        "current_turn_accepted": False}
            return accepted
        self.admitted.side_effect = notify
        options = ["forward", "from-A", *target(C), *also(D), "--text", "分户回执"]
        first = self.call(B, *options)
        self.assertFalse(first["ok"])
        self.assertEqual(first["status"], "group_incomplete")
        statuses = {item["session"]: item for item in first["deliveries"]}
        self.assertTrue(statuses[C["session"]]["ok"])
        self.assertFalse(statuses[D["session"]]["ok"])
        second = self.ok(B, *options)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "group_delivered")
        retried = {item["session"]: item for item in second["deliveries"]}
        self.assertTrue(retried[C["session"]]["previously_accepted"])
        self.assertEqual(retried[D["session"]]["id"], statuses[D["session"]]["id"])
        self.assertEqual(attempts, Counter({C["session"]: 1, D["session"]: 2}))
        self.assertEqual(len(self.addressed(C, "分户回执")), 1)
        self.assertEqual(len(self.addressed(D, "分户回执")), 1)

    def test_initial_group_and_multihop_forward(self):
        self.ok(A, "send", *target(B), *also(C), "--text", ORIGINAL)
        b_copy = self.addressed(B, ORIGINAL)[0]
        c_copy = self.addressed(C, ORIGINAL)[0]
        self.assertEqual(b_copy["group"]["id"], c_copy["group"]["id"])
        self.ok(B, "forward", b_copy["id"], *target(D), "--id", "B-to-D", "--text", NOTE)
        first_hop = self.addressed(D, NOTE)[0]
        self.ok(D, "forward", first_hop["id"], *target(C), "--text", "D 的补充")
        second_hop = self.read_item(C, self.addressed(C, "D 的补充")[0])
        self.assertEqual(second_hop["sender_endpoint"], D)
        middle = second_hop["letter_attachment"]["original"]
        self.assertEqual(middle["sender_endpoint"], B)
        self.assertEqual(middle["letter_attachment"]["original"]["sender_endpoint"], A)
        self.assertEqual(middle["letter_attachment"]["original"]["text"], ORIGINAL)
