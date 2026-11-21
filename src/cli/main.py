"""CLI and SSH entry point of CrossDeviceCollab."""

import argparse
import hashlib
from functools import partial
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from src.runtime import commands, transport
from src.runtime.device_names import REPORT_CARRIERS
from src.adapters import sessions
from src.adapters.delivery_contract import select_delivery, acceptance
from src.cli import contact


def _request(root, service, action, payload):
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")
    if service == "exec" and action == "run":
        result = commands.run(payload)
    elif service == "job" and action in {"start", "status", "log", "cancel"}:
        from src.runtime import jobs
        result = jobs.handle(root / "state", action, payload)
    elif service == "msg" and action in {"deliver", "list", "wait", "receipt", "publish", "listen"}:
        from src.runtime import messages
        if action == "deliver" and not transport.directory(root).same(payload.get("to"), transport.node(root)):
            raise ValueError("Recipient does not match this device")
        result = messages.handle(root, action, payload)
    elif service == "system" and action == "who":
        result = {**transport.directory(root).describe(), "home": str(root)}
    elif service == "surface":
        from src.runtime import surfaces
        result = surfaces.handle(root, action, payload)
    elif service == "ai" and action == "notify":
        from src.adapters import sessions
        result = sessions.notify(root, payload)
    else:
        raise ValueError("Unknown operation")
    return {"ok": True, **result}


def _call(root, host, service, action, payload, timeout=60, *, direct=False):
    if host == "local" or transport.resolve(root, host) == transport.node(root):
        return _request(root, service, action, payload)
    if service == "msg":
        from src.runtime import messages
        payload = messages.mail_payload(root, payload)
    message = {"service": service, "action": action, "payload": payload, "expected_device": transport.resolve(root, host)}
    if direct:
        return transport.peer_request(host, message, timeout, root=root, direct=True)
    return transport.peer_request(host, message, timeout, root=root)


def _emit(result):
    from src.runtime.cli_result import collecting
    if collecting():
        return result
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _error(exc):
    return {"ok": False, "error_code": type(exc).__name__, "error_message": str(exc)}


def _serve_one(root, *, mailbox_sender=None):
    try:
        raw = sys.stdin.buffer.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Request exceeds 1 MB")
        message = json.loads(raw.decode("utf-8"))
        if not isinstance(message, dict):
            raise ValueError("Expected a JSON object")
        if mailbox_sender is not None:
            from src.runtime import ssh_mail
            return _emit(ssh_mail.receive(root, mailbox_sender, message))
        if "expected_device" in message and not transport.directory(root).same(message["expected_device"], transport.node(root)):
            raise ValueError("The selected SSH route reached a different xdev device; check pairing")
        service, action = message.get("service"), message.get("action")
        if service == "surface":
            from src.runtime import surfaces
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Expected a surface request object")
            return _emit(surfaces.handle(root, action, payload, remote=True))
        if service == "msg" and action in {"publish", "listen"}:
            raise ValueError("Broker setup and actions are local only")
        if service == "msg" and action == "control":
            from src.runtime import mail_control_ssh
            return _emit(mail_control_ssh.receive(root, message.get("payload")))
        return _emit(_request(root, service, action, message.get("payload", {})))
    except (OSError, ValueError, RuntimeError) as exc:
        return _emit(_error(exc))


def _host(parser):
    parser.add_argument("--host", default="local", help="A paired device name or alias, or local")


def _command(parser):
    _host(parser)
    parser.add_argument("--cwd", help="Working directory on the target")
    parser.add_argument("--wsl", nargs="?", const="Ubuntu", help="Run in the specified WSL distribution")
    parser.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- EXECUTABLE ARGS")


