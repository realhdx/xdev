"""Temporary end-to-end encrypted session mail in a paired private GitHub release."""

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import http.client
import json
import re
import sqlite3
import ssl
import time
from urllib.parse import urlsplit

from src.runtime import device_names, github_capacity, mail_control, outbox, session_relay as mail, transport
from src.runtime.mail_routes import NotSubmitted


DOMAIN = b"xdev.github-mail.v1\x00"
MAX_WIRE = mail.MAX_BYTES + 8
POLL = 20
MAX_IDLE_POLL = 300
API = "api.github.com"
UPLOAD = "uploads.github.com"


class Unavailable(RuntimeError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class AccessDenied(ValueError):
    """GitHub denied this credential or repository operation."""


class AssetPending(Unavailable):
    """A listed object has not yet produced the advertised bytes."""
    def __init__(self, reason):
        super().__init__("GitHub mail asset is not ready", POLL)
        self.reason = reason


def safe_observation(item):
    observation = {"transport": "github", "status": item["status"]}
    if item.get("reason") in {"asset_not_visible", "incomplete_download", "asset_metadata_pending",
                              "download_digest_mismatch", "download_policy_rejected", "envelope_rejected",
                              "provider_metadata_rejected", "mailbox_rejected"}:
        observation["reason"] = item["reason"]
    return observation


class AssetConflict(ValueError):
    """A stable message name refers to other bytes."""


@dataclass(frozen=True)
class Box:
    repo: str
    repo_id: int
    release: int
    token: str = field(repr=False, compare=False)

    @property
    def context(self):
        return DOMAIN + mail._json([self.repo, self.repo_id, self.release])

    @property
    def base(self):
        return f"/repos/{self.repo}"


def _box(record):
    repo = record.get("repository")
    if (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", repo)
            or repo.split("/")[1] in {".", ".."}):
        raise ValueError("Configure the exact GitHub owner/repository")
    repo_id, release = record.get("repository_id"), record.get("release_id")
    if any(type(val) is not int or val < 1 for val in (repo_id, release)):
        raise ValueError("Pin positive numeric GitHub repository_id and release_id")
    token = mail._file(record, "token_file").read_text(encoding="ascii").rstrip("\r\n")
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{1,4096}", token):
        raise ValueError("Invalid GitHub token file")
    return Box(repo.lower(), repo_id, release, token)


class Settings(mail.PairedMailPeers):
    delivery_scope = "mailbox"

    def __init__(self, root):
        conf = device_names.github_mailbox_config(transport.config(root))
        if not isinstance(conf, dict):
            raise ValueError("Configure github_mailbox on this device")
        if conf.get("delivery_scope", "mailbox") != "mailbox":
            raise ValueError("The GitHub carrier accepts mailbox delivery only")
        super().__init__(root, conf.get("peers"), require_encryption=True)
        self.inbox = _box(conf)
        self.ttl = conf.get("retention_seconds", 3600)
        if type(self.ttl) is not int or not 60 <= self.ttl <= 86400:
            raise ValueError("GitHub mail retention must be 60–86400 seconds")
        self.capacity = github_capacity.limits(conf)
        ids = {"repository", "repository_id", "release_id"}
        self.boxes = {}
        self.sources = {}
        for name, entry in conf["peers"].items():
            peer = self.devices.identity_key(name)
            if ids & entry.keys() and not ids <= entry.keys():
                raise ValueError("Override the repository name, numeric ID and release ID together")
            self.boxes[peer] = _box({**conf, **entry}) if (ids | {"token_file"}) & entry.keys() else self.inbox
            self.sources[self.route(peer, self.local, self.inbox)] = peer

    def route(self, sender, recipient, box):
        key = self.key(recipient if sender == self.local else sender)
        tag = hmac.digest(key, box.context + b"route" + mail._json([sender, recipient]), "sha256")
        return "xdevmail-v1-" + tag[:16].hex() + "-"

    def name(self, sender, recipient, box, mid):
        route = self.route(sender, recipient, box)
        key = self.key(recipient if sender == self.local else sender)
        return route + hmac.digest(key, DOMAIN + route.encode() + mid.encode(), "sha256").hex() + ".bin"


def _http(host, method, path, headers, body=None, *, maximum=3_000_000):
    conn = http.client.HTTPSConnection(host, timeout=12)
    try:
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Oversize GitHub mail response")
        return res.status, {k.lower(): v for k, v in res.getheaders()}, data
    except ssl.SSLError as exc:
        raise ValueError("GitHub TLS verification failed") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise Unavailable("GitHub mail connection did not complete") from exc
    finally:
        conn.close()


def _delay(headers):
    value = headers.get("retry-after", "")
    try:
        delay = int(value) if value.isdecimal() else parsedate_to_datetime(value).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        delay = 60
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            delay = max(delay, int(headers["x-ratelimit-reset"]) - time.time())
        except (KeyError, ValueError):
            pass
    return max(1, int(delay) + 1)


def _limited(status, headers, data):
    return status == 429 or (status == 403 and ("retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
                                               or b"rate limit" in data[:4096].lower()))


def _status(status, headers, allowed, data=b""):
    if status in allowed:
        return
    if _limited(status, headers, data):
        delay = _delay(headers)
        raise Unavailable(f"GitHub mail rate limited; retry after {delay} seconds", delay)
    if status >= 500 or status in {408, 409}:
        raise Unavailable(f"GitHub mail operation unconfirmed (HTTP {status})")
    if status in {401, 403}:
        raise AccessDenied(f"GitHub mail permission denied (HTTP {status})")
    raise ValueError(f"GitHub mail request refused (HTTP {status})")


class Api:
    def __init__(self, root=None):
        self.cache = {}
        self.next_write = 0
        self.root = root

    def cooldown(self, repo_id, delay=0):
        if self.root is None:
            return
        with closing(_journal(self.root)) as db, db:
            if delay:
                db.execute("""INSERT INTO cooldown VALUES (?,?) ON CONFLICT(repository_id)
                            DO UPDATE SET until=max(until,excluded.until)""", (repo_id, time.time() + delay))
            row = db.execute("SELECT until FROM cooldown WHERE repository_id=?", (repo_id,)).fetchone()
        if row and row[0] > time.time():
            seconds = max(1, int(row[0] - time.time()) + 1)
            raise Unavailable(f"GitHub mail rate limited; retry after {seconds} seconds", seconds)

    def call(self, box, method, path, *, host=API, extra=None, body=None, maximum=3_000_000):
        headers = {"User-Agent": "xdev-session-mail", "Accept": "application/vnd.github+json",
                   "Authorization": "Bearer " + box.token, "X-GitHub-Api-Version": "2022-11-28"}
        headers.update(extra or {})
        if method in {"DELETE", "POST"}:
            wait = self.next_write - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_write = time.monotonic() + 1
        self.cooldown(box.repo_id)
        result = _http(host, method, path, headers, body, maximum=maximum)
        status, response_headers, data = result
        if _limited(status, response_headers, data):
            self.cooldown(box.repo_id, _delay(response_headers))
        return result

    def document(self, box, path, *, refresh_after=None):
        key = (box, path)
        cached = self.cache.get(key)
        conditional = cached and (refresh_after is None or time.monotonic() - cached[2] < refresh_after)
        headers = {"If-None-Match": cached[0]} if conditional else ({"Cache-Control": "no-cache"} if cached else {})
        status, head, data = self.call(box, "GET", path, extra=headers)
        if status == 304:
            if conditional:
                return cached[1]
            raise Unavailable("GitHub returned no representation for an unconditional mail check", POLL)
        _status(status, head, {200}, data)
        document = json.loads(data)
        if head.get("etag"):
            self.cache[key] = (head["etag"], document, time.monotonic())
        else:
            self.cache.pop(key, None)
        return document

    def inspect(self, box):
        repo = self.document(box, box.base)
        if (repo.get("private") is not True or repo.get("visibility") != "private"
                or repo.get("id") != box.repo_id or str(repo.get("full_name")).lower() != box.repo):
            raise ValueError("GitHub mail requires the pinned private repository")
        release = self.document(box, f"{box.base}/releases/{box.release}")
        if release.get("id") != box.release or release.get("immutable") is True:
            raise ValueError("GitHub mail requires the selected mutable release")

    def assets(self, box):
        items = []
        for page in range(1, 12):
            batch = self.document(box, f"{box.base}/releases/{box.release}/assets?per_page=100&page={page}",
                                  refresh_after=300)
            if not isinstance(batch, list) or len(batch) > 100:
                raise ValueError("Invalid GitHub release asset list")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ValueError("Use a dedicated GitHub mail release with at most 1000 assets")

    def download(self, box, asset):
        aid = asset.get("id")
        if type(aid) is not int or aid <= 0:
            raise ValueError("Invalid GitHub mail asset metadata")
        size = asset.get("size")
        if size is None or size == 0:
            raise AssetPending("asset_metadata_pending")
        if type(size) is not int or not 0 < size <= MAX_WIRE:
            raise ValueError("Invalid GitHub mail asset size")
        status, head, data = self.call(box, "GET", f"{box.base}/releases/assets/{aid}",
                                      extra={"Accept": "application/octet-stream"}, maximum=MAX_WIRE)
        if status == 302:
            # Never forward a bearer token or API headers to the signed asset URL.
            url = urlsplit(head.get("location", ""))
            if (url.scheme != "https" or url.netloc not in
                    {"release-assets.githubusercontent.com", "objects.githubusercontent.com",
                     "github-releases.githubusercontent.com"} or url.fragment or not url.path.startswith("/")):
                raise ValueError("Unexpected GitHub release download host")
            status, head, data = _http(url.hostname, "GET", url.path + ("?" + url.query if url.query else ""),
                                       {"User-Agent": "xdev-session-mail"}, maximum=MAX_WIRE)
        if status == 404:
            return None
        _status(status, head, {200}, data)
        if len(data) != size:
            raise AssetPending("incomplete_download")
        digest = asset.get("digest")
        if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            if not hmac.compare_digest(digest[7:], hashlib.sha256(data).hexdigest()):
                raise AssetPending("download_digest_mismatch")
        return data

    def upload(self, box, name, wire):
        status, head, data = self.call(box, "POST", f"{box.base}/releases/{box.release}/assets?name={name}",
                                      host=UPLOAD, extra={"Content-Type": "application/octet-stream"}, body=wire)
        if status == 422:
            return None
        _status(status, head, {201}, data)
        return json.loads(data)

    def delete(self, box, aid):
        if type(aid) is not int or aid <= 0:
            raise ValueError("Invalid release asset ID")
        status, head, data = self.call(box, "DELETE", f"{box.base}/releases/assets/{aid}")
        _status(status, head, {204, 404}, data)
        return status == 204


def _journal(root):
    state = root / "state"
    state.mkdir(exist_ok=True, parents=True)
    db = sqlite3.connect(state / "github-mail.sqlite3", timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS uploads
                  (name TEXT PRIMARY KEY, expires INTEGER NOT NULL, digest TEXT, attempted INTEGER DEFAULT 0,
                   asset INTEGER)""")
    db.execute("CREATE TABLE IF NOT EXISTS cleaned (name TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS cleanup_observations (
               name TEXT PRIMARY KEY, status TEXT NOT NULL, next_check INTEGER NOT NULL)""")
    db.execute("CREATE TABLE IF NOT EXISTS cooldown (repository_id INTEGER PRIMARY KEY, until REAL NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS local_events
                  (event_id INTEGER PRIMARY KEY, inbox_seq INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS pending_reports
                  (name TEXT PRIMARY KEY, envelope TEXT NOT NULL, source_name TEXT NOT NULL, source_asset INTEGER NOT NULL,
                   expires INTEGER NOT NULL, due INTEGER NOT NULL DEFAULT 0)""")
    return db


def _matched(api, box, name, digest, asset):
    if not asset or asset.get("name") != name or asset.get("state") != "uploaded":
        return None
    aid = asset.get("id")
    if type(aid) is not int or aid <= 0:
        raise ValueError("Invalid GitHub upload confirmation")
    remote = asset.get("digest")
    if not remote:
        raw = api.download(box, asset)
        if raw is None:
            return None
        remote = "sha256:" + hashlib.sha256(raw).hexdigest()
    if remote != "sha256:" + digest:
        raise AssetConflict("The GitHub message ID already names different sealed content")
    return aid


def _destination(settings, letter, notice):
    peer = settings.devices.identity_key(letter["to"])
    box = settings.boxes.get(peer)
    if box is None:
        raise ValueError("No paired GitHub mail destination")
    if "_control" in letter:
        if set(letter) != {"id", "to", "_control"} or notice is not None or letter["id"] != letter["_control"]["id"]:
            raise ValueError("Invalid control routing fields")
        return box, settings.name(settings.local, peer, box, letter["id"])
    if letter.get("kind") == "receipt":
        if (not _receipt_shape(letter) or notice is not None
                or letter.get("id") != mail.report_id(peer, letter["reply_to"])):
            raise ValueError("GitHub mail receipts must be correlated xdev delivery reports")
    elif not (letter.get("recipient_identity") or {}).get("session"):
        raise ValueError("GitHub mail requires a named recipient chat")
    return box, settings.name(settings.local, peer, box, letter["id"])


def _wire(settings, box, letter, notice, report, expires):
    header = expires.to_bytes(8, "big")
    if "_control" in letter:
        if report or notice is not None:
            raise ValueError("Mailbox controls are always silent")
        return header + mail_control.pack(settings, box, letter["_control"], header)
    if notice is not None:
        mail._validate(settings.root, letter, notice)
    # A remote participant can supply a letter, never a host prompt. Also protect recipients
    # still on an earlier release. Old bytes are computed only to check their local digest.
    _, packet = mail._packet(settings, letter, None, delivery_report=report, context=box.context + header)
    return header + packet


def _legacy_match(settings, box, letter, notice, report, expires, digest):
    if notice is None:
        return False
    header = expires.to_bytes(8, "big")
    _, packet = mail._packet(settings, letter, notice, delivery_report=report, context=box.context + header)
    return hmac.compare_digest(hashlib.sha256(header + packet).hexdigest(), digest)


def _send(api, settings, letter, notice, report=False):
    box, name = _destination(settings, letter, notice)
    with closing(_journal(settings.root)) as db:
        with db:
            db.execute("INSERT OR IGNORE INTO uploads(name, expires) VALUES (?, ?)",
                       (name, int(time.time()) + settings.ttl))
            expires, = db.execute("SELECT expires FROM uploads WHERE name=?", (name,)).fetchone()
        wire = _wire(settings, box, letter, notice, report, expires)
        digest = hashlib.sha256(wire).hexdigest()
        with db:
            db.execute("UPDATE uploads SET digest=? WHERE name=? AND digest IS NULL", (digest, name))
            previous, attempted, aid = db.execute(
                "SELECT digest, attempted, asset FROM uploads WHERE name=?", (name,)).fetchone()
        if digest != previous:
            if not _legacy_match(settings, box, letter, notice, report, expires, previous):
                raise ValueError("Reused GitHub mail ID with different content")
            if attempted:
                # A pre-upgrade upload might already exist. Only check its old digest.
                digest = previous
            else:
                with db:
                    updated = db.execute("UPDATE uploads SET digest=? WHERE name=? AND digest=? AND attempted=0",
                                         (digest, name, previous)).rowcount
                if not updated:
                    raise Unavailable("Legacy upload state changed; reconcile the original ID")
        if aid is not None:
            return aid
        if time.time() >= expires:
            raise ValueError("GitHub mail delivery window has expired")
        try:
            api.inspect(box)
            listing, confirmed_before_listing = github_capacity.snapshot(db, box, api)
            existing = next((a for a in listing if a.get("name") == name), None)
        except Unavailable as exc:
            if attempted:
                raise
            raise NotSubmitted(f"GitHub unavailable before submission: {exc}") from exc
        aid = _matched(api, box, name, digest, existing)
        if aid is None:
            # Reserve the only POST before sending. If its ACK is lost, or another consumer
            # already removed it, a later retry can look up the asset but cannot post again.
            with db:
                claimed = db.execute("UPDATE uploads SET attempted=1 WHERE name=? AND attempted=0", (name,)).rowcount
                if claimed and existing is None:
                    github_capacity.reserve(db, settings, box, listing, name, len(wire), expires, time.time(),
                                            confirmed_before_listing=confirmed_before_listing)
            if not claimed or existing is not None:
                raise Unavailable("Original GitHub upload is unconfirmed; check the original ID")
            asset = api.upload(box, name, wire)
            if asset is None:
                asset = next((a for a in api.assets(box) if a.get("name") == name), None)
            aid = _matched(api, box, name, digest, asset)
            if aid is None:
                raise Unavailable("GitHub did not confirm the uploaded asset")
        with db:
            db.execute("UPDATE uploads SET asset=? WHERE name=?", (aid, name))
        return aid


def _ack(identifier, aid):
    return {"ok": True, "transport": "github", "status": "queue_acknowledged", "id": identifier,
            "provider_message_id": str(aid), "inbox_status": "unconfirmed", "session_accepted": None,
            "model_status": "unverified"}


def publish(root, envelope, notification=None, *, delivery_report=False):
    return _ack(envelope["id"], _send(Api(root), Settings(root), envelope, notification, delivery_report))


def retention_for(root, peer, identifier):
    """Local carrier evidence for a caller already matched to its outgoing letter."""
    settings = Settings(root)
    peer = settings.devices.identity_key(peer)
    box = settings.boxes[peer]
    name = settings.name(settings.local, peer, box, identifier)
    path = root / "state" / "github-mail.sqlite3"
    if not path.is_file():
        return {"state": "unknown"}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = db.execute("SELECT expires FROM uploads WHERE name=?", (name,)).fetchone() if "uploads" in tables else None
        if not row:
            return {"state": "unknown"}
        evidence = (db.execute("SELECT status FROM cleanup_observations WHERE name=?", (name,)).fetchone()
                    if "cleanup_observations" in tables else None)
    return {"expires_at_unix": row[0], "delivery_window_elapsed": time.time() >= row[0],
            "state": evidence[0] if evidence else "deletion_unconfirmed", "scope": "temporary_relay_asset"}


def reconcile(root, envelope, notification=None, *, delivery_report=False):
    """Lookup only, including after a lost local journal. Never submit, delete or replace a cloud object."""
    settings, api = Settings(root), Api(root)
    box, name = _destination(settings, envelope, notification)
    unknown = {"ok": False, "transport": "github", "status": "submission_unconfirmed", "id": envelope["id"],
               "inbox_status": "unknown", "session_accepted": None, "model_status": "unverified"}
    path = root / "state" / "github-mail.sqlite3"
    if not path.is_file():
        return unknown
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        row = db.execute("SELECT expires,digest,asset FROM uploads WHERE name=?", (name,)).fetchone()
    if row is None or row[1] is None:
        return unknown
    expires, digest, aid = row
    wire = _wire(settings, box, envelope, notification, delivery_report, expires)
    if hashlib.sha256(wire).hexdigest() != digest and not _legacy_match(
            settings, box, envelope, notification, delivery_report, expires, digest):
        raise ValueError("Reused GitHub mail ID with different content")
    if aid is None:
        api.inspect(box)
        existing = next((a for a in api.assets(box) if a.get("name") == name), None)
        aid = _matched(api, box, name, digest, existing)
        if aid is None:
            return unknown
        with closing(_journal(root)) as db, db:
            db.execute("UPDATE uploads SET asset=? WHERE name=? AND digest=?", (aid, name, digest))
    return _ack(envelope["id"], aid)


def _receipt_shape(envelope):
    identifier = envelope.get("reply_to")
    if not isinstance(identifier, str) or envelope.get("sender_endpoint") is not None:
        return False
    if any(envelope.get(k) for k in ("letter_attachment", "group", "reply_endpoint", "recipient_endpoint")):
        return False
    try:
        data = json.loads(envelope["text"])
        return (isinstance(data, dict) and set(data) ==
                {"schema", "message_id", "inbox_status", "session_status", "placement"}
                and data["schema"] == mail.REPORT_SCHEMA and data["message_id"] == identifier
                and data["inbox_status"] == "received" and mail.valid_progress(data["session_status"], data["placement"]))
    except (TypeError, KeyError, ValueError):
        return False


def _accept(settings, source, envelope, notice, report):
    target = envelope.get("recipient_identity") or {}
    if envelope.get("kind") == "receipt":
        if notice is not None or report or not _receipt_shape(envelope) or not outbox.record_report(settings.root, envelope):
            raise ValueError("Unmatched GitHub mail delivery report")
    else:
        if not target.get("session"):
            raise ValueError("GitHub mail requires a named recipient chat")
        # A live tool call is authorized by its exact durable request and both chat identities.
        # It has no passive receiver to enable and must never send a wake to an idle host.
        waiting = (envelope.get("kind") == "reply" and notice is None and not envelope.get("recipient_endpoint")
                   and outbox.permits_tool_reply(settings.root, envelope))
        if not waiting and (not settings.allows(source, target)
                            or (envelope.get("kind") == "reply" and not outbox.permits_reply(settings.root, envelope))):
            raise ValueError("Unauthorized GitHub mail recipient or callback")
    item = mail._receive(settings, source, envelope, None, report)
    kind = "delivery_report_received" if envelope.get("kind") == "receipt" else "session_mail_available"
    with closing(_journal(settings.root)) as db, db:
        item["event_created"] = bool(db.execute(
            "INSERT OR IGNORE INTO local_events(inbox_seq,kind) VALUES (?,?)", (item["seq"], kind)).rowcount)
        cursor, = db.execute("SELECT event_id FROM local_events WHERE inbox_seq=?", (item["seq"],)).fetchone()
    item["local_event"] = _event(cursor, item["seq"], kind)
    return item


def _event(cursor, seq, kind):
    return {"transport": "github", "event": kind, "event_id": cursor, "inbox_seq": seq,
            "delivery_scope": "mailbox", "model_woken": False}


def events(root, after=0, limit=25):
    """Read safe durable local notifications, with a cursor independent of provider connectivity."""
    if type(after) is not int or not 0 <= after < 2**63 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Use a nonnegative event cursor and a limit from 1 through 1000")
    path = root / "state" / "github-mail.sqlite3"
    items = []
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_events'").fetchone():
                rows = db.execute("SELECT event_id,inbox_seq,kind FROM local_events WHERE event_id>? "
                                  "ORDER BY event_id LIMIT ?", (after, limit)).fetchall()
                items = [_event(*row) for row in rows]
    return {"ok": True, "transport": "github", "results": items,
            "next_after": items[-1]["event_id"] if items else after}


def _sweep_sent(api, settings):
    # An online sender can also clean its peer's separate release when that peer is offline.
    # Only remove a locally submitted asset whose authenticated bytes still match our record.
    routes = {settings.route(settings.local, peer, box): box for peer, box in settings.boxes.items()}
    listings = {}
    with closing(_journal(settings.root)) as db:
        now = int(time.time())
        due = db.execute("""SELECT u.name,u.digest,u.asset FROM uploads u
                            LEFT JOIN cleanup_observations o ON u.name=o.name
                            WHERE u.expires<=? AND u.attempted=1 AND COALESCE(o.next_check,0)<=?
                            AND u.name NOT IN (SELECT name FROM cleaned)
                            ORDER BY COALESCE(o.next_check,0),u.expires,u.name LIMIT 50""", (now,now)).fetchall()
        for name, digest, recorded in due:
            box = routes.get(name[:-68])
            status = "pair_not_configured"
            if box:
                if box not in listings:
                    api.inspect(box)
                    listings[box] = {a["name"]: a for a in api.assets(box)}
                asset = listings[box].get(name)
                status = "not_visible_outcome_unknown"
                if asset is not None:
                    try:
                        aid = _matched(api, box, name, digest, asset)
                    except AssetConflict:
                        aid = None
                    if aid is None or (recorded is not None and aid != recorded):
                        status = "identity_not_verified"
                    else:
                        status = "expired_asset_removed" if api.delete(box, aid) else "not_visible_outcome_unknown"
            with db:
                db.execute("""INSERT INTO cleanup_observations VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET
                              status=excluded.status,next_check=excluded.next_check
                              WHERE cleanup_observations.status!='expired_asset_removed'""", (name,status,now+86400))
                if status == "expired_asset_removed":
                    db.execute("INSERT OR IGNORE INTO cleaned VALUES (?)", (name,))


def _queue_report(settings, envelope, source_name, source_asset):
    """Durably freeze the local report and both route names before consuming the original."""
    _, name = _destination(settings,envelope,None)
    wire = mail._json(envelope).decode("utf-8")
    with closing(_journal(settings.root)) as db, db:
        db.execute("""INSERT OR IGNORE INTO pending_reports(name,envelope,source_name,source_asset,expires)
                      VALUES (?,?,?,?,?)""", (name,wire,source_name,source_asset,int(time.time())+settings.ttl))
        recorded = db.execute("SELECT envelope,source_name FROM pending_reports WHERE name=?", (name,)).fetchone()
        if recorded != (wire,source_name):
            raise ValueError("The stored report does not match this letter and route")


def _flush_reports(api, settings, remaining, limit):
    """Send only after the original asset is gone; a restarted process verifies the fresh inbox list."""
    sent = set()
    now = int(time.time())
    with closing(_journal(settings.root)) as db:
        with db:
            db.execute("DELETE FROM pending_reports WHERE expires<=?",(now,))
        rows = db.execute("""SELECT name,envelope,source_name,source_asset,expires FROM pending_reports
                             WHERE due<=? ORDER BY due,rowid LIMIT ?""",(now,min(limit,10))).fetchall()
        for name,wire,source_name,source_asset,expires in rows:
            envelope = json.loads(wire)
            peer = envelope["to"]
            delay = POLL
            throttle = 0
            if ((source_name,source_asset) not in remaining and settings.sources.get(source_name[:-68]) == peer
                    and peer in settings.boxes and settings.name(settings.local,peer,settings.boxes[peer],envelope["id"]) == name):
                try:
                    _send(api,settings,envelope,None)
                except (NotSubmitted,Unavailable) as exc:
                    # _send may reattempt a definite non-submission. Once the POST was claimed it
                    # can only compare the exact existing asset; unknown outcomes cannot post again.
                    throttle = max(getattr(exc,"retry_after",0),getattr(exc.__cause__,"retry_after",0))
                except AssetConflict:
                    delay = max(POLL,expires-now)
                else:
                    with db:
                        db.execute("DELETE FROM pending_reports WHERE name=?", (name,))
                    sent.add(envelope["id"])
                    continue
            with db:
                db.execute("UPDATE pending_reports SET due=? WHERE name=?", (now+max(delay,throttle),name))
            if throttle:
                # Api.cooldown persists the provider deadline across listeners and restarts.
                break
    return sent


def _collect(api, settings, limit, seen):
    box = settings.inbox
    api.inspect(box)
    items = []
    assets = api.assets(box)
    remaining = {(a.get("name"),a.get("id")) for a in assets}
    live = {(a.get("id"), a.get("updated_at"), a.get("digest")) for a in assets}
    for identity in seen.keys() - live:
        del seen[identity]
    for asset in assets:
        name = asset.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"xdevmail-v1-[0-9a-f]{32}-[0-9a-f]{64}\.bin", name):
            continue
        source = settings.sources.get(name[:-68])
        if source is None:
            continue
        aid = asset.get("id")
        identity = (aid, asset.get("updated_at"), asset.get("digest"))
        if seen.get(identity, 0) > time.time() or asset.get("state") != "uploaded":
            continue
        expires = time.time() + settings.ttl
        reject_reason = "download_policy_rejected"
        try:
            raw = api.download(box, asset)
            if raw is None:
                raise AssetPending("asset_not_visible")
            reject_reason = "envelope_rejected"
            stamp, wire = raw[:8], raw[8:]
            topic = f"{mail.PREFIX}.{settings.local}.{source}"
            control = wire.startswith(mail_control.MAGIC)
            if control:
                peer, notice, requested = source, None, False
                letter = mail_control.unpack(settings, peer, box, stamp, wire)
            else:
                peer, letter, notice, requested = mail._unpack(settings, topic, wire, context=box.context + stamp)
            if settings.name(source, settings.local, box, letter["id"]) != name:
                raise ValueError("Opaque GitHub asset name does not match its authenticated message ID")
            reject_reason = "provider_metadata_rejected"
            created = datetime.fromisoformat(asset["created_at"])
            if created.tzinfo is None:
                raise ValueError("Invalid GitHub asset timestamp")
            expires = min(int.from_bytes(stamp, "big"), created.timestamp() + settings.ttl)
            if time.time() >= expires:
                item = {"status": "expired", "provider_message_id": str(aid)}
            else:
                reject_reason = "mailbox_rejected"
                item = (mail_control.accept(settings, peer, letter) if control
                        else _accept(settings, peer, letter, notice, requested))
        except AssetPending as exc:
            # The list and the object download are separate provider operations.
            # Keep the same ID eligible on the next poll without acknowledging it.
            seen[identity] = time.time() + POLL
            items.append({"status": "asset_pending", "reason": exc.reason, "provider_message_id": str(aid)})
        except AccessDenied:
            raise
        except (ValueError, KeyError, TypeError, RecursionError):
            # An unauthenticated or unaddressed repository object is neither acknowledged nor removed.
            seen[identity] = max(time.time() + POLL, expires)
            items.append({"status": "rejected", "reason": reject_reason, "provider_message_id": str(aid)})
        else:
            if requested and item.get("session_notification", {}).get("awaiting_native_result"):
                continue
            if "_report" in item:
                report = item.pop("_report")
                _queue_report(settings,report,name,aid)
                item["report_id"] = report["id"]
                item["report_status"] = "pending"
            api.delete(box, aid)
            remaining.discard((name,aid))
            items.append(item)
        if len(items) >= limit:
            break
    _sweep_sent(api, settings)
    sent = _flush_reports(api,settings,remaining,limit)
    for item in items:
        if item.get("report_id") in sent:
            item["report_status"] = "queue_acknowledged"
    mail_control.flush(api, settings, limit)
    return items


def listen(root, limit=25, timeout=30):
    seconds = mail._limits(limit, timeout)
    settings, api = Settings(root), Api(root)
    deadline = time.monotonic() + seconds
    items, seen = [], {}
    while True:
        items.extend(_collect(api, settings, limit - len(items), seen))
        remaining = deadline - time.monotonic()
        if len(items) >= limit or remaining <= 0:
            break
        time.sleep(min(POLL, remaining))
        if time.monotonic() >= deadline:
            break
    return {"ok": True, "transport": "github", "status": "completed" if len(items) == limit else "timeout",
            "results": items}


def follow(root, report, limit=25, timeout=30):
    from src.runtime import github_demand
    mail._limits(limit, timeout)
    settings, api, seen = Settings(root), Api(root), {}
    idle_delay = POLL
    retry_delay = POLL
    while True:
        try:
            items = _collect(api, settings, limit, seen)
            for item in items:
                if "local_event" in item:
                    if item["event_created"]:
                        report(item["local_event"])
                else:
                    report(safe_observation(item))
            # No cloud push is available. A quiet mailbox needs fewer requests.
            # A received batch restores the normal cadence for the conversation.
            active = any(item.get("event_created") or item.get("status") == "asset_pending" for item in items)
            pause = POLL if active else idle_delay
            idle_delay = POLL if active else min(MAX_IDLE_POLL, idle_delay * 2)
            retry_delay = POLL
            minimum = POLL
        except Unavailable as exc:
            retry_delay = min(MAX_IDLE_POLL, retry_delay * 2)
            minimum = max(exc.retry_after, retry_delay)
            pause = max(minimum, idle_delay)
            report({"transport": "github", "status": "disconnected", "retry_seconds": pause})
        github_demand.wait(root, pause, minimum)
