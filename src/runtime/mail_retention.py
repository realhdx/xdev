"""Explicit, local retention for material in a device's xdev inbox."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from src.runtime import messages, transport
from src.runtime.subjects import preview_subject


FORMAT = "xdev.local-mail-archive.v1"
MAX_BLOB = 3_000_000


def material(text, attachment):
    return json.dumps(
        [text, attachment], ensure_ascii=False, separators=(",", ":")
    ).encode("utf8")


def content_digest(text, attachment):
    return hashlib.sha256(material(text, attachment)).hexdigest()


def _schema(db):
    fields = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    for name in ("archive_digest", "archive_summary"):
        if name not in fields:
            db.execute("ALTER TABLE messages ADD COLUMN " + name + " TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS retention_holds(id TEXT PRIMARY KEY, held_at TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS retention_processed(id TEXT PRIMARY KEY)")


def _checked(path):
    for p in (path, *path.parents):
        if p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Choose a local archive path without links")


def _completed(tables):
    sources = []
    if "retention_processed" in tables:
        sources.append("SELECT id FROM retention_processed")
    if "mail_observations" in tables:
        sources.append("SELECT id FROM mail_observations WHERE work='completed'")
    return " UNION ".join(sources) or "SELECT NULL WHERE 0"


@contextmanager
def archive_file(path, *, create):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise ValueError("Choose an absolute local archive file")
    _checked(path.parent)
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    _checked(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Expected a regular private archive file")
    uri = path.as_uri() + ("?mode=rw" if create else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=15)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA synchronous=FULL")
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables and create:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS archive_format(schema TEXT PRIMARY KEY)"
                )
                db.execute("INSERT OR IGNORE INTO archive_format VALUES(?)", (FORMAT,))
                db.execute(
                    "CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, content BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS letters(source TEXT,id TEXT,hash TEXT NOT NULL,metadata TEXT NOT NULL,checksum TEXT NOT NULL,"
                    "PRIMARY KEY(source,id))"
                )
        if "archive_format" not in tables and tables:
            raise ValueError("Selected file is not an xdev mail archive")
        if db.execute("SELECT schema FROM archive_format").fetchall() != [(FORMAT,)]:
            raise ValueError("Unrecognized archive format")
        yield db


def _blob(db, digest):
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Invalid archived content digest")
    row = db.execute(
        "SELECT content FROM blobs WHERE hash=? AND length(content)<=?",
        (digest, MAX_BLOB),
    ).fetchone()
    if (
        not row
        or not isinstance(row[0], bytes)
        or not hmac.compare_digest(hashlib.sha256(row[0]).hexdigest(), digest)
    ):
        raise ValueError("Archived material missing or failed its SHA-256 check")
    values = json.loads(row[0])
    if (
        not isinstance(values, list)
        or len(values) != 2
        or not isinstance(values[0], str)
        or not (values[1] is None or isinstance(values[1], str))
    ):
        raise ValueError("Invalid archived material")
    return values


def _metadata_checksum(source, identifier, digest, metadata):
    record = json.dumps(
        [source, identifier, digest, metadata],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf8")
    return hashlib.sha256(record).hexdigest()


def _catalog(db, source, identifier):
    row = db.execute(
        "SELECT hash,metadata,checksum FROM letters WHERE source=? AND id=? AND length(metadata)<=?",
        (source, identifier, MAX_BLOB),
    ).fetchone()
    if row is None:
        return None
    digest, meta, checksum = row
    if not isinstance(checksum, str) or not hmac.compare_digest(
        checksum, _metadata_checksum(source, identifier, digest, meta)
    ):
        raise ValueError("Archived letter metadata failed its integrity check")
    return digest, meta


def _same_original(archived, current):
    previous = json.loads(archived)
    return isinstance(previous, dict) and all(
        previous.get(key) == current.get(key)
        for key in (
            "id",
            "sender",
            "recipient",
            "received_at",
            "sender_endpoint",
            "recipient_identity",
            "kind",
            "reply_to",
            "reply_endpoint",
        )
    )


def record(root, identifier, action, package=None, *, processed_only=False):
    """Lock the original row through export verification. Blobs are always committed first."""
    if action not in (
        "export",
        "archive",
        "restore",
        "protect",
        "unprotect",
        "verify",
        "mark-processed",
    ):
        raise ValueError("Unknown retention action")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
        raise ValueError("Expected one xdev message ID")
    root = Path(root)
    if action in ("archive", "export", "restore", "verify"):
        if package is None or Path(package).resolve().is_relative_to(
            (root / "state").resolve()
        ):
            raise ValueError("Choose an archive outside the live state directory")
    actor = transport.node(root)
    with messages._database(root) as db:
        db.execute("BEGIN IMMEDIATE")
        _schema(db)
        row = db.execute("SELECT * FROM messages WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Message not found on this device")
        if action == "protect":
            db.execute(
                "INSERT OR IGNORE INTO retention_holds VALUES(?,?)",
                (identifier, datetime.now(timezone.utc).isoformat()),
            )
            return {"id": identifier, "status": "protected"}
        if action == "unprotect":
            db.execute("DELETE FROM retention_holds WHERE id=?", (identifier,))
            return {"id": identifier, "status": "unprotected"}
        if "withdrawn_at" in row.keys() and row["withdrawn_at"]:
            raise ValueError("This letter was withdrawn")
        if action in ("archive", "mark-processed") and (
            not row["read_at"]
            or not row["read_by_endpoint"]
            and (row["recipient_identity"] or row["recipient_endpoint"])
        ):
            raise ValueError("Unread mail remains in the live inbox")
        if action == "mark-processed":
            db.execute(
                "INSERT OR IGNORE INTO retention_processed VALUES(?)", (identifier,)
            )
            return {"id": identifier, "status": "eligible_for_retention_policy"}
        if (
            action == "archive"
            and db.execute(
                "SELECT 1 FROM retention_holds WHERE id=?", (identifier,)
            ).fetchone()
        ):
            raise ValueError("This letter is protected against pruning")
        if processed_only:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not db.execute(
                "SELECT 1 FROM (" + _completed(tables) + ") WHERE id=?", (identifier,)
            ).fetchone():
                raise ValueError("Processing was not completed for this letter")
        was_archived = row["archive_digest"] is not None
        digest = row["archive_digest"] or content_digest(
            row["text"], row["letter_attachment"]
        )
        with archive_file(package, create=action in ("export", "archive")) as target:
            if action in ("export", "archive") and not was_archived:
                raw = material(row["text"], row["letter_attachment"])
                metadata = {
                    k: row[k]
                    for k in row.keys()
                    if k
                    not in (
                        "seq",
                        "text",
                        "letter_attachment",
                        "archive_digest",
                        "archive_summary",
                    )
                }
                encoded = json.dumps(
                    metadata, ensure_ascii=False, separators=(",", ":")
                )
                with target:
                    target.execute(
                        "INSERT OR IGNORE INTO blobs VALUES(?,?)", (digest, raw)
                    )
                    previous = _catalog(target, actor, identifier)
                    if previous is None:
                        target.execute(
                            "INSERT INTO letters VALUES(?,?,?,?,?)",
                            (
                                actor,
                                identifier,
                                digest,
                                encoded,
                                _metadata_checksum(actor, identifier, digest, encoded),
                            ),
                        )
                    elif not _same_original(previous[1], metadata):
                        raise ValueError(
                            "This archive ID belongs to a different original letter"
                        )
                    elif previous[0] == digest and previous[1] != encoded:
                        target.execute(
                            "UPDATE letters SET metadata=?,checksum=? WHERE source=? AND id=?",
                            (
                                encoded,
                                _metadata_checksum(actor, identifier, digest, encoded),
                                actor,
                                identifier,
                            ),
                        )
            catalog = _catalog(target, actor, identifier)
            if not catalog or catalog[0] != digest:
                raise ValueError(
                    "No matching verified material for this device and message in the selected archive"
                )
            text, attachment = _blob(target, digest)
        if action == "restore" and was_archived:
            db.execute(
                "UPDATE messages SET text=?,letter_attachment=?,archive_digest=NULL,archive_summary=NULL WHERE id=?",
                (text, attachment, identifier),
            )
        if action == "archive" and not was_archived:
            summary = row["subject"] or preview_subject(row["text"])
            db.execute(
                "UPDATE messages SET text='',letter_attachment=NULL,archive_digest=?,archive_summary=? WHERE id=?",
                (digest, summary, identifier),
            )
    return {
        "id": identifier,
        "status": {
            "archive": "archived",
            "export": "exported",
            "restore": "restored",
            "verify": "verified",
        }[action],
        "sha256": digest,
        "material_bytes": len(material(text, attachment)),
        "archive_database_bytes": Path(package).stat().st_size,
    }


def usage(root):
    root = Path(root)
    database = root / "state/messages.sqlite3"
    if not database.exists():
        return {
            "database_bytes": 0,
            "wal_bytes": 0,
            "free_pages_bytes": 0,
            "active_material_bytes": 0,
            "archived": 0,
        }
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
        fields = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        if not fields:
            raise ValueError("Not an xdev inbox")
        cols = ["text"] + (
            ["letter_attachment"] if "letter_attachment" in fields else []
        )
        size_expr = "+".join(
            "COALESCE(length(CAST(" + k + " AS BLOB)),0)" for k in cols
        )
        used = db.execute(
            "SELECT COALESCE(SUM(" + size_expr + "),0) FROM messages"
        ).fetchone()[0]
        archived = (
            db.execute(
                "SELECT COUNT(*) FROM messages WHERE archive_digest IS NOT NULL"
            ).fetchone()[0]
            if "archive_digest" in fields
            else 0
        )
    wal = database.with_name(database.name + "-wal")
    return {
        "database_bytes": database.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "free_pages_bytes": free,
        "active_material_bytes": used,
        "archived": archived,
    }


def compact(root):
    before = usage(root)
    if before["database_bytes"]:
        with messages._database(root) as db:
            db.execute("VACUUM")
    after = usage(root)
    return {
        "before": before,
        "after": after,
        "inbox_files_bytes_change": after["database_bytes"]
        + after["wal_bytes"]
        - before["database_bytes"]
        - before["wal_bytes"],
    }


def extract(package, source, identifier, output):
    """Read a portable archive on another machine without access to the original gateway."""
    with archive_file(package, create=False) as db:
        row = _catalog(db, source, identifier)
        if not row:
            raise ValueError("No such archived letter for the selected source device")
        text, attachment = _blob(db, row[0])
        meta = json.loads(row[1])
        if not isinstance(meta, dict) or meta.get("id") != identifier:
            raise ValueError("Invalid archive metadata")
    path = Path(output)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Choose an absolute output path")
    _checked(path.parent)
    encoded = json.dumps(
        {
            "schema": FORMAT,
            "source_device": source,
            "id": identifier,
            "sha256": row[0],
            "metadata": meta,
            "text": text,
            "letter_attachment": json.loads(attachment) if attachment else None,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf8")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, path)  # Atomic no-clobber publication in the same directory.
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "status": "extracted",
        "id": identifier,
        "sha256": row[0],
        "bytes": len(encoded),
    }


def policy(root, package, keep_days=None, max_bytes=None, limit=25, *, apply=False):
    if (
        keep_days is None
        and max_bytes is None
        or type(limit) is not int
        or not 1 <= limit <= 100
        or keep_days is not None
        and (type(keep_days) is not int or not 0 <= keep_days <= 36500)
        or max_bytes is not None
        and (type(max_bytes) is not int or not 0 <= max_bytes <= 2**50)
    ):
        raise ValueError(
            "Specify a nonnegative age or byte limit, with a batch size between 1 and 100"
        )
    before = usage(root)
    remaining = before["active_material_bytes"]
    chosen = []
    if before["database_bytes"]:
        path = Path(root) / "state/messages.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "retention_processed" in tables or "mail_observations" in tables:
                columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
                checks = ["m.read_at IS NOT NULL"]
                if {
                    "recipient_identity",
                    "recipient_endpoint",
                    "read_by_endpoint",
                } <= columns:
                    checks.append(
                        "(m.read_by_endpoint IS NOT NULL OR (m.recipient_identity IS NULL AND m.recipient_endpoint IS NULL))"
                    )
                if "archive_digest" in columns:
                    checks.append("m.archive_digest IS NULL")
                if "withdrawn_at" in columns:
                    checks.append("m.withdrawn_at IS NULL")
                if "retention_holds" in tables:
                    checks.append("m.id NOT IN (SELECT id FROM retention_holds)")
                rows = db.execute(
                    """SELECT m.id, (julianday('now')-julianday(m.read_at)) as age,
                        length(CAST(m.text AS BLOB))+COALESCE(length(CAST(m.letter_attachment AS BLOB)),0) AS size
                        FROM messages m JOIN ("""
                    + _completed(tables)
                    + ") p ON p.id=m.id WHERE "
                    + " AND ".join(checks)
                    + " ORDER BY m.read_at,m.seq LIMIT ?",
                    (limit,),
                )
                for identifier, age, size in rows:
                    if not (
                        (max_bytes is not None and remaining > max_bytes)
                        or (
                            keep_days is not None
                            and age is not None
                            and age >= keep_days
                        )
                    ):
                        break
                    chosen.append(identifier)
                    remaining -= size
    done = []
    if apply:
        for identifier in chosen:
            try:
                record(root, identifier, "archive", package, processed_only=True)
            except ValueError as exc:
                return {
                    "status": "partial",
                    "completed": done,
                    "error": str(exc),
                    "after": usage(root),
                }
            done.append(identifier)
    result = usage(root) if apply else before
    return {
        "status": "applied" if apply else "plan",
        "selected": chosen,
        "completed": done,
        "before": before,
        "after": result,
        "above_requested_capacity": max_bytes is not None
        and result["active_material_bytes"] > max_bytes,
    }


def folder_usage(folder, limit=20000):
    """Measure named paired-mail slots without reading, moving or following individual files."""
    folder = Path(folder)
    if not folder.is_absolute() or ".." in folder.parts:
        raise ValueError("Choose an absolute registered paired-mail folder")
    _checked(folder)
    if not (folder / "in").is_dir() or not (folder / "out").is_dir():
        raise ValueError(
            "Expected an existing paired mailbox directory with in and out slots"
        )
    metrics = {}
    visits = 0
    for slot in ("in", "out", "read", "results", "signals"):
        directory = folder / slot
        if not directory.exists():
            continue
        _checked(directory)
        counts = metrics[slot] = {"files": 0, "bytes": 0, "skipped_entries": 0}
        with os.scandir(directory) as entries:
            for entry in entries:
                if visits >= limit:
                    return {"complete": False, "entry_limit": limit, "slots": metrics}
                visits += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    counts["skipped_entries"] += 1
                    continue
                if (
                    not entry.name.endswith(".json")
                    or not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    counts["skipped_entries"] += 1
                    continue
                counts["files"] += 1
                counts["bytes"] += info.st_size
    return {"complete": True, "slots": metrics}
