"""Stable discussion membership and per-recipient identifiers."""

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing

from src.runtime import messages, transport
from src.runtime.device_names import valid_name


def stable_id(prefix, *parts):
    wire = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + "-" + hashlib.sha256(wire.encode("utf-8")).hexdigest()


def device_key(device, root):
    """Return a device's stable identity token independently of its current display name.

    This key is for equality, SQLite indexes and stable operation hashes only.
    Unknown names can be compared as historical data and never become transport targets.
    """
    if root is None or device is None or device == "local":
        return device
    if not valid_name(device):
        raise ValueError("Invalid device address in a chat identity")
    directory = transport.directory(root)
    try:
        return directory.identity_key(device)
    except ValueError:
        return unicodedata.normalize("NFC", device).casefold()


def address_key(endpoint, *, root=None):
    address = messages.normalize_participant(endpoint)
    if not address:
        raise ValueError("A discussion member needs an exact chat address")
    return (device_key(address["device"], root), address["client"], address["session"]) + tuple(
        messages.profile_key(address.get(k)) or "" for k in ("codex_home", "claude_home"))


def normalize_group(group, *, root=None):
    if not isinstance(group, dict) or not {"id", "members"} <= group.keys() <= {"id", "members", "routes"}:
        raise ValueError("Invalid discussion membership")
    if not isinstance(group["id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", group["id"]):
        raise ValueError("Invalid discussion identifier")
    members = group["members"]
    if not isinstance(members, list) or not 2 <= len(members) <= 32:
        raise ValueError("A discussion supports 2 through 32 named chat participants")
    unique = {address_key(member, root=root): messages.normalize_participant(member) for member in members}
    if len(unique) != len(members):
        raise ValueError("Duplicate discussion member")
    normalized = {"id": group["id"], "members": sorted(unique.values(), key=address_key)}
    routes = {}
    for route in group.get("routes", []):
        route = messages.normalize_reply_endpoint(route)
        key = address_key(route, root=root)
        if key not in unique or key in routes:
            raise ValueError("A discussion route must identify one existing discussion member")
        if route != unique[key]:
            routes[key] = route
    if routes:
        normalized["routes"] = sorted(routes.values(), key=address_key)
    return normalized


def plan_group(group, root):
    """Reproduce an existing batch's historical fingerprint without changing its envelope."""
    if group is None:
        return None
    plan = {"id": group["id"]}
    for field in ("members", "routes"):
        if field in group:
            plan[field] = [{**address, "device": device_key(address["device"], root)}
                           for address in group[field]]
    return normalize_group(plan)


def batch_state(root, author, operation, fingerprint, *, receipt=None):
    directory = root / "state"
    directory.mkdir(parents=True, exist_ok=True)
    author_id = stable_id("author", address_key(author, root=root))
    with closing(sqlite3.connect(directory / "discussion-sends.sqlite3", timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS plans
                      (author TEXT, operation TEXT, fingerprint TEXT NOT NULL, PRIMARY KEY(author,operation))""")
        db.execute("""CREATE TABLE IF NOT EXISTS accepted
                      (author TEXT, operation TEXT, letter TEXT, receipt TEXT NOT NULL, PRIMARY KEY(author,operation,letter))""")
        db.execute("INSERT OR IGNORE INTO plans VALUES (?,?,?)", (author_id, operation, fingerprint))
        saved, = db.execute("SELECT fingerprint FROM plans WHERE author=? AND operation=?", (author_id, operation)).fetchone()
        if saved != fingerprint:
            raise ValueError("This batch ID already belongs to a different message or list of recipients")
        if receipt is not None:
            db.execute("INSERT OR IGNORE INTO accepted VALUES (?,?,?,?)",
                       (author_id, operation, receipt["id"], json.dumps(receipt, ensure_ascii=False)))
        rows = db.execute("SELECT letter,receipt FROM accepted WHERE author=? AND operation=?", (author_id, operation))
        return {key: json.loads(value) for key, value in rows}
