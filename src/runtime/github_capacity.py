"""Admission limits for one gateway's releases. Never remove mail to make space."""

from contextlib import closing
from pathlib import Path
import sqlite3

from src.runtime.mail_routes import NotSubmitted


def limits(config):
    count = config.get("max_pending_assets", 500)
    size = config.get("max_pending_bytes", 64 * 1024 * 1024)
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("max_pending_assets must be between 1 and 1000")
    if type(size) is not int or not 1 <= size <= 1024**3:
        raise ValueError("max_pending_bytes must be between 1 and 1073741824")
    return count, size


def snapshot(db, box, api):
    # Capture confirmed uploads before the network read. A later ACK can belong to
    # a concurrent POST that the fetch could not have observed yet.
    confirmed = set()
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_claims'"
    ).fetchone():
        confirmed = {
            r[0]
            for r in db.execute(
                """
            SELECT c.name FROM capacity_claims c JOIN uploads u ON u.name=c.name
            WHERE c.repository_id=? AND c.release_id=? AND u.attempted=1
            AND typeof(u.asset)='integer' AND u.asset>0""",
                (box.repo_id, box.release),
            )
        }
    return api.assets(box), confirmed


def reserve(
    db,
    settings,
    box,
    listing,
    name,
    length,
    expires,
    now,
    *,
    confirmed_before_listing=(),
):
    """Called in a writer transaction with the one-POST claim. Count each name once."""
    db.execute("""CREATE TABLE IF NOT EXISTS capacity_claims (
        name TEXT PRIMARY KEY, repository_id INTEGER NOT NULL, release_id INTEGER NOT NULL,
        size INTEGER NOT NULL, expires INTEGER NOT NULL)""")
    db.execute("DELETE FROM capacity_claims WHERE expires <= ?", (now,))
    observed = {}
    for asset in listing:
        size = asset.get("size")
        asset_name = asset.get("name")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(asset_name, str)
            or asset_name in observed
        ):
            raise NotSubmitted(
                "Release capacity cannot yet be verified; no upload was attempted"
            )
        observed[asset_name] = size
    # Reuse an acknowledged slot when a subsequent complete release listing no
    # longer contains it. This changes capacity accounting only, never receipts
    # or outcome evidence. Ambiguous writes stay charged until their deadline.
    released = set(confirmed_before_listing).difference(observed)
    db.executemany(
        "DELETE FROM capacity_claims WHERE name=? AND repository_id=? AND release_id=?",
        ((key, box.repo_id, box.release) for key in released),
    )
    for key, size in db.execute(
        "SELECT name,size FROM capacity_claims WHERE repository_id=? AND release_id=?",
        (box.repo_id, box.release),
    ):
        observed[key] = max(size, observed.get(key, 0))
    observed[name] = max(length, observed.get(name, 0))
    max_assets, max_bytes = settings.capacity
    if len(observed) > max_assets or sum(observed.values()) > max_bytes:
        raise NotSubmitted("GitHub mailbox capacity reached; no upload was attempted")
    db.execute(
        "INSERT OR IGNORE INTO capacity_claims VALUES(?,?,?,?,?)",
        (name, box.repo_id, box.release, length, expires),
    )


def cleanup_status(root):
    """Read just local observations, including tombstones from older versions."""
    path = Path(root) / "state" / "github-mail.sqlite3"
    observed = {}
    legacy = 0
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "cleanup_observations" in tables:
                observed = dict(
                    db.execute(
                        "SELECT status,count(*) FROM cleanup_observations GROUP BY status"
                    )
                )
            if "cleaned" in tables:
                query = "SELECT count(*) FROM cleaned"
                if "cleanup_observations" in tables:
                    query += (
                        " WHERE name NOT IN (SELECT name FROM cleanup_observations)"
                    )
                legacy = db.execute(query).fetchone()[0]
    return {"observations": observed, "legacy_closed_without_outcome_detail": legacy}
