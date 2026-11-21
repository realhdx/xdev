import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "paired_folder_mail", Path(__file__).with_name("paired-folder-mail.py")
)
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


class MailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.share = self.base / "share"
        self.root = self.base / "runtime"
        self.root.mkdir()
        (self.root / "launch.py").touch()
        self.inbox = []
        self.calls = []
        self.sent = {}
        self.lose_result = False
        self.enroll()

    def enroll(self, peer="peer-s"):
        return mail.init(
            self.state, self.share, self.root, "dot-root", "WIN", "dsh", peer, self.call
        )

    def call(self, cfg, args):
        self.calls.append(args)

        def value(flag):
            return args[args.index(flag) + 1]

        if args[0] == "whoami":
            return {"device": "LAP"}
        if args[0] == "inbox":
            self.assertEqual(value("--from"), "WIN")
            self.assertEqual(value("--from-session"), "peer-s")
            self.assertEqual(value("--as-client"), "paired-folder")
            self.assertEqual(value("--as-session"), "dot-root")
            return {
                "messages": [m for m in self.inbox if m["seq"] > int(value("--after"))][
                    : mail.BATCH
                ]
            }
        if args[0] == "read":
            return {
                "id": args[1],
                "status": "read",
                "read_by_endpoint": dict(
                    device="LAP", client="paired-folder", session="dot-root"
                ),
            }
        if args[0] == "wait":
            return {"message": self.sent.get(value("--id"))}
        if args[0] == "send":
            mid = value("--id")
            self.assertNotIn(mid, self.sent)
            local, peer = mail.endpoints(cfg)
            self.assertEqual(value("--to"), peer["device"])
            self.assertEqual(value("--return-client"), local["client"])
            self.assertEqual(value("--return-session"), local["session"])
            self.sent[mid] = {
                "id": mid,
                "from": "LAP",
                "to": "WIN",
                "sender_endpoint": local,
                "recipient_identity": peer,
                "reply_to": value("--reply-to") if "--reply-to" in args else None,
                "subject": next(
                    s.split("=", 1)[1] for s in args if s.startswith("--subject=")
                ),
                "text": next(
                    s.split("=", 1)[1] for s in args if s.startswith("--text=")
                ),
            }
            if self.lose_result:
                raise subprocess.TimeoutExpired("xdev", 1)
            return {"session_accepted": True, "id": mid}
        self.fail(args[0])

    def source(self, mid="incoming", seq=1):
        return {
            "id": mid,
            "seq": seq,
            "from": "legacy-desk",
            "to": "legacy-lap",
            "canonical_addresses": {"from": "WIN", "to": "LAP"},
            "sender_endpoint": {
                "device": "legacy-desk",
                "client": "dsh",
                "session": "peer-s",
            },
            "recipient_identity": {
                "device": "legacy-lap",
                "client": "paired-folder",
                "session": "dot-root",
            },
            "kind": "message",
            "text": "body",
            "subject": "Topic",
        }

    def request(self, slot, op, **fields):
        path = self.share / slot / (op + ".json")
        mail.put(path, dict(op_id=op, **fields))
        return path

    def result(self, op):
        return mail.read_request(self.share / "results" / (op + ".json"))

    def step(self):
        return mail.step(self.state, self.call)

    def test_two_directions_explicit_read_and_reenroll(self):
        self.inbox = [self.source()]
        self.request("out", "first", subject="我方发起", text="--host elsewhere")
        self.step()
        self.assertEqual(self.result("first")["status"], "session_accepted")
        self.assertEqual(mail.pending(self.state)["pending"], ["incoming"])
        self.assertEqual(
            mail.read_request(self.share / "in" / "incoming.json")["text"], "body"
        )
        self.assertFalse(any(a[0] == "read" for a in self.calls))
        self.request("read", "opened", message_id="incoming")
        self.request("out", "respond", subject="原信", text="答复", reply_to="incoming")
        self.enroll()
        self.step()
        self.assertEqual(self.result("opened")["status"], "read")
        self.assertEqual(mail.pending(self.state)["pending"], [])
        self.assertEqual(
            self.sent[self.result("respond")["message_id"]]["reply_to"], "incoming"
        )
        self.step()
        self.assertEqual(sum(a[0] == "send" for a in self.calls), 2)
        with self.assertRaisesRegex(ValueError, "enrollment_conflict"):
            self.enroll(peer="other-session")

    def test_timeout_uses_readonly_reconciliation_even_after_restart(self):
        self.lose_result = True
        self.request("out", "one", subject="First", text="some text")
        self.step()
        self.assertEqual(self.result("one")["status"], "unknown")
        self.enroll()
        self.step()

        def due():
            with closing(sqlite3.connect(self.state / "mail.sqlite")) as db, db:
                db.execute("UPDATE ops SET retry_after=0")

        mid = self.result("one")["message_id"]
        self.sent[mid]["subject"] = "Different"
        due()
        self.step()
        self.assertEqual(self.result("one")["status"], "unknown")
        self.sent[mid]["subject"] = "First"
        due()
        self.step()
        self.assertEqual(self.result("one")["status"], "mailbox_received")
        self.assertEqual(sum(a[0] == "send" for a in self.calls), 1)

    def test_cannot_redirect_or_ack_unexported_item(self):
        second = self.source("mail2", 2)
        second["recipient_identity"]["session"] = "different"
        third = self.source("mail3", 3)
        third["kind"] = "receipt"
        self.inbox = [self.source(), second, third]
        self.request("out", "redirect", subject="x", text="y", to="arbitrary")
        self.request("read", "forged", message_id="mail2")
        self.request("out", "badpath", subject="x", text="y", reply_to="../../other")
        self.request("out", "third", subject="x", text="y", reply_to="unknown")
        self.step()
        self.assertEqual(mail.pending(self.state)["pending"], ["incoming"])
        for op in ("redirect", "forged", "badpath", "third"):
            self.assertEqual(self.result(op)["status"], "rejected")
        self.assertFalse(any(a[0] in ("send", "read") for a in self.calls))

    def test_mutated_operation_cannot_send_twice(self):
        self.request("out", "same", subject="S", text="once")
        self.step()
        self.request("out", "same", subject="S", text="changed")
        self.step()
        self.assertEqual(self.result("same")["reason"], "op_id_conflict")
        self.assertEqual(len(self.sent), 1)

    def test_mailbox_cursor_pages_only_unread(self):
        self.inbox = [self.source("letter-" + str(i), i) for i in range(1, 55)]
        for _ in range(4):
            self.step()
        page = mail.pending(self.state)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["pending"]), 50)
        self.assertEqual(
            mail.pending(self.state, page["next_cursor"])["pending"],
            ["letter-51", "letter-52", "letter-53", "letter-54"],
        )

    def test_link_and_reserved_name_not_consumed(self):
        with self.assertRaises(ValueError):
            mail.ident("CON", mail.OP)
        hidden = self.base / "outside.json"
        hidden.write_text(
            '{"op_id":"rogue","text":"hello","subject":"s"}', encoding="utf8"
        )
        try:
            (self.share / "out" / "rogue.json").symlink_to(hidden)
        except OSError:
            os.link(hidden, self.share / "out" / "rogue.json")
        self.assertEqual(self.step()["rejected"], 1)
        self.assertEqual(len(self.sent), 0)

    def test_gateway_checks_live_inbox_even_for_stale_snapshot_or_old_pending(self):
        from src.runtime import messages, mail_lifecycle

        (self.root / "config.json").write_text(json.dumps({"device":"LAP","peers":{"WIN":{}}}))
        sender = {"device":"WIN","client":"writer","session":"peer-s"}
        target = {"device":"LAP","client":"paired-folder","session":"dot-root"}
        self.state, self.share = self.base / "lifecycle-state", self.base / "lifecycle-share"
        mail.init(self.state,self.share,self.root,"dot-root","WIN","writer","peer-s",self.call)
        env = {"from":"WIN","to":"LAP","envelope_version":2,"sender_endpoint":sender,"recipient_identity":target,
               "subject":"Topic","text":"body"}
        for ident in ("before","after","archived"):
            messages._deliver(self.root,{**env,"id":ident})
        def recall(ident):
            q = mail_lifecycle.apply(self.root,{"id":ident,"op":"recall","actor":sender},
                                     authenticated_peer="WIN", expected_target=target)
            self.assertEqual(q["status"], "recalled")
        recall("before")
        self.inbox = [self.source(mid,i) for i,mid in enumerate(("before","after","archived"),1)]
        for message in self.inbox:
            message["sender_endpoint"] = sender
            message["recipient_identity"] = target
        self.step()
        self.assertFalse((self.share / "in/before.json").exists())
        path = self.share / "in/after.json"
        self.assertEqual(mail.read_request(path)["text"], "body")
        recall("after")
        self.assertEqual(mail.pending(self.state)["pending"], ["archived"])
        self.request("read","late",message_id="after")
        self.request("out","stale",reply_to="after",subject="Ignored", text="Reply")
        messages._receipt(self.root,{"id":"archived","reader_endpoint":target})
        with messages._database(self.root) as db:
            if "archive_digest" not in {c[1] for c in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN archive_digest TEXT")
            db.execute("UPDATE messages SET archive_digest='verified_digest',text='' WHERE id='archived'")
        self.step()
        self.assertEqual(self.result("late")["status"], "recalled")
        self.assertEqual(self.result("stale")["status"], "recalled")
        self.assertFalse(self.sent)
        for name, status in (("after","recalled"), ("archived","archived")):
            marker = mail.read_request(self.share / "in" / (name + ".json"))
            self.assertEqual(marker["status"],status)
            self.assertNotIn("text",marker)
        self.assertEqual(mail.pending(self.state)["pending"],[])

    def isolated_install(self):
        root = self.base / "real-cli"
        root.mkdir()
        repo = Path(__file__).resolve().parents[1]
        (root / "launch.py").write_text(
            "import os,runpy,sys\nfrom pathlib import Path\n"
            "os.environ['XDEV_HOME']=str(Path(__file__).parent)\n"
            f"sys.path.insert(0,{str(repo)!r})\n"
            "runpy.run_module('src.cli.main',run_name='__main__')\n",
            encoding="utf8",
        )
        (root / "config.json").write_text('{"device":"localbox"}', encoding="utf8")
        state, share = self.base / "isolated-state", self.base / "isolated-share"
        mail.init(state, share, root, "room", "localbox", "fixture", "peer-s")
        return root, state, share

    def test_real_control_slot_upgrade_and_daily_status_without_a_new_binding(self):
        from src.runtime import messages

        root, state, share = self.isolated_install()
        (share / "control").rmdir()
        mail.step(state)
        self.assertTrue((share / "control").is_dir())
        actor = {"device":"localbox","client":"fixture","session":"peer-s"}
        target = {"device":"localbox","client":"paired-folder","session":"room"}
        messages._deliver(root,{"id":"letter","from":"localbox","to":"localbox","text":"private source",
            "envelope_version":2,"sender_endpoint":actor,"recipient_identity":target,"communication_intent":"invite"})
        def write(slot, name, **fields):
            mail.put(share / slot / (name + ".json"), {"op_id":name,**fields})
        def read(name):
            return mail.read_request(share / "results" / (name + ".json"))
        write("read","seen",message_id="letter")
        write("control","declared",action="availability",state="busy",ttl=120,reason="Editing a draft")
        write("control","done",action="report",message_id="letter",work_state="completed",reply_intent="no_reply")
        mail.step(state)
        self.assertEqual(read("declared")["status"], "declared")
        self.assertEqual(read("done")["observation"]["work_state"], "completed")
        self.assertEqual(read("done")["observation"]["reply_intent"], "no_reply")
        old = read("declared")
        write("control","prepare",action="prepare")
        write("control","peer",action="preflight")
        write("control","check",action="status",message_id="letter")
        mail.step(state)
        self.assertEqual(read("check")["observation"]["communication_intent"],"invite")
        own = read("prepare")["observation"]["preflight"]
        other = read("peer")["observation"]["preflight"]
        self.assertEqual(own["declaration"]["state"], "busy")
        self.assertNotEqual((other.get("declaration") or {}).get("state"), "busy")
        self.assertNotIn("private source",str(read("check")))
        self.assertEqual(read("declared"),old)

    def test_control_restricts_actor_recipient_arguments_and_ambiguous_replay(self):
        self.inbox = [self.source("enrolled")]
        self.request("control","stranger", action="status",message_id="not-in-this-inbox")
        self.request("control","spoof",action="availability",state="busy",as_session="someone-else")
        self.request("control","change",action="preflight",to="other-device")
        self.request("control","fakecall",action="exec",cmd="arbitrary")
        self.request("control","spoof-author",action="recall",message_id="enrolled")
        self.request("control","uncertain",action="availability",state="maintenance",ttl=15)
        attempts = []
        def mocked(cfg,args):
            if args[0] == "availability":
                attempts.append(args)
                raise subprocess.TimeoutExpired("xdev",1)
            return self.call(cfg,args)
        mail.step(self.state,mocked)
        mail.step(self.state,mocked)
        for name in ("stranger","spoof","change","fakecall","spoof-author"):
            self.assertEqual(self.result(name)["status"], "rejected")
        self.assertEqual(self.result("uncertain")["status"], "unknown")
        self.assertEqual(len(attempts),1)
        self.assertEqual(attempts[0][-4:],["--as-client","paired-folder","--as-session","dot-root"])

    def test_real_local_cli_delivers_to_unbound_folder_and_links_reverse_mail(self):
        # One temporary xdev device hosts both fake participants. No network or live chats.
        root, state, share = self.isolated_install()
        cfg = {"root": str(root)}
        posted = mail.run_cli(
            cfg,
            [
                "send",
                "--transport",
                "direct",
                "--to",
                "localbox",
                "--client",
                "paired-folder",
                "--session",
                "room",
                "--from-client",
                "fixture",
                "--from-session",
                "peer-s",
                "--return-client",
                "fixture",
                "--return-session",
                "peer-s",
                "--id",
                "real-letter",
                "--subject=Short",
                "--text=Hidden",
            ],
        )
        self.assertEqual(posted.get("inbox_status"), "received", str(posted)[:500])
        mail.step(state)
        self.assertEqual(mail.pending(state)["pending"], ["real-letter"])
        mail.put(
            share / "read" / "seen.json", {"op_id": "seen", "message_id": "real-letter"}
        )
        mail.put(
            share / "out" / "answer.json",
            {
                "op_id": "answer",
                "reply_to": "real-letter",
                "subject": "Re",
                "text": "Exact",
            },
        )
        mail.put(
            share / "out" / "independent.json",
            {"op_id": "independent", "subject": "Second", "text": "Independent"},
        )
        mail.step(state)
        self.assertEqual(
            mail.read_request(share / "results" / "seen.json")["status"], "read"
        )
        self.assertEqual(mail.pending(state)["pending"], [])
        for op, parent in (("answer", "real-letter"), ("independent", None)):
            receipt = mail.read_request(share / "results" / (op + ".json"))
            self.assertEqual(receipt["status"], "mailbox_received", str(receipt))
            letter = mail.run_cli(
                cfg,
                [
                    "wait",
                    "--to-client",
                    "fixture",
                    "--to-session",
                    "peer-s",
                    "--id",
                    receipt["message_id"],
                    "--timeout",
                    "0",
                ],
            )["message"]
            self.assertEqual(letter["reply_to"], parent)
            self.assertEqual(letter["sender_endpoint"]["client"], "paired-folder")


if __name__ == "__main__":
    unittest.main()
