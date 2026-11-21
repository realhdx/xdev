from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("pr_notice", Path(__file__).with_name("github-pr-notice.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
ANCHOR = "1" * 40
TREE = "2" * 40
BOX = {"repository": "example/mail", "repository_id": 12}
ROUTE = dict(enabled=True, repository_id=12, actor="owner", actor_id=22, pr_number=1, pr_issue_id=34,
             anchor=ANCHOR, tree=TREE, branch=mod.BRANCH)


class PrivateApi:
    def __init__(self):
        self.ref = "refs/heads/" + mod.BRANCH
        self.tip = ANCHOR
        self.repo = {"id": 12, "full_name": BOX["repository"], "private": True, "visibility": "private",
                     "default_branch": "main", "description": mod.mail["DESCRIPTION"]}
        self.actor = {"id": 22, "login": "owner", "type": "User"}
        self.commits = {ANCHOR: {"sha": ANCHOR, "tree": {"sha": TREE}, "message": "create notification PR"}}
        self.writes = []
        self.reads = []
        self.patch_error = None

    def request(self, method, path, payload=None):
        base = "/repos/example/mail"
        if method == "GET":
            self.reads.append(path)
            if path == "/user":
                return 200, self.actor
            if path == base:
                return 200, self.repo
            if path == base + "/git/ref/heads/" + mod.BRANCH:
                return 200, {"ref": self.ref, "object": {"type": "commit", "sha": self.tip}}
            return 200, self.commits[path.removeprefix(base + "/git/commits/")]
        self.writes.append((method, path, copy.deepcopy(payload)))
        if method == "POST" and path == base + "/git/commits":
            sha = hashlib.sha1(payload["parents"][0].encode()).hexdigest()
            doc = {"sha": sha, "tree": {"sha": payload["tree"]}, "message": payload["message"],
                   "parents": [{"sha": p} for p in payload["parents"]]}
            self.commits[sha] = doc
            return 201, doc
        if method != "PATCH" or path != base + "/git/refs/heads/" + mod.BRANCH or payload.get("force") is not False:
            raise AssertionError("unexpected remote write")
        if self.patch_error == "rejected":
            return 403, None
        if self.patch_error != "before_response":
            self.tip = payload["sha"]
        if self.patch_error:
            raise OSError("response unavailable")
        return 200, {"ref": self.ref, "object": {"type": "commit", "sha": self.tip}}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.api = PrivateApi()
        self.route = dict(ROUTE)
        self.id = "a" * 64
        # The permission helper has its own tests; use regular temp files here.
        acl = patch.dict(mod.mail, {"windows_access": lambda path, kind: None})
        acl.start()
        self.addCleanup(acl.stop)

    def notify(self, id=None):
        return mod.notify(self.api, self.root, BOX, self.route, id or self.id)

    def proof(self, notice=None):
        return mod.proof(self.api, self.root, BOX, self.route, notice or self.id)

    def test_proof_for_published_notice_survives_newer_commits_and_restart(self):
        self.notify()
        first = self.api.tip
        next_id = "b" * 64
        self.notify(next_id)
        self.api.reads.clear()
        writes_before = list(self.api.writes)
        self.assertEqual(self.proof(), {"status": "accepted", "commit_sha": first, "parent_sha": ANCHOR})
        self.assertEqual(self.proof(next_id), {"status": "accepted", "commit_sha": self.api.tip, "parent_sha": first})
        self.assertEqual(self.api.reads, [])
        self.assertEqual(self.api.writes, writes_before)

    def test_legacy_migration_verifies_exact_commit_and_caches_parent(self):
        old = "3" * 40
        self.api.tip = "9" * 40
        self.api.commits[old] = {"sha": old, "parents": [{"sha": ANCHOR}], "tree": {"sha": TREE}, "message": mod.SIGNAL}
        with closing(sqlite3.connect(self.root/"github-pr-notices.sqlite3")) as db, db:
            db.execute("CREATE TABLE notices(route TEXT,notice TEXT,started INTEGER NOT NULL,state TEXT NOT NULL,"
                       "sha TEXT,PRIMARY KEY(route,notice))")
            db.execute("INSERT INTO notices VALUES (?,?,0,'published',?)", (mod.scope_key(self.route),self.id,old))
        answer = self.proof()
        self.assertEqual(answer, {"status":"accepted","commit_sha":old,"parent_sha":ANCHOR})
        self.assertEqual(self.api.reads, ["/repos/example/mail","/repos/example/mail/git/commits/"+old])
        self.api.reads.clear()
        self.assertEqual(self.proof(),answer)
        self.assertEqual(self.api.reads, [])
        self.assertEqual(self.api.writes, [])

    def test_uncertain_or_unknown_notice_does_not_read_commit_or_publish(self):
        self.api.patch_error = "before_response"
        self.assertEqual(self.notify()["status"],"unknown")
        writes_before = list(self.api.writes)
        self.api.reads.clear()
        for n in (self.id, "f"*64):
            self.assertEqual(self.proof(n),{"status":"unknown","reason":"published_notice_not_found"})
        self.assertEqual(self.api.reads, [])
        self.assertEqual(self.api.writes, writes_before)

    def test_legacy_proof_checks_sha_tree_message_and_single_parent_before_updating(self):
        self.notify()
        sha = self.api.tip
        with mod.journal(self.root) as db:
            db.execute("UPDATE notices SET parent_sha=NULL")
        writes_before = list(self.api.writes)
        with patch.object(self.api, "request", side_effect=OSError("offline")):
            self.assertEqual(self.proof(),{"status":"unknown","reason":"commit_proof_unavailable"})
        for changed in ({"sha":"4"*40},{"tree":{}},{"message":"changed"},{"parents":[]},
                        {"parents":[{"sha":ANCHOR},{"sha":ANCHOR}]},{"parents":["invalid"]},
                        {"parents":[{"sha":"invalid"}]},{"parents":[{"sha":sha}]}):
            with self.subTest(change=changed), patch.dict(self.api.commits[sha],changed):
                self.assertEqual(self.proof(),{"status":"unknown","reason":"commit_proof_mismatch"})
        self.assertEqual(self.proof()["parent_sha"],ANCHOR)
        self.assertEqual(self.api.writes,writes_before)

    def test_prepare_returns_only_pinned_route_and_is_readonly(self):
        r = mod.prepare(self.api, BOX, 12, 1, 34, "owner", ANCHOR)
        self.assertEqual(r, {"github_pr_notice": self.route, "write_tested": False})
        self.assertEqual(self.api.writes, [])

    def test_only_fixed_message_same_tree_single_parent_and_fast_forward_ref(self):
        self.assertEqual(self.notify()["status"], "accepted")
        create, update = self.api.writes
        self.assertEqual(create, ("POST", "/repos/example/mail/git/commits",
                                 {"message": mod.SIGNAL, "parents": [ANCHOR], "tree": TREE}))
        self.assertEqual(update, ("PATCH", "/repos/example/mail/git/refs/heads/" + mod.BRANCH,
                                 {"sha": self.api.tip, "force": False}))
        self.assertEqual(self.notify()["reason"], "previously_confirmed")
        self.assertEqual(len(self.api.writes), 2)

    def test_distinct_notices_serialized_same_notice_delivered_once(self):
        other = "b" * 64
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(self.notify, [self.id, self.id, other]))
        self.assertTrue(all(r["status"] == "accepted" for r in results))
        self.assertEqual([x[0] for x in self.api.writes], ["POST", "PATCH", "POST", "PATCH"])
        self.assertEqual(self.api.writes[2][2]["parents"], [self.api.writes[1][2]["sha"]])

    def test_lost_response_after_success_reconciles_with_no_write(self):
        self.api.patch_error = "after_success"
        self.assertEqual(self.notify()["status"], "unknown")
        self.assertEqual(self.notify()["reason"], "confirmed_by_ref")
        self.assertEqual(len(self.api.writes), 2)

    def test_unconfirmed_patch_blocks_replay_and_later_ref_writes(self):
        self.api.patch_error = "before_response"
        self.assertEqual(self.notify()["status"], "unknown")
        self.assertEqual(self.notify()["status"], "unknown")
        self.assertEqual(self.notify("b" * 64)["status"], "not_submitted")
        self.assertEqual(len(self.api.writes), 2)

    def test_rejected_patch_cannot_replay_with_same_id(self):
        self.api.patch_error = "rejected"
        self.assertEqual(self.notify()["status"], "not_submitted")
        self.assertEqual(self.notify()["status"], "not_submitted")
        self.assertEqual(len(self.api.writes), 2)

    def test_private_actor_root_grant_and_fixed_branch_checks(self):
        for changes in ({"enabled": False}, {"repository_id": 35}, {"branch": "main"},
                        {"actor_id": 99}, {"actor": "somebody"}):
            with self.subTest(changes=changes):
                self.route = dict(ROUTE, **changes)
                try:
                    self.assertEqual(self.notify()["status"], "not_submitted")
                except mod.SetupError:
                    pass
        self.route = dict(ROUTE)
        for changes in ({"private": False}, {"id": 55}, {"default_branch": mod.BRANCH}):
            with self.subTest(changes=changes), patch.dict(self.api.repo, changes):
                self.assertEqual(self.notify()["status"], "not_submitted")
        self.assertEqual(self.api.writes, [])

    def test_modified_tree_or_untracked_head_blocks_new_writes(self):
        self.api.commits[ANCHOR]["tree"]["sha"] = "4" * 40
        self.assertEqual(self.notify()["status"], "not_submitted")
        other = "3" * 40
        self.api.tip = other
        self.api.commits[other] = {"sha": other, "message": mod.SIGNAL, "tree": {"sha": TREE}}
        self.assertEqual(self.notify()["reason"], "unexpected_branch_head")
        self.assertEqual(self.api.writes, [])

    def test_arbitrary_payload_cannot_be_a_notice(self):
        with self.assertRaises(mod.SetupError):
            self.notify("a" * 64 + "\nprivate content")
        self.assertEqual(self.api.writes, [])


if __name__ == "__main__":
    unittest.main()