def _parser(parser_class=argparse.ArgumentParser):
    parser = parser_class(prog="xdev", description="Exchange messages between named AI sessions and operate paired devices")
    product = json.loads((Path(__file__).resolve().parents[1] / "product.json").read_text(encoding="utf-8"))
    parser.add_argument("--version", action="version",
                        version=f"{product['product']} {product['version']} ({product['series']})")
    sub = parser.add_subparsers(dest="service", required=True)
    from src.runtime import surfaces
    surfaces.add_parser(sub)
    sub.add_parser("file", help="Remote file operations. See xdev file --help")
    sub.add_parser("mcp", help="Run the optional local MCP stdio adapter")
    who = sub.add_parser("who", help="Confirm the target device")
    _host(who)
    exe = sub.add_parser("exec", help="Run a short foreground command")
    exe.add_argument("--timeout", type=float, default=60)
    _command(exe)

    job = sub.add_parser("job", help="Start and inspect jobs owned by the target machine")
    actions = job.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start")
    start.add_argument("--id")
    _command(start)
    for name in ("status", "log", "cancel"):
        p = actions.add_parser(name)
        _host(p)
        p.add_argument("id")
        if name == "log":
            p.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
            p.add_argument("--offset", type=int, default=0)
            p.add_argument("--limit", type=int, default=32768)

    msg = sub.add_parser("msg", help="Deliver and receive messages independently of commands")
    actions = msg.add_subparsers(dest="action", required=True)
    send = actions.add_parser("send", description=(
        "Send to a device inbox. To notify an AI session on that device, supply both "
        "--client and --session. Admission/queue acceptance does not prove model processing."))
    forward = actions.add_parser("forward", help="Send your comment with a copy of a received letter")
    forward.add_argument("message_id")
    send.add_argument("--forward-of", help="Forward this received letter through the standard send action")
    for command in (send, forward):
        command.add_argument("--intent", choices=contact.INTENTS,
                             help="Describe whether this letter informs, invites, requests a reply or requests action")
        command.add_argument("--quote", help="On a forward, mark a verbatim excerpt of the original")
        command.add_argument("--excerpt-only", action="store_true", help="Include only the selected excerpt")
        command.add_argument("--to", help="Recipient device name or alias; optional for a routed --reply-to")
        command.add_argument("--id")
        command.add_argument("--subject", help="Short topic. The original topic or a short body preview is used by default")
        command.add_argument("--from-client", help="Sending client when its host supplies no chat identity")
        command.add_argument("--from-session", help="Stable ID of the sending chat on this device")
        command.add_argument("--from-profile", help="Absolute profile directory when the chat address uses one")
        command.add_argument("--as-device", action="store_true", help="Send as a device program without a chat identity")
        command.add_argument("--reply-to")
        command.add_argument("--kind", choices=("message", "reply", "receipt"), default="message")
        command.add_argument("--return-client", type=sessions.client_name)
        command.add_argument("--return-session", help="Callback chat to notify on substantive replies")
        command.add_argument("--return-host", help="Return device name or alias (default: this device)")
        command.add_argument("--return-codex-home", help="Absolute Codex home for the return chat")
        command.add_argument("--return-claude-home", help="Absolute Claude home for the return chat")
        command.add_argument("--return-queue", action="store_true",
                          help="Allow delayed native replies at this return chat")
        command.add_argument("--one-way", action="store_true", help="Explicitly send an AI notice without a return chat")
        command.add_argument("--wait-reply", type=float, metavar="SECONDS",
                          help="On a first message, return the peer's substantive reply in this same call (0-120 seconds)")
        command.add_argument("--delivery-report", action="store_true", help="Request a silent report of inbox delivery and host admission")
        command.add_argument("--transport", choices=("auto", "direct", "ssh", "nats", *REPORT_CARRIERS), default="auto")
        command.add_argument("--client", type=sessions.client_name,
                          help="Notify this AI client on the recipient device after inbox delivery; requires --session")
        command.add_argument("--session", help="Existing AI session ID on the recipient device; requires --client")
        command.add_argument("--codex-home", help="Existing absolute Codex home on the recipient device; only --client codex")
        command.add_argument("--claude-home", help="Claude config home on the recipient device containing sessions/; only --client claude")
        ai_delivery = command.add_mutually_exclusive_group()
        ai_delivery.add_argument("--steer", action="store_true", help="DSH/OpenCode: deliver to the current turn at its next native step (default for these clients)")
        ai_delivery.add_argument("--queue", action="store_true", help="Explicitly wait for the next turn instead of current-turn delivery")
        ai_delivery.add_argument("--idle-only", action="store_true", help="DSH/OpenCode: defer when the target session is active or unverified")
        source = command.add_mutually_exclusive_group()
        source.add_argument("--text")
        source.add_argument("--text-file", type=Path)
        source.add_argument("--stdin", action="store_true")
        command.add_argument("--also", action="append", nargs=3, default=[], metavar=("DEVICE", "CLIENT", "SESSION"),
                             help="Also deliver a separate copy to this discussion member (repeatable)")
        command.add_argument("--also-profile", action="append", nargs=4, default=[],
                             metavar=("DEVICE", "CLIENT", "SESSION", "PROFILE"),
                             help="Address an additional chat in a specific profile")
    reply = actions.add_parser("reply", help="Reply to one message; its saved address supplies the destination")
    reply.add_argument("message_id")
    reply.add_argument("--text", required=True)
    reply.add_argument("--intent", choices=contact.INTENTS)
    reply.add_argument("--subject", help="Change the topic of this reply")
    reply.add_argument("--id", help="Stable ID for this reply")
    reply.add_argument("--delivery-report", action="store_true")
    reply.add_argument("--transport", choices=("auto", "direct", "ssh", "nats", *REPORT_CARRIERS))
    reply.add_argument("--from-client")
    reply.add_argument("--from-session")
    reply.add_argument("--from-profile")
    reply.add_argument("--audience", choices=("callback", "sender", "original-author", "authors", "all"),
                       default="callback", help="Use the stored return address, sender, original author, both authors, or discussion")
    status = actions.add_parser("status", help="Check the route and any delivery report for your outgoing message")
    status.add_argument("id")
    status.add_argument("--as-client")
    status.add_argument("--as-session")
    status.add_argument("--as-profile")
    status.add_argument("--as-device", action="store_true", help="For a message sent by a device program")
    contact.arguments(actions, status)
    actions.add_parser("whoami", help="Show the calling chat and how it was identified")
    bind = actions.add_parser("bind", help="CAS-add a callback to one existing local legacy message")
    bind.add_argument("id")
    bind.add_argument("--return-host", required=True)
    bind.add_argument("--return-client", type=sessions.client_name, required=True)
    bind.add_argument("--return-session", required=True)
    bind.add_argument("--return-codex-home")
    bind.add_argument("--return-claude-home")
    bind.add_argument("--return-queue", action="store_true")
    bind.add_argument("--client", type=sessions.client_name)
    bind.add_argument("--session")
    bind.add_argument("--codex-home")
    bind.add_argument("--claude-home")
    for name in ("inbox", "wait", "receipt"):
        p = actions.add_parser(name, aliases={"inbox": ["list"], "receipt": ["read"]}.get(name, []))
        _host(p)
        p.add_argument("--as-client", help="Identify this caller's client when the host supplies no identity")
        p.add_argument("--as-session", help="Stable ID of this caller's chat on its own device")
        p.add_argument("--as-profile", help="Absolute profile directory when the chat address uses one")
        if name == "receipt":
            p.add_argument("id")
            continue
        p.add_argument("--device-inbox", action="store_true", help="Explicitly inspect the device-wide inbox")
        p.add_argument("--from-client", help="Filter by author; use with --from-session")
        p.add_argument("--from-session")
        p.add_argument("--from-profile")
        p.add_argument("--to-client", help="Filter by recipient; use with --to-session")
        p.add_argument("--to-session")
        p.add_argument("--to-profile")
        p.add_argument("--after", "--after-seq", type=int,
                       help="Resume after this inbox sequence; use the cursor from the previous result")
        p.add_argument("--from", dest="sender", help="Filter mail by the sender's registered device name or alias")
        p.add_argument("--reply-to")
        p.add_argument("--group-id", help="Only letters in this discussion")
        p.add_argument("--subject", help="Find a literal substring in the topic")
        p.add_argument("--substantive-only", action="store_true", help="Only replies to a message, excluding pure receipts")
        p.add_argument("--unread-only", action="store_true", help="Return messages without an explicit read receipt")
        if name == "wait":
            p.add_argument("--id")
            p.add_argument("--timeout", type=float, default=30)
        else:
            p.add_argument("--limit", type=int, default=25)
            p.add_argument("--wait", dest="inbox_wait", type=float, metavar="SECONDS",
                           help="Wait once (0–60s) for this local chat's mail summaries. Starts with unread mail; "
                                "resume with --after-seq CURSOR. Excludes pure receipts. Limit 1–100.")
    listen = actions.add_parser("listen", help="Receive mail from the selected relay or private queue")
    listen.add_argument("--timeout", type=float, default=30)
    listen.add_argument("--limit", type=int, default=25)
    listen.add_argument("--transport", choices=("nats", *REPORT_CARRIERS), default="nats")
    listen.add_argument("--follow", action="store_true", help="Keep receiving relay mail until interrupted; emit one JSON event at a time")
    events = actions.add_parser("events", help="Read committed local mailbox arrival events without reading letters")
    events.add_argument("--transport", choices=("github",), default="github")
    events.add_argument("--after", type=int, default=0, help="Resume after next_after from the previous event listing")
    events.add_argument("--limit", type=int, default=25)
    subscriber = actions.add_parser("host-events", help="Stream new authenticated mail events to a local host plugin over its pipes")
    subscriber.add_argument("--peer", required=True, help="Paired sender device")
    subscriber.add_argument("--client", required=True, help="Local host application")
    subscriber.add_argument("--session", required=True, help="Existing original chat owned by the host")
    subscriber.add_argument("--profile", help="Local profile bound to that chat")
    ai = sub.add_parser("ai", help="Notify an existing local AI session about a received message")
    ai.add_argument("--client", type=sessions.client_name,
                    help="AI client (default: opencode); Codex uses its official queue without --resume")
    ai.add_argument("--session", help="Existing local client session ID; DSH also accepts its native session-UUID")
    ai.add_argument("--codex-home", help="Existing Codex home; defaults to XDEV_CODEX_HOME, CODEX_HOME or ~/.codex")
    ai.add_argument("--claude-home", help="Existing target Claude config home containing sessions/")
    ai_action = ai.add_mutually_exclusive_group()
    ai_action.add_argument("--capabilities", action="store_true", help="Show static adapter contracts without probing hosts or sending")
    ai_action.add_argument("--discover", action="store_true", help="Find existing local chat addresses without reading messages or starting a model")
    ai_action.add_argument("--register", action="store_true", help="Register this Codex executor's own native receiver; never a different chat")
    delivery = ai.add_mutually_exclusive_group()
    delivery.add_argument("--steer", action="store_true", help="DSH/OpenCode: current-turn delivery at its next native step (default)")
    delivery.add_argument("--queue", action="store_true", help="Explicitly wait for the next turn")
    delivery.add_argument("--idle-only", action="store_true", help="DSH/OpenCode: defer when observed active")
    ai.add_argument("id", nargs="?", help="Local inbox message ID")
    return parser


