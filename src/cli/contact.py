"""User-facing, bounded contact checks and control operations."""

import sqlite3
from src.runtime import mail_contact as contact, messages, outbox, transport


ACTIONS = {"preflight", "availability", "report", "status", "recall", "replace", "correct"}
CARRIERS = ("auto", "direct", "ssh", "github", "relay", "sqs", "nats")
INTENTS = contact.INTENTS


def arguments(actions, status):
    def actor(p):
        for flag in ("client", "session", "profile"):
            p.add_argument("--as-" + flag)
    p = actions.add_parser("preflight", help="Check the exact chat and known availability without sending mail")
    p.add_argument("--to", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--profile")
    p.add_argument("--transport", choices=CARRIERS, default="auto")
    actor(p)
    p = actions.add_parser("availability", help="Publish a time-limited availability declaration for your own chat")
    p.add_argument("--state", required=True, choices=contact.STATES)
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--reason")
    actor(p)
    for action in ("report", "recall", "replace", "correct"):
        p = actions.add_parser(action)
        p.add_argument("id")
        actor(p)
        p.add_argument("--operation-id")
        p.add_argument("--expected-version", type=int)
        if action == "report":
            p.add_argument("--work-state", choices=("pending", "in_progress", "completed", "blocked"))
            p.add_argument("--reply-intent", choices=("undecided", "will_reply", "no_reply"))
            p.add_argument("--reason")
        else:
            p.add_argument("--transport", choices=CARRIERS, default="auto")
            if action in ("replace", "correct"):
                p.add_argument("--with", dest="replacement_id", required=True)
    status.add_argument("--transport", choices=CARRIERS, default="auto")


def carrier_for(root, peer, selected, previous=None):
    directory = transport.directory(root)
    if directory.same(peer, directory.node):
        return "local"
    if selected != "auto":
        return selected
    if previous:
        return previous
    from src.runtime import mail_routes
    return mail_routes.candidates(root, peer, selected)[0]


def availability_options(observation):
    declared = observation.get("declaration") or {}
    if not declared.get("expired", True) and declared.get("state") in {"busy", "quota_limited", "maintenance"}:
        return ["leave_message", "check_after_declaration_expires"]
    if observation.get("host_state") == "unknown":
        if observation.get("observation_request") == "requested":
            return ["check_pending_preflight_later", "leave_message"]
        return ["refresh_preflight", "leave_message"]
    return []


def with_availability_options(observation):
    options = availability_options(observation)
    return {**observation, **({"next_options": options} if options else {})}


def observe(root, actor, target, carrier, *, explicit=False):
    if target is None:
        return contact.cached(root, None)
    observed = contact.cached(root, target, actor=actor)
    if not explicit and not observed["host_observation_stale"]:
        return with_availability_options({**observed, "transport": carrier, "live_check": "cached"})
    receipt = {"checked_at": contact.now()}
    # A fresh, authenticated response was sampled after this local request began.
    earliest_sample = receipt["checked_at"]
    try:
        if carrier == "local":
            fresh = contact.local_probe(root, target)
        elif carrier in ("direct", "ssh"):
            from src.runtime import mail_control_ssh
            checked = mail_control_ssh.call(root,target["device"], {"op": "preflight", "actor": actor, "target": target},
                                            timeout=8 if explicit else 3, direct=carrier == "direct")
            fresh = checked.get("observation") if isinstance(checked, dict) else None
            if isinstance(checked, dict) and checked.get("ok") is False:
                receipt["peer_check_status"] = "unavailable"
                code = checked.get("error_code")
                if isinstance(code, str) and code.isascii() and code.isidentifier() and len(code) <= 80:
                    receipt["peer_error_code"] = code
        elif carrier == "github" and actor:
            from src.runtime import mail_control
            from datetime import datetime, timezone
            cmd = {"op": "preflight", "actor": contact.identity(root, actor), "target": contact.identity(root, target)}
            response = mail_control.latest(root, target["device"], cmd)
            if explicit:
                try:
                    response = mail_control.submit(root, target["device"], cmd)
                except (ValueError, RuntimeError, OSError, sqlite3.Error):
                    receipt["observation_request"] = "unconfirmed"
            evidence = response.get("evidence") or {}
            fresh = evidence.get("observation") or (evidence if evidence.get("source") == "local_host" else None)
            sent_at = response.get("evidence_requested_at")
            earliest_sample = datetime.fromtimestamp(sent_at, timezone.utc).isoformat() if isinstance(sent_at, (int, float)) else None
            receipt.setdefault("observation_request", response.get("status"))
            receipt.update({k: response[k] for k in ("requested_at", "response_observed_at", "earlier_observation")
                            if k in response})
        else:
            fresh = None
        if fresh:
            observed = contact.cached(root, target, actor=actor, refresh=fresh, earliest_sample=earliest_sample)
            if carrier == "github":
                observed.update(status="cached", live_check="cached_peer_report")
            else:
                observed["live_check"] = "sampled"
        else:
            observed["live_check"] = "no_live_check" if not explicit else "unavailable_on_route"
    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        observed["live_check"] = "unavailable_on_route"
    return with_availability_options({**observed, **receipt, "transport": carrier})


def remote_or_local(root, carrier, payload):
    target = payload["target"]
    if carrier == "local":
        from src.runtime import mail_lifecycle
        return mail_lifecycle.apply(root, {k:v for k,v in payload.items() if k != "target"}, expected_target=target)
    if carrier in ("ssh", "direct"):
        from src.runtime import mail_control_ssh
        try:
            return mail_control_ssh.call(root, target["device"], payload, direct=carrier == "direct")
        except (ValueError, OSError, RuntimeError) as exc:
            return {"ok": False, "status": "unconfirmed", "error_code": type(exc).__name__,
                    "source": "paired_control_rpc", "checked_at": contact.now()}
    if carrier == "github":
        from src.runtime import mail_control
        try:
            return mail_control.submit(root, target["device"], payload)
        except (ValueError, OSError, RuntimeError) as exc:
            return {"ok": False, "status": "unconfirmed", "error_code": type(exc).__name__,
                    "source": "private_github_control", "checked_at": contact.now()}
    return {"ok": False, "status":"control_unavailable", "transport": carrier}


def handle(root, args):
    from src.cli.main import _address, _actor
    from src.runtime import mail_lifecycle
    origin = transport.node(root)
    as_device = getattr(args, "as_device", False)
    actor, source = _actor(origin, args.as_client, args.as_session, profile=args.as_profile, as_device=as_device)
    from src.adapters import caller_context
    if (args.action in {"availability", "report", "recall", "replace", "correct"}
            and caller_context.current()["source"] != "unbound" and source not in {"cli_host", "mcp_metadata"}):
        return {"ok": False, "status": "host_chat_identity_required"}
    if args.action == "preflight":
        target = _address(transport.resolve(root,args.to),args.client,args.session,args.profile)
        carrier = carrier_for(root,target["device"],args.transport)
        return {"ok": True,"target": target,"preflight": observe(root,actor,target,carrier,explicit=True)}
    if args.action == "availability":
        return contact.report_presence(root,actor,args.state,args.ttl,args.reason)
    if args.action == "report":
        cmd = {"op":"progress","id":args.id,"actor":actor}
        cmd.update({name:getattr(args,name) for name in ("work_state","reply_intent","reason","operation_id","expected_version")
                    if getattr(args,name) is not None})
        return mail_lifecycle.apply(root,cmd)
    details = contact.dispatch(root,args.id,actor,as_device=as_device)
    previous = outbox.status(root,args.id,actor,as_device=as_device)
    if not details:
        if args.action == "status" and not previous.get("ok") and actor:
            own = mail_lifecycle.apply(root,{"op":"status","actor":actor,"id":args.id})
            if own.get("ok"):
                return own
        return previous if args.action == "status" else {"ok":False,"status":"outgoing_message_not_found"}
    target = details["recipient"]
    carrier = carrier_for(root, details["peer"], args.transport, previous.get("transport")) if details["peer"] else "unknown"
    if target and actor:
        cmd = {"op":args.action,"actor":contact.identity(root,actor),"target":target,"id":args.id}
        for field in ("operation_id", "expected_version"):
            if getattr(args, field, None) is not None:
                cmd[field] = getattr(args, field)
        if getattr(args,"replacement_id",None):
            replacement = contact.dispatch(root,args.replacement_id,actor)
            if not replacement or not messages.matches_participant(target,replacement["recipient"],exact=True,root=root):
                return {"ok":False,"status":"replacement_outside_exchange"}
            cmd["replacement_id"] = args.replacement_id
        recipient_result = remote_or_local(root,carrier,cmd)
    else:
        recipient_result = {"ok":False,"status":"exact_chat_required"}
    if args.action != "status":
        choices = ["check_same_operation_later"] if recipient_result.get("status") in {"requested","unconfirmed"} else []
        return {**recipient_result, "transport": carrier, **({"next_options": choices} if choices else {})}
    if not previous.get("ok"):
        previous = {"ok": True,"status":"outbound_status","id":args.id}
    # A caller can select a different carrier for this status query. Retention belongs to
    # the route that carried the actual letter, even when a different route is available.
    original_route = previous.get("transport") or details["submission"].get("transport")
    if original_route == "github" and details["peer"]:
        from src.runtime import github_mail
        try:
            previous["transport_retention"] = github_mail.retention_for(root, details["peer"], args.id)
        except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error):
            previous["transport_retention"] = {"state": "unknown", "source": "private_github_mailbox"}
    linked = contact.related_reply(root,args.id,actor,target)
    observation = recipient_result.get("evidence") or recipient_result
    if observation.get("withdrawn_at"):
        previous["available_to_addressed_chat"] = False
        previous["current_mailbox_state"] = "withdrawn"
    if linked:
        choices = []
    elif observation.get("withdrawn_at") or observation.get("reply_intent") == "no_reply":
        choices = ["no_further_wait_needed"]
    elif observation.get("work_state") == "completed" and observation.get("reply_intent") != "will_reply":
        choices = ["review_reported_completion"]
    elif observation.get("work_state") == "blocked":
        choices = ["review_recipient_report"]
    elif previous.get("status") == "outbound_status" or not observation.get("read_at"):
        choices = ["check_same_message_id_later"]
    else:
        choices = []
    return {**previous, "dispatch": details, "recipient_observation": recipient_result,
            "linked_reply": linked, "reply_evidence": "linked_reply_received" if linked else "unverified",
            "queried_at": contact.now(), **({"next_options": choices} if choices else {})}
