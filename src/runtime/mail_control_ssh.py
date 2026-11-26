"""Narrow non-prompting control RPC on an already paired SSH route."""

from contextlib import closing
import hmac
import json
import re
import secrets
import sqlite3
import time

from src.runtime import device_names, messages, session_relay, transport


def _pair(root, peer, provider=None):
    config = transport.config(root)
    directory = transport.directory(root)
    remote = directory.identity_key(peer)
    for name, section in (("github", device_names.github_mailbox_config(config)), ("relay", config.get("session_relay")),
                          ("sqs", config.get("managed_queue"))):
        if provider not in (None, name) or not isinstance(section, dict):
            continue
        peers = section.get("peers", {})
        if not isinstance(peers, dict) or not any(directory.same(alias, peer) for alias in peers):
            continue
        settings = session_relay.PairedMailPeers(root, peers, require_encryption=True)
        return name, settings.key(remote), settings
    raise ValueError("No authenticated mailbox control route for this peer.")


def _mac(key, frame):
    raw = json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.digest(key, b"xdev.mail-control.ssh.v1\0" + raw, "sha256").hex()


def call(root, peer, payload, *, timeout=12, direct=False):
    directory = transport.directory(root)
    envelope = dict(payload)
    for key in ("actor", "target"):
        if envelope.get(key):
            envelope[key] = messages.mapped_endpoint(envelope[key], directory.identity_key)
    if directory.rpc_profile(peer) == "linux-mailbox":
        wire = {"command": envelope}
    else:
        name, key, _ = _pair(root, peer)
        content = {"from": directory.identity_key(directory.node), "to": directory.identity_key(peer),
                   "provider": name, "expires": int(time.time()) + 90, "nonce": secrets.token_hex(16), "command": envelope}
        wire = {**content, "mac": _mac(key, content)}
    return transport.peer_request(peer, {"service": "msg", "action": "control", "expected_device": directory.resolve(peer),
                                        "payload": wire}, timeout=timeout, root=root, direct=direct)


def receive(root, wire, *, bound_peer=None):
    directory = transport.directory(root)
    if not isinstance(wire, dict) or not isinstance(wire.get("command"), dict):
        raise ValueError("Malformed mailbox control request.")
    if bound_peer is not None:
        if set(wire) != {"command"}:
            raise ValueError("Invalid peer-bound mailbox control request.")
        peer = directory.identity_key(bound_peer)
        grant = None
    else:
        if set(wire) != {"from", "to", "provider", "expires", "nonce", "command", "mac"}:
            raise ValueError("Authenticated mailbox control proof required.")
        peer = directory.identity_key(wire["from"])
        stamp = int(time.time())
        # Let a peer whose clock runs ahead sample delivery evidence. Never accept an
        # expired proof; nonces remain reserved through its signed expiration.
        max_future = 180 if wire["command"].get("op") in ("status", "preflight") else 120
        if (not directory.same(wire["to"], directory.node) or directory.same(peer,directory.node)
                or type(wire["expires"]) is not int or not stamp <= wire["expires"] <= stamp + max_future
                or not isinstance(wire["nonce"], str) or not re.fullmatch("[0-9a-f]{32}", wire["nonce"])
                or not isinstance(wire["mac"], str) or not re.fullmatch("[0-9a-f]{64}", wire["mac"])
                or wire["provider"] not in ("github", "relay", "sqs")):
            raise ValueError("Invalid control proof identity or expiration.")
        _, key, grant = _pair(root, peer, wire["provider"])
        expected = _mac(key, {k:v for k,v in wire.items() if k != "mac"})
        if not hmac.compare_digest(expected, wire["mac"]):
            raise ValueError("Invalid paired mailbox control signature.")
        state = root / "state"
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(state / "mail-control-nonces.sqlite3", timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS accepted(peer TEXT, nonce TEXT, expires INTEGER, PRIMARY KEY(peer,nonce))")
            db.execute("DELETE FROM accepted WHERE expires < ?",(stamp,))
            if not db.execute("INSERT OR IGNORE INTO accepted VALUES(?,?,?)",(peer,wire["nonce"],wire["expires"])).rowcount:
                raise ValueError("Control proof already used; query status before retrying.")
    command = dict(wire["command"])
    target = messages.normalize_participant(command.pop("target", None))
    if not target or not directory.same(target["device"], directory.node):
        raise ValueError("Target chat does not belong to this device.")
    from src.runtime import ssh_mail
    allowed = grant.allows(peer, target) if grant else ssh_mail._allows(root, bound_peer, target)
    if not allowed:
        raise ValueError("This paired peer has no permission for the addressed chat.")
    if command.get("op") == "preflight":
        from src.runtime import mail_contact
        if set(command) - {"actor", "op"}:
            raise ValueError("Peer has no grant to inspect this addressed chat.")
        return {"ok": True, "observation": mail_contact.local_probe(root,target)}
    from src.runtime import mail_lifecycle
    return mail_lifecycle.apply(root, command, authenticated_peer=peer, expected_target=target)