def _args(values):
    argv = values.pop("argv")
    values["argv"] = argv[1:] if argv[:1] == ["--"] else argv
    return values


def _register_caller(root, *, allow_cli_env=True):
    """Use only this call's host context, or this actual CLI executor's own env."""
    from src.adapters import caller_context, codex_receiver
    from src.runtime.cli_result import collecting
    caller = caller_context.current()
    if caller["status"] == "ready" and caller["client"] == "codex":
        return codex_receiver.register(root, caller["session"])
    if allow_cli_env and not collecting() and os.environ.get("CODEX_THREAD_ID"):
        with caller_context.bind({"threadId": os.environ["CODEX_THREAD_ID"]}):
            own = caller_context.current()
            if own["status"] == "ready":
                return codex_receiver.register(root, own["session"], source="own_cli_host")
    return {"ok": False, "status": "not_registered", "error_code": "CODEX_CALLER_UNIDENTIFIED"}


def _address(device, client, session, profile=None):
    from src.runtime import messages
    if client is None and session is None and profile is None:
        return None
    if not client or not session:
        raise ValueError("Supply both client and session to identify a chat")
    address = {"device": device, "client": client, "session": session}
    if profile is not None:
        key = sessions.ADAPTERS.get(client, {}).get("profile_field")
        if not key:
            raise ValueError("This client's chat identity has no profile directory")
        address[key] = profile
    return messages.normalize_participant(address)


def _actor(origin, client=None, session=None, *, as_device=False, profile=None):
    from src.adapters import caller_context
    from src.runtime.cli_result import collecting
    declared = _address(origin, client, session)
    caller = caller_context.current()
    if caller["status"] == "conflict" or caller["source"] == "invalid_mcp_meta":
        raise ValueError("The host supplied conflicting or invalid chat identity")
    actual, source = None, "unknown"
    if caller["status"] == "ready":
        actual = _address(origin, caller["client"], caller["session"])
        source = "cli_host" if caller["source"] in {"official_channel_hook", "dsh_host_tool"} else "mcp_metadata"
    elif not collecting() and os.environ.get("CODEX_THREAD_ID"):
        actual = _address(origin, "codex", os.environ["CODEX_THREAD_ID"])
        source = "cli_host"
    if as_device:
        if declared or actual:
            raise ValueError("--as-device is reserved for a device program without a chat identity")
        return None, "unknown"
    if actual and declared and actual != declared:
        raise ValueError("Declared chat conflicts with the calling host's chat")
    actor = actual or declared
    if profile is not None:
        actor = _address(origin, (actor or {}).get("client"), (actor or {}).get("session"), profile)
    return actor, source if actual else ("explicit" if declared else "unknown")


def _return_endpoint(root, args, origin, actor=None):
    from src.runtime import messages
    if args.wait_reply is not None:
        if not 0 <= args.wait_reply <= 120:
            raise ValueError("--wait-reply must be within 0 to 120 seconds")
        if (args.kind != "message" or args.reply_to or args.one_way or args.return_client or
                args.return_session or args.return_host or args.return_codex_home or args.return_claude_home or args.return_queue):
            raise ValueError("Use --wait-reply on an initial message as its sole return route")
        return {"device": origin, "channel": "tool_result", "request_id": args.id}
    client, session = args.return_client, args.return_session
    explicit = client is not None or session is not None or args.return_host is not None
    if args.one_way:
        if explicit or args.return_codex_home or args.return_claude_home or args.return_queue:
            raise ValueError("--one-way conflicts with an explicit return address")
        return None
    if explicit and (client is None or session is None):
        raise ValueError("Return routing requires --return-client and --return-session")
    if not explicit and actor and sessions.receiver_bound(root, actor["client"], actor["session"]):
        from src.adapters import caller_context
        client = caller_context.current().get("client") or args.from_client or actor["client"]
        session = actor["session"]
    if client is None:
        if args.return_codex_home or args.return_claude_home or args.return_queue:
            raise ValueError("Return profile requires an identified return chat")
        return None
    device = transport.resolve(root, args.return_host) if args.return_host is not None else origin
    endpoint = {"device": device, "client": client, "session": session}
    if not explicit and actor:
        endpoint.update({k: actor[k] for k in ("codex_home", "claude_home") if k in actor})
    if args.return_queue or (not explicit and sessions.descriptor(client).get("backend") == "official_mcp_channel"):
        endpoint["delivery"] = "queue"
    endpoint.update({key: value for key, value in (
        ("codex_home", args.return_codex_home), ("claude_home", args.return_claude_home)) if value is not None})
    return messages.normalize_reply_endpoint(endpoint)


