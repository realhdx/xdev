"""Outbound-only JetStream delivery for paired devices and addressed sessions."""

import asyncio
from contextlib import closing
import hashlib
import hmac
import ipaddress
import json
import logging
from pathlib import Path
import re
import sqlite3
import ssl
import time
from urllib.parse import urlsplit

from src.adapters import delivery_contract, sessions
from src.runtime import messages, transport
from src.runtime.mail_routes import NotSubmitted


STREAM = "XDEV_SESSION_V1"
PREFIX = "xdev.session.v1"
MAX_BYTES = 1_000_000
SEALED_PREFIX = b"xdev.sealed-mail.v1\x00"
REPORT_SCHEMA = "xdev.delivery-report.v1"
REPORT_STATUSES = frozenset({"admitted", "queued", "surface_accepted", "not_requested", "already_read", "recalled",
                             "requires_activation", "requires_receiver_binding", "unverified_reply_origin", "unknown"})
REPORT_PLACEMENTS = frozenset(delivery_contract.SCOPES)
_PLACEMENT_STATUS = {"current_turn": "admitted", "original_session": "admitted",
                     "next_turn": "queued", "surface": "surface_accepted"}


def valid_progress(status, placement):
    if not isinstance(status, str) or not isinstance(placement, str) or status not in REPORT_STATUSES:
        return False
    if placement == "unknown":
        return status not in _PLACEMENT_STATUS.values()
    return _PLACEMENT_STATUS.get(placement) == status


def report_id(source_key, message_id):
    return "xdev-report-" + hashlib.sha256(_json([REPORT_SCHEMA, source_key, message_id])).hexdigest()


def _json(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _file(settings, key):
    value = settings.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"Configure an absolute {key} path on this device")
    return Path(value)


def receiver_policy(root, exact, apps):
    """Local policy may select whole applications or pin individual chats and profiles."""
    if not isinstance(exact, list) or not isinstance(apps, list):
        raise ValueError("Receiver addresses and receiver applications must be lists")
    if not all(isinstance(name, str) for name in apps):
        raise ValueError("Each receiver application must be a client identifier")
    clients = frozenset(sessions.client_name(name) for name in apps)
    addresses = []
    for record in exact:
        if not isinstance(record, dict) or record.keys() - {"client", "session", "codex_home", "claude_home"}:
            raise ValueError("Each receiver address must name one local chat")
        addresses.append(messages.normalize_reply_endpoint({"device": transport.node(root), **record}))
    return clients, addresses


def receiver_allowed(root, policy, recipient):
    clients, addresses = policy
    route = messages.normalize_reply_endpoint(recipient)
    route.pop("delivery", None)
    if not transport.directory(root).same(route["device"], transport.node(root)):
        return False
    # A paired sender may select an addressed chat, never another profile's filesystem path.
    if route["client"] in clients and not (route.get("codex_home") or route.get("claude_home")):
        return True
    return any(messages.matches_participant(route, address, exact=True, root=root) for address in addresses)


class PairedMailPeers:
    """Local peer keys and address grants shared by authenticated mail carriers."""

    def __init__(self, root, peers, *, require_encryption=False):
        self.root = root
        self.devices = transport.directory(root)
        self.local = self.devices.identity_key(self.devices.node)
        self.peers = {}
        self.encrypted_peers = set()
        keys = set()
        if not isinstance(peers, dict) or not peers:
            raise ValueError("Configure at least one authorized mail peer")
        for name, peer in peers.items():
            token = self.devices.identity_key(name)
            if token == self.local or token in self.peers or not isinstance(peer, dict):
                raise ValueError("Ambiguous or invalid session relay peer")
            if "encryption" in peer and peer["encryption"] != "required":
                raise ValueError("Set peer encryption to 'required' or omit it for the carrier default")
            if require_encryption or peer.get("encryption") == "required" or not messages.legacy_broker_allowed(root, name):
                self.encrypted_peers.add(token)
            key = _file(peer, "key_file").read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
                raise ValueError("The pairing key must be 32 random bytes encoded as hex")
            key = bytes.fromhex(key)
            if key in keys:
                raise ValueError("Use a separate pairing key for each peer")
            keys.add(key)
            policy = receiver_policy(root, peer.get("receivers", []), peer.get("receiver_apps", []))
            self.peers[token] = (key, policy)

    def key(self, token):
        if token not in self.peers:
            raise ValueError("Unauthorized session relay peer")
        return self.peers[token][0]

    def allows(self, token, recipient):
        return receiver_allowed(self.root, self.peers[token][1], recipient)


