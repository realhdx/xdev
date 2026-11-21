"""Notify an opted-in private mailbox PR by advancing only its dedicated branch.

prepare/check issue only GET requests. An independently verified PR binding is
required because the mailbox credential itself need only have Contents access.
"""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sqlite3
import sys
import time


mail = runpy.run_path(str(Path(__file__).with_name("github-mail.py")))
SetupError = mail["SetupError"]
BRANCH = "xdev-mailbox-notifications"
SIGNAL = "xdev: mailbox updated"
SHA = re.compile(r"[0-9a-f]{40}")
NOTICE = re.compile(r"[0-9a-f]{64}")
LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}")


def checked_path(path, directory=False):
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise SetupError("Use an absolute local path under the current user's control.")
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise SetupError("Linked paths are not supported for notification state.")
    if not (path.is_dir() if directory else path.is_file()):
        raise SetupError("Notification configuration or state has the wrong type.")
    if os.name == "nt":
        mail["windows_access"](path, "directory" if directory else "file")
    elif path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise SetupError("Notification configuration and state must be writable only by their owner.")


def validate(route, box):
    if not isinstance(route, dict) or route.get("enabled") is not True:
        raise SetupError("No enabled private PR notification route.")
    if route.get("branch") != BRANCH or not isinstance(box, dict):
        raise SetupError("Configure the dedicated mailbox notification branch.")
    for key in ("repository_id", "pr_number", "pr_issue_id", "actor_id"):
        mail["positive"](route.get(key))
    repo = box.get("repository", "")
    if (not isinstance(repo, str) or not mail["REPO"].fullmatch(repo) or repo.split("/")[1] in {".", ".."}
            or mail["positive"](box.get("repository_id")) != route["repository_id"]
            or not isinstance(route.get("actor"), str) or not LOGIN.fullmatch(route["actor"])
            or any(not isinstance(route.get(k), str) or not SHA.fullmatch(route[k]) for k in ("anchor", "tree"))):
        raise SetupError("Invalid or mismatched mailbox, actor or branch identity.")


def live(api, box, route):
    validate(route, box)
    user = mail["fetch"](api, "/user")
    if (user.get("type") != "User" or user.get("id") != route["actor_id"]
            or str(user.get("login", "")).lower() != route["actor"].lower()):
        raise SetupError("The signed-in GitHub account does not match the enrolled actor.")
    base = "/repos/" + box["repository"]
    repo = mail["fetch"](api, base)
    mail["repo_record"](repo, box["repository"], route["repository_id"])
    if not repo.get("default_branch") or repo["default_branch"] == BRANCH:
        raise SetupError("The dedicated branch must be different from the default branch.")
    ref = mail["fetch"](api, base + "/git/ref/heads/" + BRANCH)
    tip = ref.get("object", {}).get("sha", "")
    if (ref.get("ref") != "refs/heads/" + BRANCH or ref.get("object", {}).get("type") != "commit"
            or not SHA.fullmatch(str(tip))):
        raise SetupError("The notification branch ref did not match.")
    commit = mail["fetch"](api, base + "/git/commits/" + tip)
    if (commit.get("sha") != tip or commit.get("tree", {}).get("sha") != route["tree"]
            or (tip != route["anchor"] and commit.get("message") != SIGNAL)):
        raise SetupError("The notification branch has unexpected content.")
    return base, tip


def prepare(api, box, repository_id, pr_number, pr_issue_id, actor, anchor):
    # The operator obtains these branch/PR coordinates from the approved PR.
    if (not isinstance(box, dict) or box.get("repository_id") != repository_id
            or not SHA.fullmatch(anchor) or not LOGIN.fullmatch(actor)):
        raise SetupError("Use the independently verified PR, repository and head commit.")
    repo = box.get("repository", "")
    if not isinstance(repo, str) or not mail["REPO"].fullmatch(repo) or repo.split("/")[1] in {".", ".."}:
        raise SetupError("Invalid repository name.")
    uid = mail["fetch"](api, "/user").get("id")
    commit = mail["fetch"](api, "/repos/" + repo + "/git/commits/" + anchor)
    route = dict(enabled=True, repository_id=repository_id, pr_number=pr_number, pr_issue_id=pr_issue_id,
                 branch=BRANCH, anchor=anchor, tree=commit.get("tree", {}).get("sha"), actor=actor, actor_id=uid)
    _, tip = live(api, box, route)
    if tip != anchor:
        raise SetupError("Initial branch ref differs from the independently verified PR head.")
    return {"github_pr_notice": route, "write_tested": False}


def database_path(root, filename):
    path = root / filename
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    checked_path(path)
    return path