def _return_readiness(root, endpoint):
    """Describe what the sender knows about the callback without waking a model."""
    if not endpoint:
        return {"return_address_known": False, "bidirectional_ready": False, "return_session_status": "no_address"}
    if endpoint.get("channel") == "tool_result":
        return {"return_address_known": True, "bidirectional_ready": True, "return_session_status": "bounded_wait"}
    result = {"return_address_known": True, "bidirectional_ready": None, "return_session_status": "address_only"}
    if not transport.directory(root).same(endpoint["device"], transport.node(root)):
        return result
    client = endpoint["client"]
    if client == "dsh":
        from src.adapters import dsh
        try:
            binding = dsh.receiver_binding(root, endpoint["session"])
        except (OSError, ValueError) as exc:
            return {**result, "return_session_status": "binding_unverified", "return_diagnostic": str(exc)}
        if binding.get("binding_state", "").startswith("host_checks_"):
            return {**result, "return_session_status": "host_resolves_on_delivery"}
        result["bidirectional_ready"] = None if binding["bound"] else False
        result["return_session_status"] = "receiver_registered" if binding["bound"] else "requires_receiver_binding"
        if not binding["bound"]:
            result.update(return_error_code=binding["error_code"], return_next_action=binding["next_action"])
    elif client not in sessions.ADAPTERS:
        if sessions.receiver_bound(root, client, endpoint["session"]):
            result["return_session_status"] = "host_resolves_on_delivery"
        else:
            result.update(bidirectional_ready=False, return_session_status="no_host_receiver")
    return result


def _relay_carrier(name):
    if name == "github":
        from src.runtime import github_mail
        return github_mail
    if name == "sqs":
        from src.runtime import managed_queue
        return managed_queue
    from src.runtime import session_relay
    return session_relay


def _extensions_received(delivered, attachment, group, root=None):
    if not delivered.get("ok") or (attachment is None and group is None):
        return True
    copy = delivered.get("message") or {}
    if attachment is not None and (copy.get("letter_attachment") or {}).get("sha256") != attachment["sha256"]:
        return False
    if group is None or copy.get("group") == group:
        return True
    if not isinstance(copy.get("group"), dict):
        return False
    from src.runtime.correspondence import plan_group
    try:
        return plan_group(copy["group"], root) == plan_group(group, root)
    except (TypeError, ValueError, KeyError):
        return False