class _Settings(PairedMailPeers):
    def __init__(self, root):
        settings = transport.config(root).get("session_relay")
        if not isinstance(settings, dict) or not isinstance(settings.get("url"), str):
            raise ValueError("Configure session_relay on this device")
        super().__init__(root, settings.get("peers"))
        address = urlsplit(settings["url"])
        if (address.scheme not in ("tls", "nats") or not address.hostname or address.username is not None
                or address.path not in ("", "/") or address.fragment or address.query):
            raise ValueError("session_relay.url must name one broker without embedded credentials")
        self.denied = False
        self.options = {"servers": [settings["url"]], "allow_reconnect": False, "connect_timeout": 5,
                        "max_reconnect_attempts": 1, "reconnect_time_wait": 0,
                        "inbox_prefix": f"_INBOX.xdev.{self.local}"}
        if address.scheme == "tls":
            ca = _file(settings, "ca_file") if "ca_file" in settings else None
            self.options.update(tls=ssl.create_default_context(cafile=ca), tls_handshake_first=True)
        else:
            try:
                loopback = ipaddress.ip_address(address.hostname).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("Use verified TLS for a broker outside the IP loopback interface")
        user = settings.get("user")
        password = _file(settings, "password_file").read_text(encoding="utf-8").rstrip("\r\n")
        if not isinstance(user, str) or not user.strip() or not password or "\n" in password or "\r" in password:
            raise ValueError("Configure separate session relay credentials for each device")
        self.options.update(user=user, password=password)


def _signature(key, topic, body):
    return hmac.digest(key, _json([topic, body]), "sha256").hex()


def _mail_cipher(pair_key, sender, recipient, context):
    # The device identities are shared wire names, independent of the carrier.
    # Derive a distinct key per direction, separated from the legacy pairing MAC.
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESSIV
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise RuntimeError("Encrypted xdev mail requires cryptography with AES-SIV support") from exc
    if len(pair_key) != 32 or not sender or not recipient or sender == recipient:
        raise ValueError("Encrypted mail needs a paired 32-byte key and two distinct device identities")
    if not isinstance(context, bytes) or len(context) > 4096:
        raise ValueError("Invalid encrypted mail transport context")
    route = _json([sender, recipient, context.hex()] if context else [sender, recipient])
    key = HKDF(algorithm=hashes.SHA256(), length=64, salt=SEALED_PREFIX, info=route).derive(pair_key)
    try:
        return AESSIV(key), [SEALED_PREFIX, route]
    except UnsupportedAlgorithm as exc:
        raise RuntimeError("Encrypted xdev mail needs an AES-SIV-capable cryptographic backend") from exc


def seal_mail(pair_key, sender, recipient, plaintext, *, context=b""):
    """Seal a mail frame. Other carriers bind their own stable context on both endpoints."""
    if len(plaintext) + len(SEALED_PREFIX) + 16 > MAX_BYTES:
        raise ValueError("Encrypted session mail exceeds 1 MB")
    cipher, associated = _mail_cipher(pair_key, sender, recipient, context)
    # RFC 5297 deterministic AEAD: the algorithm computes its own synthetic IV.
    # An exact retry has the same opaque digest for broker deduplication. A
    # change to any content, including the letter ID, changes the ciphertext.
    return SEALED_PREFIX + cipher.encrypt(plaintext, associated)


