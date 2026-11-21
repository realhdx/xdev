"""Notify a locally enrolled chat when its private folder has new unread mail."""

import argparse
from contextlib import closing
from functools import cache
import hashlib
from itertools import groupby
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time


HERE = Path(__file__).resolve().parent
STATUS = "paired-folder-worker-status.json"
MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
TOKEN = re.compile(r"[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_MANIFEST_IDS = 128


def enrollment(state, root):
    # Only the private enrollment database chooses either endpoint.
    with closing(sqlite3.connect((state / "mail.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT config FROM settings WHERE id=1").fetchone()
    if not row:
        raise ValueError("missing_enrollment")
    config = json.loads(row[0])
    if Path(config["root"]).resolve(strict=True) != root:
        raise ValueError("installation_mismatch")
    return config


def route_identity(state, root):
    return address_key(enrollment(state, root))


def address_key(config):
    identity = [config[key] for key in ("local_device", "local_session", "peer_device", "peer_client", "peer_session")]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def invoke(script, arguments):
    # This module only invokes the two fixed local adapters installed beside it.
    command = [sys.executable, "-I", "-B", "-X", "utf8", str(HERE / script), *map(str, arguments)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=180,
                            shell=False, creationflags=flags)
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("invalid_adapter_output")
    if result.returncode and not (script == "github-pr-notice.py" and data.get("status") == "not_submitted"):
        raise ValueError("adapter_failed")
    return data


def journal(state):
    db = sqlite3.connect(state / "paired-folder-notices.sqlite", timeout=8)
    db.execute("""CREATE TABLE IF NOT EXISTS notices(
               route TEXT NOT NULL, id TEXT NOT NULL, token TEXT NOT NULL, status TEXT NOT NULL,
               attempts INTEGER NOT NULL, due REAL NOT NULL DEFAULT 0,
               PRIMARY KEY(route,id))""")
    return db


@cache
def adapters():
    return (runpy.run_path(str(HERE / "paired-folder-mail.py")), runpy.run_path(str(HERE / "github-pr-notice.py")))


def regular_file(path, safe_path):
    safe_path(path)
    attrs = path.lstat()
    if not stat.S_ISREG(attrs.st_mode) or attrs.st_nlink != 1:
        raise ValueError("expected_unlinked_regular_file")
    return attrs


def write_manifest(folder, token, obj, host):
    signals = folder / "signals"
    signals.mkdir(mode=0o700, exist_ok=True)
    host["safe_path"](signals)
    dest = signals / (token + ".json")
    if os.path.lexists(dest):
        info = regular_file(dest, host["safe_path"])
        if info.st_size <= host["MAX_BYTES"]:
            try:
                if host["read_request"](dest) == obj:
                    return 0
            except ValueError:
                regular_file(dest, host["safe_path"])  # Refuse a link or path change during the read.
    host["put"](dest, obj)
    return 1


def recover_signals(state, root, route, token=None, *, lookup_parents=False, call=invoke):
    """Restore logical notification proofs from the two enrolled local ledgers."""
    provider_path = root / "github-pr-notices.sqlite3"
    ledger = state / "paired-folder-notices.sqlite"
    if not os.path.lexists(provider_path) or not os.path.lexists(ledger):
        return 0
    host, provider = adapters()
    safe_path = host["safe_path"]
    for path in (provider_path, ledger, state / "mail.sqlite", root / "config.json"):
        regular_file(path, safe_path)
    cfg = enrollment(state, root)
    if address_key(cfg) != route:
        raise ValueError("enrollment_changed")
    folder = safe_path(Path(cfg["folder"]))
    if not folder.is_dir() or any(folder.is_relative_to(p) or p.is_relative_to(folder) for p in (state, root)):
        raise ValueError("invalid_shared_folder")
    config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    pr, box = config.get("github_pr_notice"), config.get("github_mailbox")
    if not isinstance(pr, dict) or pr.get("enabled") is not True:
        return 0
    provider["validate"](pr, box)
    scope = provider["scope_key"](pr)
    with (closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as local,
          closing(sqlite3.connect(provider_path.as_uri() + "?mode=ro", uri=True)) as published):
        fields = {r[1] for r in published.execute("PRAGMA table_info(notices)")}
        if not fields:
            return 0
        proof_sql = "SELECT sha," + ("parent_sha" if "parent_sha" in fields else "NULL") + (
            " FROM notices WHERE route=? AND notice=? AND state='published'")
        query = "SELECT token,id FROM notices WHERE route=? AND status='accepted'"
        args = [route]
        if token is not None:
            query += " AND token=?"
            args.append(token)
        cursor = local.execute(query + " ORDER BY token,rowid", args)
        count = 0
        for notice, rows in groupby(cursor, key=lambda r: r[0]):
            if not isinstance(notice, str) or not TOKEN.fullmatch(notice):
                continue
            record = published.execute(proof_sql, (scope, notice)).fetchone()
            if not record or not isinstance(record[0], str) or not SHA.fullmatch(record[0]):
                continue
            ids = []
            for _, mid in rows:
                if len(ids) == MAX_MANIFEST_IDS or not isinstance(mid, str) or not MESSAGE_ID.fullmatch(mid):
                    raise ValueError("invalid_manifest_message_ids")
                ids.append(mid)
            sha, parent = record
            if not parent and lookup_parents:
                try:
                    proof = call("github-pr-notice.py", ["proof", "--root", root, "--notice-id", notice])
                    if proof.get("status") == "accepted" and proof.get("commit_sha") == sha:
                        parent = proof.get("parent_sha")
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass  # The published commit itself still has both local records.
            obj = {"schema": "xdev.paired-folder.notice.v1", "commit_sha": sha, "message_ids": ids}
            if isinstance(parent, str) and SHA.fullmatch(parent) and parent != sha:
                obj["parent_sha"] = parent
            count += write_manifest(folder, notice, obj, host)
        return count


def claim(db, route, ids, now):
    with db:
        db.execute("BEGIN IMMEDIATE")
        fresh = []
        for mid in dict.fromkeys(ids):
            if not isinstance(mid, str) or not MESSAGE_ID.fullmatch(mid):
                raise ValueError("invalid_local_message_id")
            row = db.execute("SELECT status,attempts,due FROM notices WHERE route=? AND id=?", (route, mid)).fetchone()
            if row is None or (row[0] == "not_submitted" and row[2] <= now):
                fresh.append((mid, 1 if row is None else row[1] + 1))
                if len(fresh) == MAX_MANIFEST_IDS:
                    break
        if not fresh:
            return None
        token = hashlib.sha256(f"xdev-folder-notice-v1/{route}/{fresh[0][0]}".encode("ascii")).hexdigest()
        # A crash after this commit may have submitted a wake. Preserve ambiguity.
        db.executemany("INSERT OR REPLACE INTO notices VALUES(?,?,?,'unknown',?,0)",
                       [(route, mid, token, attempts) for mid, attempts in fresh])
        return token, fresh


def cycle(state, root, route, call=invoke):
    call("paired-folder-mail.py", ["step", "--state", state])
    after = 0
    ids = []
    while True:
        args = ["pending", "--state", state]
        if after:
            args.extend(["--after", after])
        page = call("paired-folder-mail.py", args)
        if not isinstance(page.get("pending"), list):
            raise ValueError("invalid_pending_page")
        ids.extend(page["pending"])
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if type(cursor) is not int or cursor <= after:
            raise ValueError("invalid_local_cursor")
        after = cursor
    with closing(journal(state)) as db:
        batch = claim(db, route, ids, time.time())
        outcome = "no_new_mail"
        if batch:
            token, fresh = batch
            try:
                reply = call("github-pr-notice.py", ["notify", "--root", root, "--notice-id", token])
                outcome = reply.get("status")
            except (OSError, ValueError, subprocess.SubprocessError):
                outcome = "unknown"
            if outcome not in ("accepted", "not_submitted", "unknown"):
                outcome = "unknown"
            now = time.time()
            with db:
                db.executemany("UPDATE notices SET status=?,due=? WHERE route=? AND id=? AND token=?",
                               [(outcome, now + min(300, 10 * 2 ** min(n - 1, 5)) if outcome == "not_submitted" else 0,
                                 route, mid, token) for mid, n in fresh])
            if outcome == "accepted":
                recover_signals(state, root, route, token)
        counts = dict(db.execute("SELECT status,COUNT(*) FROM notices WHERE route=? GROUP BY status", (route,)))
    return {"pending_count": len(ids), "last_notification": outcome,
            "accepted_count": counts.get("accepted", 0), "unknown_count": counts.get("unknown", 0),
            "retry_count": counts.get("not_submitted", 0)}


def write_heartbeat(path, values):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as staged:
        json.dump(values, staged)
    try:
        for attempt in range(4):
            try:
                os.replace(staged.name, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(.05)
    finally:
        Path(staged.name).unlink(missing_ok=True)


def follow(state, root, route):
    # Lifetime lock also prevents two heartbeat writers competing across restarts.
    with closing(sqlite3.connect(state / "paired-folder-worker.lock.sqlite", timeout=0)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        status = {"pid": os.getpid(), "startup_id": secrets.token_hex(16), "started_at": time.time(),
                  "cycle_ok": False, "phase": "starting"}
        guard = threading.Lock()
        stop = threading.Event()

        def update(**changes):
            with guard:
                status.update(changes, heartbeat_at=time.time())
                write_heartbeat(state / STATUS, status)

        def heartbeat():
            while not stop.wait(10):
                update()

        update()
        pulse = threading.Thread(target=heartbeat, name="xdev-folder-heartbeat", daemon=True)
        pulse.start()
        errors = 0
        recover_at = 0
        try:
            while not stop.is_set():
                try:
                    if time.monotonic() >= recover_at:
                        recover_signals(state, root, route)
                        recover_at = time.monotonic() + 300
                    result = cycle(state, root, route)
                    errors = 0
                    update(phase="running", cycle_ok=True, last_cycle_at=time.time(), error=None, **result)
                except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
                    errors = min(errors + 1, 6)
                    update(phase="retrying", cycle_ok=False, last_cycle_at=time.time(), error=type(exc).__name__)
                stop.wait(min(120, 4 * 2 ** errors))
        finally:
            stop.set()
            pulse.join()
            update(phase="stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--follow", action="store_true")
    mode.add_argument("--recover-signals", action="store_true", help="Repair local notification manifests without sending mail or notifications")
    parser.add_argument("--lookup-missing-parents", action="store_true", help="With --recover-signals, look up the exact confirmed Git commit parent using read-only provider requests")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--xdev-root", required=True, type=Path)
    args = parser.parse_args()
    if not args.state.is_absolute() or not args.xdev_root.is_absolute():
        parser.error("supply absolute state and installation paths")
    state = args.state.resolve(strict=True)
    root = args.xdev_root.resolve(strict=True)
    route = route_identity(state, root)
    if args.lookup_missing_parents and not args.recover_signals:
        parser.error("--lookup-missing-parents requires --recover-signals")
    if args.recover_signals:
        n = recover_signals(state, root, route, lookup_parents=args.lookup_missing_parents)
        print(json.dumps({"signal_files_written": n}))
    elif args.once:
        print(json.dumps(cycle(state, root, route)))
    else:
        follow(state, root, route)


if __name__ == "__main__":
    main()