def _send(root, args, *, attachment=None, group=None, linked_to=None):
    from src.runtime import messages
    from src.runtime.subjects import normalize_subject, preview_subject
    origin = transport.node(root)
    if args.id is None:
        args.id = uuid4().hex
    target = transport.resolve(root, args.to) if args.to is not None else None
    author, identity_source = _actor(origin, args.from_client, args.from_session, as_device=args.as_device,
                                     profile=args.from_profile)
    return_endpoint = _return_endpoint(root, args, origin, author)
    client, session, codex_home = args.client, args.session, args.codex_home
    claude_home = args.claude_home
    route = None
    if args.reply_to:
        route = messages.resolve_reply_route(root, args.reply_to, kind=args.kind, recipient=target)
        if not route["ok"]:
            # No blind fallback to a device inbox for a purported conversation reply.
            return {**route, "id": args.id, "inbox_status": "not_attempted",
                    "session_status": "not_attempted", "session_accepted": False}
        expected = route.get("original_recipient_identity")
        if expected and (author or route.get("original_envelope_version", 1) >= 2):
            if not messages.matches_participant(author, expected, root=root):
                return {"ok": False, "id": args.id, "status": "requires_sender_identity",
                        "error_code": "REPLY_SENDER_MISMATCH" if author else "REPLY_SENDER_REQUIRED",
                        "error_message": "Reply from the addressed chat. Supply --from-client/--from-session for an unbound caller and --from-profile for a profile-qualified address.",
                        "inbox_status": "not_attempted", "session_accepted": False}
        if route.get("notify_endpoint"):
            endpoint = route["notify_endpoint"]
            if client is not None or session is not None:
                explicit_endpoint = messages.normalize_reply_endpoint({
                    "device": target or endpoint["device"], "client": client or endpoint["client"],
                    "session": session or endpoint["session"],
                    **({"codex_home": codex_home} if codex_home else {}),
                    **({"claude_home": claude_home} if claude_home else {}),
                })
                if any(explicit_endpoint[key] != endpoint[key] for key in ("client", "session")):
                    raise ValueError("Explicit target chat conflicts with the stored reply route")
            for key, explicit in (("codex_home", codex_home), ("claude_home", claude_home)):
                if explicit is not None and messages.profile_key(explicit) != messages.profile_key(endpoint.get(key)):
                    raise ValueError("Explicit target profile conflicts with the stored reply route")
            target, client, session = endpoint["device"], endpoint["client"], endpoint["session"]
            if endpoint.get("delivery") == "queue" and not (args.steer or args.idle_only):
                args.queue = True
            codex_home, claude_home = endpoint.get("codex_home"), endpoint.get("claude_home")
            inherited = route.get("return_endpoint")
            if not (args.one_way or args.return_client):
                return_endpoint = inherited or return_endpoint
        elif route["status"] == "tool_result":
            if client or session or codex_home or claude_home or args.steer or args.queue or args.idle_only:
                raise ValueError("A waiting caller receives replies through its original tool call")
            if args.transport == "nats":
                raise ValueError("A tool-result reply needs confirmed device delivery; use auto or ssh")
            target = route["target_device"]
            inherited = route["return_endpoint"]
            if not (args.one_way or args.return_client):
                return_endpoint = inherited or return_endpoint
        else:
            # Receipts deliberately do not wake a model, including when flags were supplied.
            if client is not None or session is not None or args.steer or args.queue or args.idle_only:
                raise ValueError("Receipt traffic cannot request an AI wake")
            original = messages.handle(root, "wait", {"id": args.reply_to, "timeout": 0}).get("message")
            if original is None:
                raise ValueError("Receipt source is not in this device's inbox")
            callback = original.get("reply_endpoint")
            receipt_target = callback["device"] if callback else original["from"]
            if target is not None and not transport.directory(root).same(target, receipt_target):
                raise ValueError("Receipt destination conflicts with its original message")
            target = receipt_target
            return_endpoint = None
    if target is None:
        raise ValueError("Provide --to or a --reply-to with a stored callback")
    target = transport.resolve(root, target)
    if args.transport == "auto" and target != origin:
        args.transport = transport.directory(root).mail_transport(target)
    if args.reply_to and args.kind == "message" and (args.transport in ("auto", "direct", *REPORT_CARRIERS) or
            transport.directory(root).rpc_profile(target) == "linux-mailbox"):
        # The legacy send --reply-to form has already resolved a local source and callback.
        args.kind = "reply"
    if return_endpoint:
        return_endpoint = {**return_endpoint, "device": transport.resolve(root, return_endpoint["device"])}
    idle_only = args.idle_only
    notify = client is not None or session is not None or codex_home is not None or claude_home is not None or args.steer or idle_only or args.queue
    spec, requested_mode, steer = None, None, False
    if notify:
        if client is None or session is None:
            raise ValueError("AI notification requires both --client and --session")
        from src.adapters import sessions
        spec = sessions.descriptor(client)
        requested_mode = select_delivery(spec, args.steer, args.queue, idle_only)
        steer = args.steer or (requested_mode == "send_now" and spec.get("native_send_now_flag") == "steer")
        sessions.validate(client, session, steer, idle_only, codex_home, claude_home)
        if requested_mode == "unsupported_requires_binding":
            return {"ok": False, "status": "requires_receiver_binding",
                    "error_code": "DELIVERY_MODE_UNSUPPORTED", "client": client, "session": session,
                    "inbox_status": "not_attempted", "session_status": "not_attempted",
                    "current_turn_accepted": False, "session_accepted": False,
                    "next_action": "This receiver has not advertised immediate admission. Bind a capable receiver, or explicitly request a supported queue mode."}
        if args.transport == "nats":
            raise ValueError("For an addressed chat choose auto, direct, ssh, relay, sqs or github")
        if return_endpoint is None and not args.one_way:
            return {"ok": False, "status": "requires_return_binding", "error_code": "RETURN_ENDPOINT_REQUIRED",
                    "error_message": "This host did not identify the sender chat. Use --wait-reply SECONDS for a bounded exchange, supply --return-client and --return-session for native callbacks, or explicitly use --one-way.",
                    "id": args.id, "inbox_status": "not_attempted", "session_status": "not_attempted",
                    "session_accepted": False}
        if author is None and not args.as_device:
            return {"ok": False, "status": "requires_sender_identity", "error_code": "SENDER_SESSION_REQUIRED",
                    "id": args.id, "inbox_status": "not_attempted", "session_accepted": False,
                    "error_message": "Supply --from-client and --from-session for this chat. A device program may use --as-device."}
    if args.text is not None:
        raw = args.text
    elif args.text_file:
        with args.text_file.open(encoding="utf-8-sig") as source:
            raw = source.read(65537)
    else:
        raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        raise ValueError("Message text exceeds 65,536 characters. Send the file separately.")
    inherited_subject = (route or {}).get("original_subject")
    subject = normalize_subject(args.subject) if args.subject is not None else inherited_subject or preview_subject(raw)
    envelope = {"id": args.id, "from": origin, "to": target, "text": raw, "subject": subject, "envelope_version": 2,
                "sender_endpoint": author, "sender_identity_source": identity_source}
    if args.intent is not None:
        if args.kind == "receipt":
            raise ValueError("A receipt cannot request work or a reply.")
        envelope["communication_intent"] = args.intent
    if linked_to or args.reply_to:
        envelope["reply_to"] = linked_to or args.reply_to
    if args.kind != "message":
        envelope["kind"] = args.kind
    if return_endpoint is not None:
        envelope["reply_endpoint"] = return_endpoint
    recipient_endpoint = None
    if notify:
        recipient_endpoint = messages.normalize_reply_endpoint({
            "device": target, "client": client, "session": session,
            **({"delivery": "queue"} if args.queue else {}),
            **({"codex_home": codex_home} if codex_home else {}),
            **({"claude_home": claude_home} if claude_home else {}),
        })
        envelope["recipient_endpoint"] = recipient_endpoint
    destination = messages.normalize_participant(recipient_endpoint) if notify else (route or {}).get("recipient_identity")
    if destination:
        destination = {**destination, "device": transport.resolve(root, destination["device"])}
    envelope["recipient_identity"] = destination
    if group is None and route and route.get("group") and author and destination:
        from src.runtime.correspondence import address_key
        members = {address_key(p, root=root) for p in route["group"]["members"]}
        if address_key(author, root=root) in members and address_key(destination, root=root) in members:
            group = route["group"]
    if attachment is not None:
        envelope["letter_attachment"] = attachment
    if group is not None:
        envelope["group"] = group
    if attachment is not None or group is not None:
        if args.transport == "nats":
            raise ValueError("Use auto, direct, ssh, relay, sqs or github for a forwarded letter or discussion")
        frame = {"service": "msg", "action": "deliver", "payload": envelope}
        if len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1_000_000:
            raise ValueError("The complete letter exceeds the transport limit; send larger files separately")
    participants = {"sender_endpoint": author, "sender_identity_source": identity_source, "recipient_identity": destination,
                    "subject": subject, **_return_readiness(root, return_endpoint)}
    notification = None
    if notify:
        notification = {"id": envelope["id"], "client": client, "session": session}
        notification.update({k: v for k, v in (("codex_home", codex_home), ("claude_home", claude_home),
                                               ("steer", steer), ("idle_only", idle_only),
                                               ("force_queue", spec.get("force_queue_option") and args.queue)) if v})
    from src.runtime import mail_routes, outbox
    tool_reply = route is not None and route["status"] == "tool_result"
    routes = mail_routes.candidates(root, target, args.transport, delivery_report=args.delivery_report,
                                   legacy_mail=not (notify or tool_reply or attachment or group))
    if args.delivery_report and (args.kind == "receipt" or not all(r in REPORT_CARRIERS for r in routes)):
        raise ValueError("A delivery report requires a letter and a paired relay, sqs or github carrier")
    relayed = any(r in REPORT_CARRIERS for r in routes)
    if relayed and target == origin:
        raise ValueError("Use the local route to address another chat on this device")
    if target != origin:
        auto_receipt = bool(author and destination and not args.one_way and args.kind != "receipt" and tuple(routes) == ("github",))
        args.delivery_report = outbox.remember(root, envelope, delivery_report=args.delivery_report, automatic_report=auto_receipt)
    from src.runtime import mail_contact
    route_name = "local" if target == origin else routes[0]
    preflight = contact.observe(root, author, destination, route_name)
    mail_contact.before_send(root, envelope, args.intent)

    def submit(carrier, *, lookup=False):
        if carrier in REPORT_CARRIERS:
            adapter = _relay_carrier(carrier)
            operation = adapter.reconcile if lookup else adapter.publish
            sent = operation(root, envelope, notification if author else None, delivery_report=args.delivery_report)
            return {**sent, **participants, "host": target, "client": client, "session": session,
                    "reply_endpoint": return_endpoint, "recipient_endpoint": recipient_endpoint,
                    "delivery_report_requested": args.delivery_report,
                    "requested_delivery_mode": requested_mode, "session_status": "unconfirmed" if notify and author else "not_requested"}
        if carrier == "nats":
            result = _request(root, "msg", "publish", envelope)
            return {**result, **participants, "id": envelope["id"], "transport": "nats", "host": target}
        call = partial(_call, direct=True) if carrier == "direct" else _call
        channel = "local" if target == origin else "direct" if carrier == "direct" else "ssh"
        return _send_to_inbox(root, target, call, channel, envelope, participants, notification, spec,
                              requested_mode, steer, idle_only, return_endpoint, recipient_endpoint,
                              attachment, group, tool_reply)

    if target == origin:
        result = submit(routes[0])
    else:
        result = mail_routes.send(root, target, envelope["id"], routes, submit, reconcile=partial(submit, lookup=True))
    mail_contact.after_send(root, envelope["id"], result)
    delivery = result.get("inbox_delivery", result)
    received_intent = (delivery.get("message") or {}).get("communication_intent")
    return {**result, "preflight": preflight, "communication_intent": args.intent,
            "intent_delivery": "unspecified" if args.intent is None else
                               "recipient_copy_confirmed" if received_intent == args.intent else "unverified"}