def open_sealed_mail(pair_key, sender, recipient, wire, *, context=b""):
    """Authenticate the route and sealed data before exposing any mail content."""
    if len(wire) > MAX_BYTES or not wire.startswith(SEALED_PREFIX) or len(wire) < len(SEALED_PREFIX) + 16:
        raise ValueError("A complete encrypted xdev mail frame is required")
    cipher, associated = _mail_cipher(pair_key, sender, recipient, context)
    from cryptography.exceptions import InvalidTag
    try:
        return cipher.decrypt(wire[len(SEALED_PREFIX):], associated)
    except InvalidTag as exc:
        raise ValueError("Encrypted xdev mail authentication failed") from exc


def _validate(root, envelope, notice):
    # Validation uses the same versioned identities, callbacks and attachment
    # limits as local mail. No peer-supplied program or URL reaches a receiver.
    messages._envelope(envelope, root=root)
    if envelope.get("envelope_version") != 2:
        raise ValueError("Session relay requires a v2 envelope")
    if envelope.get("reply_to") is not None and envelope.get("kind") not in {"reply", "receipt"}:
        raise ValueError("Correlated relay letters must declare reply or receipt")
    if notice is None:
        return
    keys = {"id", "client", "session", "codex_home", "claude_home", "steer", "force_queue", "idle_only"}
    if not isinstance(notice, dict) or notice.keys() - keys or notice.get("id") != envelope.get("id"):
        raise ValueError("Invalid session notice")
    modes = [notice.get(k, False) for k in ("steer", "force_queue", "idle_only")]
    if any(type(flag) is not bool for flag in modes) or sum(modes) > 1:
        raise ValueError("Invalid session delivery mode")
    endpoint = {k: notice[k] for k in ("client", "session", "codex_home", "claude_home") if k in notice}
    target = messages.normalize_participant({"device": envelope["to"], **endpoint})
    for field in ("recipient_identity", "recipient_endpoint"):
        if not messages.matches_participant(messages.normalize_participant(envelope.get(field)), target,
                                            exact=True, root=root):
            raise ValueError("Notice does not target the addressed chat")
    route = envelope["recipient_endpoint"]
    if notice["client"] != route["client"]:
        raise ValueError("Notice does not use the addressed receiver")
    queue = route.get("delivery") == "queue"
    spec = sessions.descriptor(notice["client"])
    if "force_queue" in notice and (not spec.get("force_queue_option") or notice["force_queue"] != queue):
        raise ValueError("Queue selection disagrees with the addressed receiver")
    mode = delivery_contract.select_delivery(spec, modes[0], queue, modes[2])
    if mode == "unsupported_requires_binding":
        raise ValueError("The addressed receiver cannot admit the requested delivery mode")
    if envelope.get("sender_endpoint") is None or envelope.get("kind") == "receipt":
        raise ValueError("Notification requires an addressed, substantive chat letter")
    sessions.validate(notice["client"], notice["session"], modes[0], modes[2],
                      target.get("codex_home"), target.get("claude_home"))


def _packet(settings, envelope, notice, *, delivery_report=False, context=b""):
    _validate(settings.root, envelope, notice)
    if settings.devices.resolve(envelope["from"]) != settings.devices.node:
        raise ValueError("Session relay sender must be this device")
    target = settings.devices.identity_key(envelope["to"])
    topic = f"{PREFIX}.{target}.{settings.local}"
    # Only the routing header is canonicalized. A forwarded original keeps
    # the addresses, content and digest supplied by its author.
    wire_addresses = messages.address_fields(envelope, settings.devices.identity_key)
    body = {"envelope": {**envelope, **wire_addresses}, "notification": notice}
    if type(delivery_report) is not bool or (delivery_report and envelope.get("kind") == "receipt"):
        raise ValueError("Only a substantive letter can request a delivery report")
    if delivery_report:
        body["request_report"] = True
    if target in settings.encrypted_peers:
        wire = seal_mail(settings.key(target), settings.local, target, _json(body), context=context)
    else:
        wire = _json({**body, "mac": _signature(settings.key(target), topic, body)})
    if len(wire) > MAX_BYTES:
        raise ValueError("Session relay packet exceeds 1 MB")
    return topic, wire


