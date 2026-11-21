"""Short-lived, sealed session mail over private Amazon SQS FIFO queues."""

import base64
import binascii
from contextlib import closing
import hashlib
import hmac
import ipaddress
import json
import math
import re
import time
from urllib.parse import urlsplit

from src.runtime import session_relay as mail, transport
from src.runtime.mail_routes import NotSubmitted


DOMAIN = b"xdev.sqs-mail.v1\x00"
MAX_RETENTION = 86400
QUEUE_ATTRIBUTES = ["QueueArn", "FifoQueue", "MessageRetentionPeriod",
                    "MaximumMessageSize", "Policy", "RedrivePolicy"]


class Unavailable(RuntimeError):
    """Delivery may have taken place. Retrying uses the original message ID."""


class _QueueNotConnected(NotSubmitted, Unavailable):
    """The queue service was unavailable during pre-submission inspection."""


def _url(value, *, queue=False):
    if not isinstance(value, str) or not value.isascii() or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        raise ValueError("Configure an absolute SQS endpoint and FIFO queue URL")
    u = urlsplit(value)
    if not u.hostname or u.username is not None or u.query or u.fragment or "\\" in value:
        raise ValueError("Invalid SQS URL")
    if u.scheme != "https":
        try:
            local = ipaddress.ip_address(u.hostname).is_loopback
        except ValueError:
            local = False
        if u.scheme != "http" or not local:
            raise ValueError("Use verified HTTPS outside an IP loopback test endpoint")
    if queue and not re.fullmatch(r"/[0-9]{12}/[A-Za-z0-9_-]{1,75}\.fifo", u.path):
        raise ValueError("Set a FIFO queue URL with its account and queue name")
    if not queue and u.path not in ("", "/"):
        raise ValueError("The SQS endpoint URL must have no path")
    return value.rstrip("/")


class Settings(mail.PairedMailPeers):
    def __init__(self, root):
        settings = transport.config(root).get("managed_queue")
        if not isinstance(settings, dict):
            raise ValueError("Configure managed_queue on this device")
        super().__init__(root, settings.get("peers"), require_encryption=True)
        self.region = settings.get("region")
        if not isinstance(self.region, str) or not re.fullmatch("[a-z][a-z0-9-]{1,62}", self.region):
            raise ValueError("Configure the SQS region")
        self.inbox = _url(settings.get("queue_url"), queue=True)
        self.endpoint = _url(settings["endpoint_url"]) if "endpoint_url" in settings else None
        self.urls = {self.devices.identity_key(name): _url(peer.get("queue_url"), queue=True)
                     for name, peer in settings["peers"].items()}
        queues = [self.inbox, *self.urls.values()]
        if len(set(queues)) != len(queues):
            raise ValueError("Assign a distinct inbound FIFO queue to each device")
        own = urlsplit(self.inbox)
        if any((parsed.scheme, parsed.netloc, parsed.path.split("/")[1]) !=
               (own.scheme, own.netloc, own.path.split("/")[1]) for parsed in map(urlsplit, queues)):
            raise ValueError("Use queues at one service endpoint in the same account")
        self.sources = {self.group(token, self.local, self.inbox): token for token in self.peers}

    def group(self, sender, recipient, url):
        peer = recipient if self.local == sender else sender
        route = mail._json([sender, recipient, url])
        return hmac.digest(self.key(peer), DOMAIN + b"group\x00" + route, "sha256").hex()


def _connect(settings):
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError
    except ImportError as exc:
        raise NotSubmitted("The SQS transport requires the optional boto3 SDK") from exc
    try:
        api = boto3.client("sqs", region_name=settings.region, endpoint_url=settings.endpoint,
                           config=Config(connect_timeout=5, read_timeout=28, ignore_configured_endpoint_urls=True,
                                         retries={"mode": "standard", "total_max_attempts": 2}))
    except BotoCoreError as exc:
        raise ValueError(f"Check the SQS endpoint and device credentials ({type(exc).__name__})") from exc
    service = urlsplit(api.meta.endpoint_url)
    queue = urlsplit(settings.inbox)
    if (queue.scheme, queue.netloc) != (service.scheme, service.netloc):
        api.close()
        raise ValueError("All queue URLs must match the configured SQS service endpoint")
    return api


def _call(api, operation, **kwargs):
    from botocore import exceptions
    try:
        return getattr(api, operation)(**kwargs)
    except exceptions.ClientError as exc:
        response = exc.response
        code = str(response.get("Error", {}).get("Code", "invalid_response"))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if status >= 500 or code in {"RequestThrottled", "Throttling", "ThrottlingException", "RequestTimeout"}:
            raise Unavailable(f"SQS {operation} was not confirmed ({code})") from exc
        raise ValueError(f"SQS {operation} failed ({code})") from exc
    except (exceptions.SSLError, exceptions.ProxyConnectionError) as exc:
        raise ValueError("The queue TLS or proxy configuration could not be verified") from exc
    except (exceptions.ConnectionError, exceptions.HTTPClientError) as exc:
        raise Unavailable(f"SQS {operation} was not confirmed ({type(exc).__name__})") from exc
    except exceptions.BotoCoreError as exc:
        raise ValueError(f"Check the device SQS credentials and settings ({type(exc).__name__})") from exc