def _send_to_inbox(root, target, call, channel, envelope, participants, notification, spec, requested_mode,
                   steer, idle_only, return_endpoint, recipient_endpoint, attachment, group, tool_reply):
    client, session = (notification["client"], notification["session"]) if notification else (None, None)
    if notification is None:
        delivered = call(root, target, "msg", "deliver", envelope)
        if not _extensions_received(delivered, attachment, group, root):
            return {**participants, "ok": False, "id": envelope["id"], "host": target,
                    "status": "copy_unverified", "inbox_status": "unknown", "session_accepted": False}
        inbox = delivered.get("status") if delivered.get("ok") and delivered.get("status") in {"received", "duplicate"} else "unknown"
        return {**delivered, **participants, "id": envelope["id"], "host": target, "inbox_status": inbox,
                **({"return_channel": "tool_result", "caller_read": "unverified"} if tool_reply else {}),
                "transport": channel}
    try:
        delivered = call(root, target, "msg", "deliver", envelope)
    except ValueError as exc:
        delivered = _error(exc)
    except (OSError, RuntimeError) as exc:
        delivered = {**_error(exc), "outcome": "unknown"}
    if not _extensions_received(delivered, attachment, group, root):
        delivered = {"ok": False, "outcome": "unknown", "error_code": "LETTER_COPY_UNVERIFIED",
                     "error_message": "The destination did not confirm the complete letter. Inspect its original ID before retrying."}
    result = {"ok": False, **participants, "id": envelope["id"], "host": target,
              "transport": channel, "client": client,
              "session": session, "inbox_delivery": delivered, "model_status": "unverified",
              "reply_endpoint": return_endpoint, "recipient_endpoint": recipient_endpoint,
              "requested_delivery_mode": requested_mode,
              "delivery_mode": "idle_only" if idle_only else "steer" if steer else requested_mode}
    if not delivered.get("ok") or delivered.get("status") not in {"received", "duplicate"}:
        stage = "unknown" if delivered.get("outcome") == "unknown" else "failed"
        return {**result, "status": "inbox_" + stage, "inbox_status": stage,
                "session_status": "not_attempted", "session_accepted": False,
                "session_notification": {"status": "not_attempted"},
                "error_code": delivered.get("error_code", "INBOX_DELIVERY_UNVERIFIED"),
                "error_message": delivered.get("error_message", "Target inbox delivery was not confirmed")}
    result["inbox_status"] = delivered["status"]
    try:
        admitted = call(root, target, "ai", "notify", notification, 75)
    except ValueError as exc:
        admitted = _error(exc)
    except (OSError, RuntimeError) as exc:
        admitted = {**_error(exc), "outcome": "unknown"}
    result["session_notification"] = admitted
    result["delivery_mode"] = admitted.get("delivery_mode", result["delivery_mode"])
    delivery_receipt = acceptance(admitted, requested_mode, spec)
    result["delivery_receipt"] = delivery_receipt
    result["current_turn_accepted"] = delivery_receipt["current_turn"]
    if delivery_receipt["scope"] == "original_session":
        return {**result, "ok": requested_mode == "queue", "status": "original_session_accepted",
                "session_status": "accepted_placement_unknown", "session_accepted": True}
    accepted = delivery_receipt["scope"] in {"current_turn", "next_turn"}
    if accepted and requested_mode == "send_now" and not delivery_receipt["current_turn"]:
        return {**result, "status": "session_requires_promotion", "session_status": "queued_not_current",
                "session_accepted": True, "error_code": "SESSION_CURRENT_TURN_NOT_ACCEPTED",
                "next_action": "Promote the existing native queue item; do not submit another copy. Queue acceptance did not satisfy immediate delivery."}
    if accepted:
        return {**result, "ok": True, "status": "session_accepted", "session_status": "accepted",
                "session_accepted": True}
    if admitted.get("outcome") == "unknown" or admitted.get("status") == "unknown":
        stage = "unknown"
    elif admitted.get("status") == "deferred_in_inbox":
        stage = "deferred"
    elif admitted.get("status") == "requires_activation":
        return {**result, "status": "session_requires_activation", "session_status": "requires_activation",
                "session_accepted": False, "requires_activation": True,
                "gui_required": admitted.get("gui_required", False),
                "error_code": admitted.get("error_code", "AI_SESSION_REQUIRES_ACTIVATION"),
                "next_action": admitted.get("next_action", "Activate the exact original chat using its native client")}
    elif admitted.get("status") in {"promotion_required", "requires_receiver_binding"}:
        return {**result, "status": "session_" + admitted["status"],
                "session_status": admitted["status"], "session_accepted": admitted.get("original_accepted", False),
                "error_code": admitted.get("error_code", "AI_RECEIVER_BINDING_REQUIRED"),
                "next_action": admitted.get("next_action", "Check or promote the exact existing native item; do not resubmit another copy.")}
    elif admitted.get("ok") is True:
        stage = "unknown"
    else:
        stage = "failed"
    return {**result, "status": "session_" + stage, "session_status": stage,
            "session_accepted": None if stage == "unknown" else False,
            "error_code": admitted.get("error_code", "AI_NOTIFICATION_UNVERIFIED"),
            "error_message": admitted.get("error_message", "Message is in the target inbox; AI session acceptance was not confirmed")}


