"""Explicitly forward a stored message to a selected OpenCode session."""

from hashlib import sha256
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

from src.runtime import messages


def _cli():
    override = os.environ.get("XDEV_OPENCODE_CLI")
    if override:
        return Path(override)
    base = Path(os.environ["APPDATA"]) / "ai.opencode.desktop" / "cli"
    candidates = [p for p in base.glob("*-x64/opencode-cli.exe")
                  if re.fullmatch(r"\d+\.\d+\.\d+-x64", p.parent.name)]
    if not candidates:
        raise RuntimeError("No OpenCode Desktop command-line executable found.")
    return max(candidates, key=lambda p: tuple(map(int, p.parent.name.split("-")[0].split("."))))


def _active_sessions(cli):
    try:
        process = subprocess.run([str(cli), "api", "session.active"],
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
        data = json.loads(process.stdout.decode("utf-8-sig")).get("data")
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return None
    return data if process.returncode == 0 and isinstance(data, dict) else None


def _admissions(root):
    directory = root / "state"
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "opencode-admissions.sqlite3", timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS admissions (
                prompt_id TEXT PRIMARY KEY, admitted INTEGER NOT NULL DEFAULT 0,
                delivery TEXT NOT NULL DEFAULT 'unknown')""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(admissions)")}
            if "delivery" not in columns:
                connection.execute("ALTER TABLE admissions ADD COLUMN delivery TEXT NOT NULL DEFAULT 'unknown'")
    except Exception:
        connection.close()
        raise
    return connection


def _existing(connection, receipt, requested_delivery=None):
    row = connection.execute(
        "SELECT admitted,delivery FROM admissions WHERE prompt_id=?", (receipt["prompt_id"],)
    ).fetchone()
    if row is None:
        return None
    if row[0]:
        delivery = row[1] if row[1] in {"steer", "queue"} else "unknown"
        accepted = {**receipt, "delivery_mode": delivery, "delivery": delivery,
                    "admission_evidence": "native_session_prompt_accepted", "model_status": "unverified"}
        if delivery == "queue" and requested_delivery == "steer":
            return {"ok": False, "status": "promotion_required", **accepted,
                    "error_code": "OPENCODE_QUEUE_PROMOTION_REQUIRED", "promotion_required": True,
                    "original_accepted": True, "original_status": "admitted",
                    "requested_delivery_mode": requested_delivery,
                    "next_action": "Promote the existing prompt with a supported native API; do not send a duplicate."}
        return {"ok": True, "status": "already_admitted", **accepted,
                **({"requires_delivery_verification": True} if delivery == "unknown" else {})}
    return {"ok": False, "error_code": "OPENCODE_ADMISSION_UNVERIFIED", "outcome": "unknown",
            "error_message": "A submission has started. Check the target session for this message ID.",
            **receipt, "delivery_mode": "unknown", "model_status": "unverified"}


def submit(root, identifier, session, steer, idle_only=False):
    """Admission to OpenCode is distinct from model completion."""
    if not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,100}", session):
        raise ValueError("Supply the id of the OpenCode session you want to receive the message.")
    query = messages.handle(root, "wait", {"id": identifier, "timeout": 0})
    item = query.get("message")
    if query.get("status") != "received" or not item:
        raise ValueError("This message is not in the local inbox.")
    prompt_id = "msg_" + sha256(f"{session}\n{identifier}".encode()).hexdigest()[:32]
    delivery = "steer" if steer else "queue"
    receipt = {"message_id": identifier, "session": session, "prompt_id": prompt_id,
               "requested_delivery_mode": delivery}
    with closing(_admissions(root)) as connection:
        previous = _existing(connection, receipt, delivery)
    if previous is not None:
        return previous
    cli = _cli()
    states = _active_sessions(cli)
    observed = "unknown" if states is None else ("active" if session in states else "idle")
    if idle_only and observed != "idle":
        return {"ok": True, "status": "deferred_in_inbox", "message_id": identifier,
                "session": session, "observed_before_submit": observed,
                "delivery_mode": "unknown", "requested_delivery_mode": delivery,
                "model_status": "unverified"}
    origin = item["from"]
    from src.adapters.notices import body as notice_body
    notice = f"[xdev notice, from {json.dumps(origin, ensure_ascii=False)}] " + notice_body(item, identifier, "opencode", session)
    data = {"id": prompt_id, "delivery": delivery,
            "text": notice}
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    argv = [str(cli), "api", "session.prompt", "--param", f"sessionID={session}", "--data", body]
    if len(subprocess.list2cmdline(argv).encode("utf-16-le")) >= 65500:
        raise ValueError("Message is too long for OpenCode's Windows CLI; send a file reference.")
    with closing(_admissions(root)) as connection:
        with connection:
            reserved = connection.execute(
                "INSERT OR IGNORE INTO admissions (prompt_id) VALUES (?)", (prompt_id,)
            ).rowcount
        if not reserved:
            return _existing(connection, receipt, delivery)
        return _submit_reserved(connection, argv, receipt, data["delivery"], observed)


def _submit_reserved(connection, argv, receipt, delivery, observed):
    try:
        process = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "OPENCODE_TIMEOUT", "outcome": "unknown", **receipt,
                "delivery_mode": "unknown", "model_status": "unverified"}
    except OSError:
        return {"ok": False, "error_code": "OPENCODE_START_UNVERIFIED", "outcome": "unknown", **receipt,
                "delivery_mode": "unknown", "model_status": "unverified"}
    try:
        returned = json.loads(process.stdout.decode("utf-8-sig")).get("data", {})
    except (UnicodeError, ValueError, AttributeError):
        returned = {}
    if process.returncode or not isinstance(returned, dict) or returned.get("id") != receipt["prompt_id"]:
        return {"ok": False, "error_code": "OPENCODE_ADMISSION_UNVERIFIED",
                "outcome": "unknown", **receipt, "delivery_mode": "unknown", "model_status": "unverified"}
    try:
        with connection:
            connection.execute("UPDATE admissions SET admitted=1,delivery=? WHERE prompt_id=?",
                               (delivery, receipt["prompt_id"]))
    except sqlite3.Error:
        return {"ok": False, "error_code": "OPENCODE_ACK_PERSIST_UNVERIFIED", "outcome": "unknown", **receipt,
                "delivery_mode": "unknown", "model_status": "unverified"}
    return {"ok": True, "status": "admitted", **receipt, "delivery": delivery,
            "delivery_mode": delivery, "admission_evidence": "native_session_prompt_accepted",
            "model_status": "unverified", "observed_before_submit": observed}