@contextmanager
def journal(root):
    # Keep the process mutex in a separate database so a durable reservation
    # can commit before the network call. OS releases the mutex after a crash.
    with closing(sqlite3.connect(database_path(root, "github-pr-notice-lock.sqlite3"), timeout=15)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        with closing(sqlite3.connect(database_path(root, "github-pr-notices.sqlite3"), isolation_level=None)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS routes(route TEXT PRIMARY KEY, tip TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS notices(route TEXT, notice TEXT, started INTEGER NOT NULL,
                         state TEXT NOT NULL, sha TEXT, parent_sha TEXT, PRIMARY KEY(route,notice))""")
            if "parent_sha" not in {row[1] for row in db.execute("PRAGMA table_info(notices)")}:
                db.execute("ALTER TABLE notices ADD COLUMN parent_sha TEXT")
            yield db


def scope_key(route):
    return hashlib.sha256(json.dumps([route[k] for k in ("repository_id", "pr_number", "pr_issue_id",
                          "branch", "anchor", "tree", "actor_id")]).encode()).hexdigest()


def notify(api, root, box, route, notice):
    if not isinstance(notice, str) or not NOTICE.fullmatch(notice):
        raise SetupError("Expected a locally generated 64-hex notice ID.")
    validate(route, box)
    scope = scope_key(route)
    checked_path(root, directory=True)
    with journal(root) as db:
        existing = db.execute("SELECT state FROM notices WHERE route=? AND notice=?", (scope, notice)).fetchone()
        if existing and existing[0] == "published":
            return {"status": "accepted", "reason": "previously_confirmed"}
        if existing and existing[0] in ("creating", "rejected"):
            # A ref PATCH is impossible in this phase. A detached Git object
            # may exist; a duplicate invocation must never create another one.
            return {"status": "not_submitted", "reason": "previous_reservation_closed"}
        outstanding = db.execute("SELECT notice, sha FROM notices WHERE route=? AND state='updating'",
                                 (scope,)).fetchone()
        try:
            base, tip = live(api, box, route)
        except (SetupError, OSError):
            if outstanding and outstanding[0] == notice:
                return {"status": "unknown", "reason": "cannot_reconcile"}
            return {"status": "not_submitted", "reason": "preflight_failed"}
        db.execute("INSERT OR IGNORE INTO routes VALUES (?,?)", (scope, route["anchor"]))
        if outstanding:
            previous, sha = outstanding
            if tip != sha:
                status = "unknown" if previous == notice else "not_submitted"
                return {"status": status, "reason": "unconfirmed_ref_update"}
            settled(db, scope, previous, sha)
            if previous == notice:
                return {"status": "accepted", "reason": "confirmed_by_ref"}
        expected = db.execute("SELECT tip FROM routes WHERE route=?", (scope,)).fetchone()[0]
        if tip != expected:
            return {"status": "not_submitted", "reason": "unexpected_branch_head"}
        db.execute("INSERT INTO notices(route,notice,started,state) VALUES (?,?,?,'creating')",
                   (scope, notice, time.time_ns()))
        # GitHub assigns the real authenticated author's identity and timestamp.
        payload = {"message": SIGNAL, "parents": [tip], "tree": route["tree"]}
        try:
            status, obj = api.request("POST", base + "/git/commits", payload)
        except (OSError, SetupError):
            return {"status": "not_submitted", "reason": "commit_unconfirmed"}
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if (status != 201 or not isinstance(sha, str) or not SHA.fullmatch(sha)
                or commit_parent(obj, sha, route["tree"]) != tip):
            return {"status": "not_submitted", "reason": "commit_unconfirmed"}
        db.execute("UPDATE notices SET state='updating', sha=?, parent_sha=? WHERE route=? AND notice=?",
                   (sha, tip, scope, notice))
        try:
            status, result = api.request("PATCH", base + "/git/refs/heads/" + BRANCH, {"sha": sha, "force": False})
        except (OSError, SetupError):
            return {"status": "unknown", "reason": "unconfirmed_ref_update"}
        if (status == 200 and isinstance(result, dict) and result.get("ref") == "refs/heads/" + BRANCH
                and result.get("object", {}).get("sha") == sha):
            settled(db, scope, notice, sha)
            return {"status": "accepted", "reason": "ref_updated"}
        if status in (401, 403, 404, 422):
            db.execute("UPDATE notices SET state='rejected' WHERE route=? AND notice=?", (scope, notice))
            return {"status": "not_submitted", "reason": f"ref_rejected_http_{status}"}
        return {"status": "unknown", "reason": "unconfirmed_ref_update"}


def settled(db, scope, notice, sha):
    db.execute("BEGIN IMMEDIATE")
    with db:
        db.execute("UPDATE routes SET tip=? WHERE route=?", (sha, scope))
        db.execute("UPDATE notices SET state='published' WHERE route=? AND notice=?", (scope, notice))


def commit_parent(commit, sha, tree_sha):
    if not isinstance(commit, dict) or commit.get("sha") != sha or commit.get("message") != SIGNAL:
        return None
    parents, tree = commit.get("parents"), commit.get("tree")
    if (not isinstance(tree, dict) or tree.get("sha") != tree_sha or not isinstance(parents, list)
            or len(parents) != 1 or not isinstance(parents[0], dict)):
        return None
    parent = parents[0].get("sha")
    return parent if isinstance(parent, str) and SHA.fullmatch(parent) and parent != sha else None


def proof(api, root, box, route, notice):
    """Return the exact published commit and its verified sole parent. GitHub access is GET only."""
    if not isinstance(notice, str) or not NOTICE.fullmatch(notice):
        raise SetupError("Expected a locally generated 64-hex notice ID.")
    validate(route, box)
    scope = scope_key(route)
    checked_path(root, directory=True)
    with journal(root) as db:
        row = db.execute("SELECT sha,parent_sha FROM notices WHERE route=? AND notice=? AND state='published'",
                         (scope, notice)).fetchone()
        if not row or not isinstance(row[0], str) or not SHA.fullmatch(row[0]):
            return {"status": "unknown", "reason": "published_notice_not_found"}
        sha, parent = row
        if parent is None:
            # Older versions did not persist the parent. Look up the immutable
            # object by its own SHA; the current branch tip can be several notices ahead.
            try:
                if api is None:
                    api = mail["GitHub"](mail["credential"](box["token_file"]))
                base = "/repos/" + box["repository"]
                mail["repo_record"](mail["fetch"](api, base), box["repository"], route["repository_id"])
                commit = mail["fetch"](api, base + "/git/commits/" + sha)
            except (SetupError, OSError):
                return {"status": "unknown", "reason": "commit_proof_unavailable"}
            parent = commit_parent(commit, sha, route["tree"])
            if parent is None:
                return {"status": "unknown", "reason": "commit_proof_mismatch"}
            db.execute("UPDATE notices SET parent_sha=? WHERE route=? AND notice=? AND sha=? "
                       "AND state='published' AND parent_sha IS NULL", (parent, scope, notice, sha))
        elif not isinstance(parent, str) or not SHA.fullmatch(parent) or parent == sha:
            return {"status": "unknown", "reason": "invalid_stored_parent"}
        return {"status": "accepted", "commit_sha": sha, "parent_sha": parent}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("prepare", "check", "notify", "proof"):
        sub = actions.add_parser(name)
        sub.add_argument("--root", required=True, type=Path)
        if name == "prepare":
            for arg in ("repository-id", "pr-number", "pr-issue-id"):
                sub.add_argument("--" + arg, type=int, required=True)
            sub.add_argument("--actor", required=True)
            sub.add_argument("--anchor", required=True)
        if name in ("notify", "proof"):
            sub.add_argument("--notice-id", required=True)
    args = parser.parse_args()
    mutation_possible = False
    try:
        checked_path(args.root, directory=True)
        cfg = args.root / "config.json"
        checked_path(cfg)
        conf = json.loads(cfg.read_text(encoding="utf-8-sig"))
        box = conf["github_mailbox"]
        api = None if args.action == "proof" else mail["GitHub"](mail["credential"](box["token_file"]))
        if args.action == "prepare":
            result = prepare(api, box, args.repository_id, args.pr_number, args.pr_issue_id, args.actor, args.anchor)
        elif args.action == "check":
            _, tip = live(api, box, conf.get("github_pr_notice"))
            result = {"status": "ready", "branch_tip": tip, "write_tested": False}
        elif args.action == "proof":
            result = proof(api, args.root, box, conf.get("github_pr_notice"), args.notice_id)
        else:
            mutation_possible = True
            result = notify(api, args.root, box, conf.get("github_pr_notice"), args.notice_id)
    except (KeyError, TypeError, ValueError, SetupError, OSError, sqlite3.Error) as exc:
        detail = str(exc) if isinstance(exc, SetupError) else type(exc).__name__
        result = {"status": "unknown" if mutation_possible else "not_submitted", "reason": detail}
    print(json.dumps(result))
    if args.action in ("notify", "proof"):  # The host protocol includes negative acknowledgments.
        return 0
    return 0 if result.get("status") == "ready" or "github_pr_notice" in result else 1


if __name__ == "__main__":
    sys.exit(main())
