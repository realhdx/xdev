"""Mail-only RPC for an SSH key bound to one peer by the receiving account."""

from src.runtime import messages, outbox, session_relay, transport
from src.runtime.device_names import valid_name


OPERATIONS = {("msg", "deliver"), ("ai", "notify"), ("msg", "control")}
_ENVELOPE_FIELDS = ("id", "from", "to", "text", "subject", "kind", "reply_to", "envelope_version",
                    "sender_endpoint", "sender_identity_source", "recipient_identity",
                    "recipient_endpoint", "reply_endpoint", "letter_attachment", "group", "communication_intent")


def _allows(root, peer, endpoint):
    settings = transport.config(root).get("peers", {}).get(peer, {})
    policy = session_relay.receiver_policy(root, settings.get("mail_receivers", []), settings.get("receiver_apps", []))
    return session_relay.receiver_allowed(root, policy, endpoint)


def _notify(root, devices, peer, notice):
    identifier = notice.get("id")
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or not identifier.strip():
        raise ValueError("Invalid message ID")
    stored = messages.handle(root, "wait", {"id": identifier, "timeout": 0}).get("message")
    if (not stored or not devices.same(stored["from"], peer) or not devices.same(stored["to"], devices.node)
            or stored.get("recipient_endpoint_source") != "envelope"):
        raise ValueError("No addressed letter from the bound peer matches this notification")
    if stored.get("withdrawn_at"):
        return {"ok": False, "status": "recalled", "submitted": False}
    envelope = {name: stored[name] for name in _ENVELOPE_FIELDS if stored.get(name) is not None}
    session_relay._validate(root, envelope, notice)
    endpoint = envelope["recipient_endpoint"]
    if not _allows(root, peer, endpoint):
        return {"ok": False, "status": "requires_receiver_binding"}
    if stored["kind"] == "reply" and not outbox.permits_reply(root, envelope):
        return {"ok": False, "status": "unverified_reply_origin"}
    if stored["read_at"]:
        return {"ok": True, "status": "already_read"}
    result = session_relay._notify_once(root, notice, endpoint.get("delivery") == "queue")
    receipt = result.get("delivery_receipt", {})
    if receipt.get("scope", "unknown") == "unknown":
        if result.get("status") in {"requires_activation", "requires_receiver_binding", "deferred_in_inbox"}:
            return {"ok": False, "status": result["status"]}
        return {"ok": False, "status": "unknown", "outcome": "unknown"}
    # Return only observations matched by the adapter's acceptance rules. Do
    # not expose the host's raw diagnostics, profile state or session history.
    response = {"ok": True, "status": result["status"]}
    for path, value in receipt["evidence"].items():
        target = response
        names = path.split(".")
        for component in names[:-1]:
            target = target.setdefault(component, {})
        target[names[-1]] = value
    return response


def receive(root, bound_peer, frame):
    """The bound peer is fixed by the local forced command, never by the frame."""
    devices = transport.directory(root)
    peer = devices.resolve(bound_peer)
    if not valid_name(bound_peer) or peer == devices.node:
        raise ValueError("The mailbox SSH key must be bound to a paired remote device")
    if (not isinstance(frame, dict) or set(frame) != {"service", "action", "expected_device", "payload"}
            or not valid_name(frame["expected_device"]) or not devices.same(frame["expected_device"], devices.node)):
        raise ValueError("A mailbox request must explicitly target this device")
    payload = frame["payload"]
    if (not isinstance(payload, dict) or not isinstance(frame["service"], str) or not isinstance(frame["action"], str)
            or (frame["service"], frame["action"]) not in OPERATIONS):
        raise ValueError("This SSH endpoint accepts only mailbox delivery and addressed notifications")
    if frame["service"] == "ai":
        return _notify(root, devices, peer, payload)
    if frame["action"] == "control":
        from src.runtime import mail_control_ssh
        return mail_control_ssh.receive(root, payload, bound_peer=peer)
    if not devices.same(payload.get("from"), peer) or not devices.same(payload.get("to"), devices.node):
        raise ValueError("Letter endpoints disagree with the authenticated SSH peer and recipient")
    session_relay._validate(root, payload, None)
    delivered = messages.handle(root, "deliver", payload)
    # Confirm the copy without revealing any subsequent local annotation.
    copy = {key: delivered["message"][key] for key in ("id", "text", "subject", "letter_attachment", "group")}
    if delivered["message"].get("communication_intent"):
        copy["communication_intent"] = delivered["message"]["communication_intent"]
    return {"ok": True, "id": delivered["id"], "status": delivered["status"], "message": copy}