def _unpack(settings, topic, wire, *, context=b""):
    if not isinstance(topic, str) or len(wire) > MAX_BYTES:
        raise ValueError("Invalid session relay packet")
    prefix, _, sender = topic.rpartition(".")
    if prefix != f"{PREFIX}.{settings.local}" or sender not in settings.peers:
        raise ValueError("Delivery does not match an authorized subject")
    if sender in settings.encrypted_peers:
        frame = json.loads(open_sealed_mail(settings.key(sender), sender, settings.local, wire, context=context))
    else:
        if wire.startswith(SEALED_PREFIX):
            raise ValueError("Enable required encryption on both endpoints for this peer")
        frame = json.loads(wire)
        if not isinstance(frame, dict):
            raise ValueError("Invalid session relay frame")
        mac = frame.pop("mac", None)
        if not isinstance(mac, str) or not re.fullmatch("[a-f0-9]{64}", mac) or not hmac.compare_digest(
                _signature(settings.key(sender), topic, frame), mac):
            raise ValueError("Invalid session relay authentication")
    if not isinstance(frame, dict) or set(frame) - {"request_report"} != {"envelope", "notification"}:
        raise ValueError("Invalid session relay frame")
    envelope, notice = frame["envelope"], frame["notification"]
    _validate(settings.root, envelope, notice)
    if "request_report" in frame and (frame["request_report"] is not True or envelope.get("kind") == "receipt"):
        raise ValueError("Invalid delivery report request")
    if (settings.devices.identity_key(envelope["from"]) != sender or
            settings.devices.identity_key(envelope["to"]) != settings.local):
        raise ValueError("Envelope origin or destination disagrees with the authenticated subject")
    return sender, envelope, notice, frame.get("request_report", False)


def _notify_once(root, notice, queue):
    # Reserve durably before calling a host. A crash after reservation can
    # leave an unknown outcome; redelivery must not start a second model turn.
    with closing(sqlite3.connect(root / "state" / "session-relay.sqlite3", timeout=10)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, result TEXT NOT NULL)")
            reservation = {"status": "unknown", "outcome": "unknown", "settle_until": time.time() + 120}
            inserted = db.execute("INSERT OR IGNORE INTO notices VALUES (?, ?)", (
                notice["id"], _json(reservation).decode("utf-8"))).rowcount
        if not inserted:
            row = db.execute("SELECT result FROM notices WHERE id=?", (notice["id"],)).fetchone()
            earlier = json.loads(row[0])
            until = earlier.pop("settle_until", 0)
            if until > time.time():
                earlier["awaiting_native_result"] = True
            return {**earlier, "repeat_suppressed": True}
        try:
            spec = sessions.descriptor(notice["client"])
            mode = delivery_contract.select_delivery(spec, notice.get("steer", False),
                                                     queue, notice.get("idle_only", False))
            native_notice = dict(notice)
            if mode == "send_now" and spec.get("native_send_now_flag") == "steer":
                native_notice["steer"] = True
            if mode == "queue" and spec.get("force_queue_option"):
                native_notice["force_queue"] = True
            native = sessions.notify(root, native_notice)
            receipt = delivery_contract.acceptance(native, mode, spec)
            result = {"status": native.get("status", "unknown"), "delivery_receipt": receipt}
        except Exception:
            result = {"status": "unknown", "outcome": "unknown"}
        with db:
            db.execute("UPDATE notices SET result=? WHERE id=?", (_json(result).decode(), notice["id"]))
    return result