def _parse_and_run(root, argv, *, parser=None):
    if argv[:1] == ["file"]:
        from src.file_layer import xdev_file
        return xdev_file.main(argv[1:], root=root)
    parser = parser or _parser()
    args = parser.parse_args(argv)
    if args.service == "msg" and args.action == "send":
        if args.forward_of is not None:
            args.action, args.message_id = "forward", args.forward_of
        elif args.quote is not None or args.excerpt_only:
            raise ValueError("A quotation requires forward ID or send --forward-of ID")
        elif args.text is None and args.text_file is None and not args.stdin:
            parser.error("Supply --text, --text-file or --stdin")
    if args.service == "msg" and args.action == "reply":
        audience = args.audience
        expanded = ["msg", "send", "--kind", "reply", "--reply-to", args.message_id, "--text=" + args.text]
        if args.id is None:
            from src.runtime import messages
            from src.runtime.correspondence import device_key
            origin = transport.node(root)
            author, _ = _actor(origin, args.from_client, args.from_session, profile=args.from_profile)
            canonical = {k: messages.profile_key(v) if k in {"codex_home", "claude_home"} else v
                         for k, v in (author or {}).items()}
            if canonical:
                canonical["device"] = device_key(canonical["device"], root)
            parts = [device_key(origin, root), canonical, args.message_id, args.text]
            if audience != "callback":
                parts.append(audience)
            if args.subject is not None:
                parts.append(args.subject)
            if args.intent is not None:
                parts.append({"communication_intent": args.intent})
            key = json.dumps(parts, sort_keys=True,
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            args.id = "reply-" + hashlib.sha256(key).hexdigest()
        for field in ("id", "from_client", "from_session", "from_profile", "subject", "transport", "intent"):
            value = getattr(args, field)
            if value is not None:
                expanded.extend(["--" + field.replace("_", "-"), value])
        if args.delivery_report:
            expanded.append("--delivery-report")
        args = parser.parse_args(expanded)
        args.audience = audience
    if args.service == "msg" and args.action == "read":
        args.action = "receipt"
    # CLI executors carry their own host context. Shared MCP servers must use
    # the per-call context established by xdev_msg instead of process env.
    from src.runtime.cli_result import collecting
    passive = getattr(args, "discover", False) or (args.service == "msg" and args.action in {"events", "host-events"})
    if args.service != "surface" and not passive and not collecting() and os.environ.get("CODEX_THREAD_ID"):
        _register_caller(root)
    values = vars(args).copy()
    service = values.pop("service")
    host = values.pop("host", "local")
    if service == "surface":
        from src.runtime import surfaces
        action = values.pop("action")
        result = _call(root, host, service, action, {k: v for k, v in values.items() if v is not None})
        result = surfaces.guide(result, action, args.provider, host, values.get("target"))
    elif service == "who":
        result = _call(root, host, "system", "who", {})
        if result.get("ok") and host != "local" and not transport.directory(root).same(host, result["device"]):
            raise ValueError("The peer reported a different device name; check both address mappings")
    elif service == "exec":
        if not 0 < args.timeout <= 300:
            raise ValueError("Invalid timeout")
        result = _call(root, host, service, "run", _args(values), args.timeout + 15)
    elif service == "job":
        action = values.pop("action")
        if action == "start":
            values["id"] = args.id or uuid4().hex
            _args(values)
        result = _call(root, host, service, action, values)
        result.setdefault("id", values["id"])
    elif service == "msg" and args.action in contact.ACTIONS:
        result = contact.handle(root, args)
    elif service == "msg" and args.action == "events":
        result = _relay_carrier(args.transport).events(root, after=args.after, limit=args.limit)
    elif service == "msg" and args.action == "host-events":
        if collecting() or sys.stdin is None or sys.stdout is None or sys.stdin.isatty() or sys.stdout.isatty():
            raise ValueError("Start host-events from your local host plugin with private input and output pipes")
        from src.adapters.mail_arrival_stdio import serve
        serve(root, args.peer, args.client, args.session, sys.stdin, sys.stdout, profile=args.profile)
        return 0
    elif service == "msg" and args.action == "listen":
        if args.follow and args.transport == "nats":
            raise ValueError("--follow requires a relay, sqs or github listener")
        if args.transport in REPORT_CARRIERS:
            carrier = _relay_carrier(args.transport)
            if args.follow:
                if collecting():
                    raise ValueError("Run the continuous listener from the device service or terminal")
                try:
                    carrier.follow(root, _emit, limit=args.limit, timeout=args.timeout)
                except KeyboardInterrupt:
                    pass
                result = {"ok": True, "status": "stopped", "transport": args.transport}
            else:
                result = carrier.listen(root, limit=args.limit, timeout=args.timeout)
        else:
            result = _request(root, "msg", "listen", {"limit": args.limit, "timeout": args.timeout})
    elif service == "msg" and args.action == "whoami":
        origin = transport.node(root)
        endpoint, source = _actor(origin)
        result = {"ok": True, "device": origin, "sender_endpoint": endpoint,
                  "sender_identity_source": source, "status": "identified" if endpoint else "device_only",
                  **_return_readiness(root, endpoint)}
    elif service == "msg" and args.action in {"send", "forward"}:
        from src.cli import letters
        if args.action == "forward" or args.also or args.also_profile or getattr(args, "audience", "callback") != "callback":
            result = letters.deliver(root, args)
        else:
            result = _send(root, args)
        if args.wait_reply is not None and (result.get("ok") or result.get("inbox_status") in
                                            {"received", "duplicate", "unknown"}):
            from src.runtime import messages
            query = {"reply_to": args.id, "from": result["host"], "substantive_only": True,
                     "timeout": args.wait_reply, "exact_profiles": True}
            if result.get("recipient_identity"):
                query["sender_endpoint"] = result["recipient_identity"]
            if result.get("sender_endpoint"):
                query["recipient_endpoint"] = result["sender_endpoint"]
            waited = messages.handle(root, "wait", query)
            result["reply_wait"] = {"status": waited["status"], "request_id": args.id,
                                    "from": query["from"], "message": waited["message"]}
            if waited["message"] is not None:
                # The response completes this exchange even if native push is
                # still unverified. Preserve the original admission evidence.
                result.update(ok=True, submission_status=result["status"], status="reply_received")
            if waited["message"] is None:
                result["reply_wait"]["resume_args"] = ["msg", "wait", "--reply-to", args.id,
                                                       "--from", query["from"], "--substantive-only"]
                resume = result["reply_wait"]["resume_args"]
                for field, prefix in (("sender_endpoint", "from"), ("recipient_endpoint", "to")):
                    if field in query:
                        endpoint = query[field]
                        resume.extend(["--" + prefix + "-client", endpoint["client"],
                                       "--" + prefix + "-session", endpoint["session"]])
                        profile = endpoint.get("codex_home") or endpoint.get("claude_home")
                        if profile:
                            resume.extend(["--" + prefix + "-profile", profile])
                if "recipient_endpoint" not in query:
                    resume.append("--device-inbox")
    elif service == "msg" and args.action == "bind":
        from src.runtime import messages
        origin = transport.node(root)
        endpoint = {"device": transport.resolve(root, args.return_host),
                    "client": args.return_client, "session": args.return_session}
        if args.return_queue:
            endpoint["delivery"] = "queue"
        endpoint.update({key: value for key, value in (
            ("codex_home", args.return_codex_home), ("claude_home", args.return_claude_home)) if value is not None})
        recipient = None
        if args.client is not None or args.session is not None:
            recipient = {"device": origin, "client": args.client, "session": args.session}
            recipient.update({key: value for key, value in (
                ("codex_home", args.codex_home), ("claude_home", args.claude_home)) if value is not None})
        elif args.codex_home or args.claude_home:
            raise ValueError("Recipient profile requires --client and --session")
        result = messages.bind_reply_endpoint(root, args.id, endpoint, recipient_endpoint=recipient)
    elif service == "ai":
        if args.register:
            if args.id or args.capabilities or args.codex_home or args.claude_home or args.steer or args.idle_only or args.queue:
                raise ValueError("Receiver registration cannot be combined with delivery")
            from src.adapters import caller_context
            caller = caller_context.current()
            own_thread = caller["session"] if caller["status"] == "ready" else os.environ.get("CODEX_THREAD_ID")
            if args.session is not None and args.session != own_thread:
                raise ValueError("Registration is only for this executor's own chat")
            result = _register_caller(root)
        elif args.discover:
            if args.id or args.codex_home or args.claude_home or args.steer or args.idle_only or args.queue:
                raise ValueError("Discovery accepts only optional --client and --session filters")
            result = sessions.discover(root, client=args.client, session=args.session)
        elif args.capabilities:
            if args.id or args.session or args.codex_home or args.claude_home or args.steer or args.idle_only or args.queue:
                raise ValueError("Capabilities is read-only; do not combine it with a notification")
            result = sessions.capabilities(root)
        else:
            if not args.id or not args.session:
                raise ValueError("AI notification requires --session and a local inbox message ID")
            args.client = args.client or "opencode"
            spec = sessions.descriptor(args.client)
            requested_mode = select_delivery(spec, args.steer, args.queue, args.idle_only)
            steer = args.steer or (requested_mode == "send_now" and spec.get("native_send_now_flag") == "steer")
            sessions.validate(args.client, args.session, steer, args.idle_only, args.codex_home, args.claude_home)
            if requested_mode == "unsupported_requires_binding":
                result = {"ok": False, "status": "requires_receiver_binding",
                          "client": args.client, "session": args.session, "id": args.id,
                          "error_code": "DELIVERY_MODE_UNSUPPORTED", "session_accepted": False,
                          "current_turn_accepted": False, "requested_delivery_mode": requested_mode,
                          "delivery_receipt": acceptance({}, requested_mode, spec),
                          "next_action": "Bind a receiver advertising immediate admission, or explicitly request a supported queue mode."}
            else:
                options = {"force_queue": True} if spec.get("force_queue_option") and args.queue else {}
                native = sessions.submit(root, args.id, args.session, args.client,
                                         steer, args.idle_only, args.codex_home, args.claude_home, **options)
                delivery_receipt = acceptance(native, requested_mode, spec)
                scope = delivery_receipt["scope"]
                result = {**native, "requested_delivery_mode": requested_mode,
                          "delivery_receipt": delivery_receipt, "current_turn_accepted": delivery_receipt["current_turn"],
                          "session_accepted": scope in {"current_turn", "next_turn", "original_session"}}
                if scope == "next_turn" and requested_mode == "send_now":
                    result.update(ok=False, status="promotion_required", native_status=native.get("status"),
                                  error_code="SESSION_CURRENT_TURN_NOT_ACCEPTED",
                                  next_action="Promote the existing native queue item; do not submit another copy. Queue acceptance did not satisfy immediate delivery.")
                elif scope == "original_session":
                    result.update(ok=requested_mode == "queue", status="original_session_accepted",
                                  session_status="accepted_placement_unknown")
                elif scope not in {"current_turn", "next_turn"}:
                    if not (native.get("ok") is True and native.get("status") == "receipt_suppressed") and native.get("ok") is not False:
                        result.update(ok=False, outcome="unknown", native_status=native.get("status"),
                                      status="input_surface_accepted" if scope == "surface" else "unknown",
                                      error_code="AI_ADMISSION_EVIDENCE_UNVERIFIED")
    else:
        action = values.pop("action")
        if action in {"inbox", "list", "wait", "receipt"}:
            origin = transport.node(root)
            destination = transport.resolve(root, host)
            actor, _ = _actor(origin, values.pop("as_client"), values.pop("as_session"),
                              profile=values.pop("as_profile"))
            if action == "receipt":
                values["reader_endpoint"] = actor
            else:
                wide = values.pop("device_inbox")
                sender = values.get("sender")
                client, session = values.pop("from_client"), values.pop("from_session")
                if (client or session) and not sender:
                    raise ValueError("Filter an author using --from DEVICE --from-client CLIENT --from-session ID")
                values["sender_endpoint"] = _address(transport.resolve(root, sender) if sender else None, client, session,
                                                     values.pop("from_profile"))
                recipient = _address(destination, values.pop("to_client"), values.pop("to_session"),
                                     values.pop("to_profile"))
                if values.get("inbox_wait") is not None and (wide or destination != origin):
                    raise ValueError("Inbox waiting requires one local recipient chat")
                if wide and recipient:
                    raise ValueError("Choose a device-wide inbox or a specific recipient")
                if not wide and not recipient:
                    recipient = actor if destination == origin else None
                    if not recipient and (actor or collecting() or values.get("inbox_wait") is not None) and not values.get("id"):
                        return _emit({"ok": False, "status": "requires_reader_identity",
                                      "error_code": "INBOX_SCOPE_REQUIRED",
                                      "error_message": "Select the recipient with --to-client and --to-session, identify your local chat, or explicitly request --device-inbox."})
                values["recipient_endpoint"] = recipient
                values["exact_profiles"] = True
        if "sender" in values:
            values["from"] = values.pop("sender")
            if values["from"]:
                values["from"] = transport.resolve(root, values["from"])
        values = {key: value for key, value in values.items() if value is not None}
        if action == "wait" and not 0 <= args.timeout <= 600:
            raise ValueError("Wait must be within 0 to 600 seconds")
        if "inbox_wait" in values:
            from src.runtime import messages
            values["timeout"] = values.pop("inbox_wait")
            result = messages.wait_inbox(root, values)
        else:
            wait_limit = 15 + args.timeout if action == "wait" else 60
            result = _call(root, host, service, "list" if action in {"inbox", "list"} else action,
                           values, wait_limit)
        if action == "receipt" and result.get("message") and result.get("ok"):
            from src.cli.letters import reply_choices
            result["reply_choices"] = reply_choices(result["message"], root=root)
            if result["message"].get("kind") != "receipt":
                result["forward_via"] = {"action": "send", "source_arguments": ["--forward-of", result["message"]["id"]]}
    result.setdefault("host", transport.resolve(root, host))
    if service == "msg":
        from src.runtime import messages
        result = messages.display_addresses(root, result)
    return _emit(result)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv == ["mcp"]:
        from src.adapters import mcp_stdio
        try:
            mcp_stdio.run()
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    try:
        root = transport.home()
        if argv[:2] == ["msg", "host-events"]:
            # A host subscription outlives individual calls and their routing snapshots.
            return _parse_and_run(root, argv)
        with transport.routing_snapshot(root):
            if len(argv) == 2 and argv[0] == "_rpc_mail":
                return _serve_one(root, mailbox_sender=argv[1])
            return _serve_one(root) if argv == ["_rpc"] else _parse_and_run(root, argv)
    except (OSError, ValueError, RuntimeError) as exc:
        return _emit(_error(exc))


if __name__ == "__main__":
    raise SystemExit(main())