def _attributes(api, url):
    data = _call(api, "get_queue_attributes", QueueUrl=url, AttributeNames=QUEUE_ATTRIBUTES)["Attributes"]
    path = urlsplit(url).path.strip("/").split("/")
    arn = data.get("QueueArn", "").split(":")
    if (len(arn) != 6 or arn[2] != "sqs" or arn[4:] != path or data.get("FifoQueue") != "true"
            or not 60 <= int(data.get("MessageRetentionPeriod", 0)) <= MAX_RETENTION or data.get("RedrivePolicy")):
        raise ValueError("Use a private FIFO queue with retention of 60–86400 seconds and no dead-letter redrive")
    policy = json.loads(data.get("Policy") or "{}")
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for entry in statements:
        if entry.get("Effect") == "Deny":
            continue
        principal = entry.get("Principal")
        if not isinstance(principal, dict) or set(principal) != {"AWS"}:
            raise ValueError("Use private same-account IAM principals for the mail queue")
        names = principal["AWS"]
        if isinstance(names, str):
            names = [names]
        pattern = rf"arn:{re.escape(arn[1])}:iam::{path[0]}:(?:root|(?:role|user)/[\w+=,.@/-]+)"
        if not isinstance(names, list) or not names or any(
                not isinstance(name, str) or not re.fullmatch(pattern, name) for name in names):
            raise ValueError("Use private same-account IAM principals for the mail queue")
    return int(data["MaximumMessageSize"])


def _context(url):
    return DOMAIN + url.encode("ascii")


def _send(api, settings, letter, notice, delivery_report=False):
    peer = settings.devices.identity_key(letter["to"])
    if peer not in settings.urls:
        raise ValueError("The destination has no paired SQS route")
    url = settings.urls[peer]
    _, wire = mail._packet(settings, letter, notice, delivery_report=delivery_report, context=_context(url))
    body = base64.b64encode(wire).decode("ascii")
    try:
        size_limit = _attributes(api, url)
    except Unavailable as exc:
        raise _QueueNotConnected("SQS was unreachable while checking the queue before submission") from exc
    if len(body) > min(1048576, size_limit):
        raise ValueError("The sealed and encoded message exceeds the configured SQS message size")
    response = _call(api, "send_message", QueueUrl=url, MessageBody=body,
                     MessageGroupId=settings.group(settings.local, peer, url),
                     MessageDeduplicationId=hashlib.sha256(wire).hexdigest())
    if not response.get("MessageId"):
        raise Unavailable("SQS did not confirm the submitted message")
    return response["MessageId"]


def publish(root, envelope, notification=None, *, delivery_report=False):
    settings = Settings(root)
    with closing(_connect(settings)) as api:
        mid = _send(api, settings, envelope, notification, delivery_report)
    return {"ok": True, "transport": "sqs", "status": "queue_acknowledged",
            "id": envelope["id"], "provider_message_id": mid, "inbox_status": "unconfirmed",
            "session_accepted": None, "model_status": "unverified"}


def _next(api, settings, seconds):
    url = settings.inbox
    items = _call(api, "receive_message", QueueUrl=url, MaxNumberOfMessages=1,
                  WaitTimeSeconds=min(20, max(1, math.ceil(seconds))), VisibilityTimeout=180,
                  MessageSystemAttributeNames=["MessageGroupId"]).get("Messages", [])
    if not items:
        return None
    msg = items[0]
    receipt = msg["ReceiptHandle"]
    token = settings.sources.get(msg.get("Attributes", {}).get("MessageGroupId"))
    try:
        if token is None or len(msg["Body"]) > 1048576:
            raise ValueError("Unknown peer or oversize SQS message")
        wire = base64.b64decode(msg["Body"], validate=True)
        topic = f"{mail.PREFIX}.{settings.local}.{token}"
        source, letter, notice, requested = mail._unpack(settings, topic, wire, context=_context(url))
        item = mail._receive(settings, source, letter, notice, requested)
    except (ValueError, binascii.Error, RecursionError):
        # A poison message must not block its FIFO group. Never include its contents in the report.
        item = {"status": "rejected", "provider_message_id": msg.get("MessageId")}
    else:
        if requested and item["session_notification"].get("awaiting_native_result"):
            _call(api, "change_message_visibility", QueueUrl=url, ReceiptHandle=receipt, VisibilityTimeout=10)
            return None
        if requested:
            report = item.pop("_report")
            _send(api, settings, report, None)
            item["report_id"] = report["id"]
    _call(api, "delete_message", QueueUrl=url, ReceiptHandle=receipt)
    return item


def listen(root, limit=25, timeout=30):
    seconds = mail._limits(limit, timeout)
    settings = Settings(root)
    with closing(_connect(settings)) as api:
        _attributes(api, settings.inbox)
        deadline = time.monotonic() + seconds
        results = []
        while len(results) < limit and (remaining := deadline - time.monotonic()) > 0:
            item = _next(api, settings, remaining)
            if item is not None:
                results.append(item)
    return {"ok": True, "transport": "sqs", "status": "completed" if len(results) == limit else "timeout",
            "results": results}


def follow(root, report, limit=25, timeout=30):
    seconds = mail._limits(limit, timeout)
    settings = Settings(root)
    delay = 1
    with closing(_connect(settings)) as api:
        while True:
            try:
                _attributes(api, settings.inbox)
                while True:
                    item = _next(api, settings, seconds)
                    delay = 1
                    if item is not None:
                        report({"transport": "sqs", "status": item.get("inbox_status", item.get("status")), **item})
            except Unavailable:
                report({"transport": "sqs", "status": "disconnected", "retry_seconds": delay})
                time.sleep(delay)
                delay = min(30, 2 * delay)