def _receive(settings, source, envelope, notice, request_report=False):
    result = messages.handle(settings.root, "deliver", envelope)
    stored = result["message"]
    item = {"id": result["id"], "seq": result["seq"], "inbox_status": result["status"]}
    if stored["kind"] == "receipt":
        from src.runtime import outbox
        outbox.record_report(settings.root, envelope)
    if notice is None:
        item["session_notification"] = {"status": "not_requested"}
    elif stored.get("withdrawn_at"):
        item["session_notification"] = {"status": "recalled"}
    elif stored.get("read_at"):
        item["session_notification"] = {"status": "already_read"}
    elif not settings.allows(source, stored["recipient_endpoint"]):
        item["session_notification"] = {"status": "requires_receiver_binding"}
    elif stored["kind"] == "reply" and not _permits_reply(settings.root, envelope):
        item["session_notification"] = {"status": "unverified_reply_origin"}
    else:
        queue = stored["recipient_endpoint"].get("delivery") == "queue"
        item["session_notification"] = _notify_once(settings.root, notice, queue)
    if request_report and not item["session_notification"].get("awaiting_native_result"):
        item["_report"] = _report(settings, source, envelope, item)
    return item


def _report(settings, source, envelope, item):
    identifier = report_id(source, envelope["id"])
    notice = item["session_notification"]
    placement = notice.get("delivery_receipt", {}).get("scope", "unknown")
    status = _PLACEMENT_STATUS.get(placement, notice["status"])
    if not valid_progress(status, placement):
        status, placement = "unknown", "unknown"
    data = {"schema": REPORT_SCHEMA, "message_id": envelope["id"], "inbox_status": "received",
            "session_status": status, "placement": placement}
    receipt = {"envelope_version": 2, "kind": "receipt", "id": identifier, "reply_to": envelope["id"],
               "from": settings.local, "to": source, "subject": "xdev delivery report",
               "recipient_identity": envelope.get("sender_endpoint"), "text": _json(data).decode("utf-8")}
    # Freeze the first genuine observation before publishing. A lost ACK or
    # crash must replay exactly the same signed receipt, even if a user has
    # since read the original or a concurrent listener observed a later stage.
    with closing(sqlite3.connect(settings.root / "state" / "session-relay.sqlite3", timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, envelope TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO reports VALUES (?,?)", (identifier, _json(receipt).decode("utf-8")))
        return json.loads(db.execute("SELECT envelope FROM reports WHERE id=?", (identifier,)).fetchone()[0])


def _permits_reply(root, envelope):
    from src.runtime.outbox import permits_reply
    return permits_reply(root, envelope)


class _RelayUnavailable(RuntimeError):
    """Transient connection failure; a sender may already have published."""


class _RelayNotConnected(NotSubmitted, _RelayUnavailable):
    """No JetStream publication was attempted."""


async def _run(root, operation):
    try:
        import nats
        from nats import errors
        from nats.js.errors import ServiceUnavailableError
    except ImportError:
        raise NotSubmitted("Session relay needs the optional nats-py package") from None
    settings = _Settings(root)
    connection = None
    async def report_client_error(exc):
        if isinstance(exc, ssl.SSLError) or "permission" in str(exc).lower() or "authorization" in str(exc).lower():
            settings.denied = True
        logging.getLogger(__name__).debug("Session relay client error: %s", type(exc).__name__)
    try:
        connection = await asyncio.wait_for(nats.connect(**settings.options, error_cb=report_client_error), 8)
        return await operation(settings, connection.jetstream())
    except (errors.Error, TimeoutError, ConnectionError) as exc:
        if settings.denied or isinstance(exc, errors.AuthorizationError):
            raise ValueError("The relay denied this device's credentials or subject permissions") from exc
        transient = (TimeoutError, ConnectionError, errors.ConnectionClosedError, errors.StaleConnectionError,
                     errors.NoServersError, ServiceUnavailableError)
        problem = _RelayUnavailable if isinstance(exc, transient) else RuntimeError
        if problem is _RelayUnavailable and connection is None:
            problem = _RelayNotConnected
        raise problem(f"Session relay operation was not confirmed ({type(exc).__name__})") from exc
    finally:
        if connection is not None:
            await connection.close()


async def _publish(settings, js, envelope, notification, delivery_report=False):
    topic, wire = _packet(settings, envelope, notification, delivery_report=delivery_report)
    return await js.publish(topic, wire, stream=STREAM, headers={"Nats-Msg-Id": hashlib.sha256(wire).hexdigest()})


def publish(root, envelope, notification=None, *, delivery_report=False):
    async def send(settings, js):
        ack = await _publish(settings, js, envelope, notification, delivery_report)
        return {"ok": True, "status": "broker_acknowledged", "id": envelope["id"], "transport": "relay",
                "stream_seq": ack.seq, "duplicate": bool(ack.duplicate), "inbox_status": "unconfirmed",
                "session_accepted": None, "model_status": "unverified"}
    return asyncio.run(_run(root, send))


def _limits(limit, timeout):
    seconds = messages._timeout({"timeout": timeout})
    if seconds <= 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Listen requires a positive timeout and a limit from 1 to 1000")
    return seconds


async def _consumer(settings, js):
    consumer = "xdev-" + settings.local
    info = await js.consumer_info(STREAM, consumer)
    expected_filter = f"{PREFIX}.{settings.local}.*"
    if (info.config.filter_subject != expected_filter or getattr(info.config, "filter_subjects", None)
            or info.config.ack_policy != "explicit" or info.config.deliver_subject):
        raise ValueError("The configured pull consumer must filter only this device and require explicit ACKs")
    return await js.pull_subscribe_bind(stream=STREAM, durable=consumer)


async def _next(settings, js, sub, seconds):
    from nats.errors import TimeoutError as NatsTimeout
    try:
        batch = await sub.fetch(1, timeout=seconds)
    except NatsTimeout:
        if settings.denied:
            raise ValueError("The relay denied access to this device's consumer")
        return None
    msg = batch[0]
    try:
        sender, letter, notice, request_report = _unpack(settings, msg.subject, msg.data)
        item = await asyncio.to_thread(_receive, settings, sender, letter, notice, request_report)
    except (ValueError, UnicodeError, RecursionError, sqlite3.IntegrityError):
        await msg.term()
        return {"status": "rejected", "stream_seq": msg.metadata.sequence.stream}
    if request_report:
        if item["session_notification"].get("awaiting_native_result"):
            # Another listener owns the only native call. It may have a
            # stronger result shortly; don't freeze its in-flight reservation
            # as a definitive unknown. After a crash the reservation expires
            # into a reportable unknown without ever repeating that host call.
            await msg.nak(delay=10)
            return None
        receipt = item.pop("_report")
        await _publish(settings, js, receipt, None)
        item["report_id"] = receipt["id"]
    await msg.ack_sync()
    return item


def listen(root, limit=25, timeout=30):
    seconds = _limits(limit, timeout)

    async def consume(settings, js):
        sub = await _consumer(settings, js)
        results = []
        deadline = time.monotonic() + seconds
        while len(results) < limit and (remaining := deadline - time.monotonic()) > 0:
            item = await _next(settings, js, sub, min(30, remaining))
            if item is not None:
                results.append(item)
        return {"ok": True, "transport": "relay", "status": "completed" if len(results) == limit else "timeout",
                "results": results}
    return asyncio.run(_run(root, consume))


def follow(root, report, limit=25, timeout=30):
    """Keep one outbound subscription. Report each result as it is committed."""
    seconds = _limits(limit, timeout)
    delay = 1

    async def consume(settings, js):
        nonlocal delay
        sub = await _consumer(settings, js)
        while True:
            # Long-poll even when a caller chose a tiny bounded-listen timeout.
            # A healthy connection resets previous failures; never poll a model.
            item = await _next(settings, js, sub, min(30, max(1, seconds)))
            delay = 1
            if item is not None:
                report({"transport": "relay", "status": item.get("inbox_status", item.get("status")), **item})

    async def supervise():
        nonlocal delay
        while True:
            try:
                await _run(root, consume)
            except _RelayUnavailable:
                report({"transport": "relay", "status": "disconnected", "retry_seconds": delay})
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    try:
        asyncio.run(supervise())
    except KeyboardInterrupt:
        return {"ok": True, "transport": "relay", "status": "stopped"}
