"""Forward a received letter and address a named discussion without rewriting its author."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import copy
import sys

from src.adapters import sessions
from src.adapters.delivery_contract import select_delivery
from src.runtime import messages, transport
from src.runtime.correspondence import address_key, normalize_group, stable_id, batch_state, device_key, plan_group
from src.runtime.letter_attachment import make_attachment
from src.runtime.subjects import normalize_subject, preview_subject


def _source(root, identifier, actor):
    source = messages.handle(root, "wait", {"id": identifier, "timeout": 0})["message"]
    if not source or source.get("recipient_identity_error") or source.get("recipient_endpoint_error"):
        raise ValueError("The local source letter or its address could not be verified")
    if not actor or not transport.directory(root).same(actor["device"], source["to"]) or (source["recipient_identity"] and
            not messages.matches_participant(actor, source["recipient_identity"], root=root)):
        raise ValueError("Only the addressed chat can forward this letter or select a new audience")
    if source.get("withdrawn_at"):
        raise ValueError("This letter was withdrawn.")
    if source.get("letter_attachment_error") or source.get("group_error"):
        raise ValueError("The source letter has unverifiable attachments or discussion membership")
    return source


def _body(args, forward):
    if args.stdin:
        body = sys.stdin.read(65537)
    elif args.text_file:
        with args.text_file.open(encoding="utf-8-sig") as stream:
            body = stream.read(65537)
    else:
        body = args.text
    if body is None and forward:
        body = "转寄原信。"
    if not isinstance(body, str) or not body.strip() or len(body) > 65536:
        raise ValueError("Supply a message body of 1 through 65536 characters")
    return body


def _target(device, client, session, root, **profile):
    address = {"device": transport.resolve(root, device), "client": client, "session": session,
               **{k: v for k, v in profile.items() if v is not None}}
    return messages.normalize_reply_endpoint(address)


def _oldest_copy(source):
    attachment = source.get("letter_attachment")
    while attachment and attachment["original"].get("letter_attachment"):
        attachment = attachment["original"]["letter_attachment"]
    if attachment and attachment["original"].get("attached_sha256"):
        raise ValueError("Only an excerpt of the history is available; the original author cannot be established")
    return attachment


def reply_choices(source, *, root=None):
    if source.get("kind") == "receipt":
        return []
    options = []
    callback, sender = source.get("reply_endpoint"), source.get("sender_endpoint")
    if callback:
        options.append({"audience": "callback", "destination": callback})
    if sender and (not callback or not callback.get("client") or address_key(sender, root=root) != address_key(callback, root=root)):
        options.append({"audience": "sender", "destination": sender})
    attachment = source.get("letter_attachment")
    if attachment:
        try:
            oldest = _oldest_copy(source)["original"]
        except ValueError:
            oldest = {}
        if oldest.get("sender_endpoint"):
            options.append({"audience": "original-author", "destination": oldest["sender_endpoint"]})
            if sender and address_key(sender, root=root) != address_key(oldest["sender_endpoint"], root=root):
                options.append({"audience": "authors", "destinations": [sender, oldest["sender_endpoint"]]})
    if source.get("group"):
        options.append({"audience": "all", "group_id": source["group"]["id"]})
    if root is not None:
        directory = transport.directory(root)
        if directory.legacy_clients:
            for option in options:
                if "destination" in option:
                    option["destination"] = messages.mapped_endpoint(option["destination"], directory.resolve)
                if "destinations" in option:
                    option["destinations"] = [messages.mapped_endpoint(p, directory.resolve) for p in option["destinations"]]
    return options


def _audience(source, choice, actor, root):
    sender = source["sender_endpoint"]
    if choice == "all":
        if not source.get("group"):
            raise ValueError("This letter has no discussion membership; specify sender or authors")
        routes = {address_key(p, root=root): p for p in source["group"].get("routes", [])}
        own = address_key(actor, root=root)
        endpoints = [routes.get(address_key(p, root=root), p) for p in source["group"]["members"] if address_key(p, root=root) != own]
    else:
        attachment = _oldest_copy(source) if choice in {"original-author", "authors"} else None
        original = attachment["original"] if attachment else {}
        author = original.get("author_route") or original.get("sender_endpoint")
        endpoints = {"sender": [sender], "original-author": [author], "authors": [sender, author]}[choice]
    if not endpoints or any(p is None for p in endpoints):
        raise ValueError("The selected letter does not identify the requested author or recipient")
    # A known native route to the same chat can carry a channel alias or queue opt-in.
    callback = source.get("reply_endpoint")
    if callback and callback.get("client"):
        endpoints = [callback if address_key(e, root=root) == address_key(callback, root=root) else e for e in endpoints]
    return endpoints


def _batch_preflight(observation):
    if not isinstance(observation, dict):
        return {"status": "not_recorded"}
    fields = ("status", "source", "observed_at", "checked_at", "host_state", "adapter_state",
              "host_xdev_version", "host_observation_stale", "observation_age_seconds",
              "live_check", "observation_request", "next_options")
    result = {key: observation[key] for key in fields if key in observation}
    declaration = observation.get("declaration")
    if isinstance(declaration, dict):
        # The author needs the scheduling constraint. Do not put a peer's free-text reason in the batch journal.
        result["declaration"] = {key: declaration[key] for key in ("state", "source", "observed_at", "expires_at", "expired")
                                 if key in declaration}
    return result


def deliver(root, args):
    from src.cli.main import _actor, _send
    origin = transport.node(root)
    actor, _ = _actor(origin, args.from_client, args.from_session, as_device=args.as_device, profile=args.from_profile)
    forward = args.action == "forward"
    choice = getattr(args, "audience", "callback")
    redirect = choice != "callback"
    if not actor:
        raise ValueError("Identify the sending chat before forwarding or addressing a discussion")
    if forward and (args.reply_to or args.kind != "message" or (args.excerpt_only and args.quote is None)):
        raise ValueError("Forward a received letter; --excerpt-only requires a verbatim --quote")
    if (args.also or args.also_profile) and (args.wait_reply is not None or args.reply_to or args.kind != "message"):
        raise ValueError("Use --also on an initial letter; collect separate responses by discussion ID")
    source = _source(root, args.message_id if forward else args.reply_to, actor) if forward or redirect else None
    if redirect and source["kind"] == "receipt":
        raise ValueError("Receipt traffic cannot start an automatic reply to another audience")
    args.text = _body(args, forward)
    args.text_file, args.stdin = None, False
    attachment = None
    if forward:
        attachment = make_attachment(source, mode="quote" if args.quote is not None else "forward",
                                     quote=args.quote, include_full=not args.excerpt_only)
    elif choice in {"authors", "original-author"}:
        attachment = _oldest_copy(source)
    if args.subject is None and source:
        base = attachment["original"] if choice == "original-author" and attachment else source
        args.subject = base.get("subject") or preview_subject(base.get("text") or source["text"])
    args.subject = normalize_subject(args.subject) if args.subject is not None else preview_subject(args.text)
    endpoints = []
    if redirect:
        endpoints = _audience(source, choice, actor, root)
    elif args.client or args.session or args.also or args.also_profile:
        endpoints = [_target(args.to, args.client, args.session, root,
                             codex_home=args.codex_home, claude_home=args.claude_home)]
        endpoints.extend(_target(*values, root) for values in args.also)
        for device, client, session, profile in args.also_profile:
            field = sessions.descriptor(client).get("profile_field")
            if not field:
                raise ValueError("This additional recipient does not support a profile directory")
            endpoints.append(_target(device, client, session, root, **{field: profile}))
    elif args.to is None:
        raise ValueError("Supply a recipient device")
    targets = {}
    for endpoint in endpoints:
        endpoint = messages.normalize_reply_endpoint(endpoint)
        endpoint["device"] = transport.resolve(root, endpoint["device"])
        key = address_key(endpoint, root=root)
        if key in targets and targets[key] != endpoint:
            raise ValueError("Conflicting delivery routes for one chat")
        targets[key] = endpoint
    author_key = address_key(actor, root=root)
    if len(targets) > 1 and author_key in targets:
        raise ValueError("A group send must address other participants")
    if len(targets) > 31:
        raise ValueError("A discussion supports up to 31 recipients and the sender")
    if args.id is None:
        if forward:
            args.id = stable_id("forward", author_key, source["id"], attachment["sha256"],
                                sorted(targets) if targets else device_key(args.to, root), args.text, args.subject, device_key(args.return_host, root),
                                args.return_client, args.return_session, args.one_way, args.wait_reply is not None)
        else:
            args.id = stable_id("broadcast", author_key, sorted(targets), args.text, args.subject)
        if args.intent is not None:
            args.id = stable_id("intent", args.id, args.intent)
    group = source.get("group") if choice == "all" else None
    if len(targets) > 1 and group is None:
        from src.cli.main import _return_endpoint
        routes = [{**t, **({"delivery": "queue"} if args.queue else {})} for t in targets.values()]
        callback = _return_endpoint(root, args, origin, actor)
        if callback and callback.get("client") and address_key(callback, root=root) == author_key:
            routes.append(callback)
        group = normalize_group({"id": stable_id("group", author_key, args.id),
                                 "members": [actor, *[messages.normalize_participant(t) for t in targets.values()]],
                                 "routes": routes})
    link = args.reply_to if redirect else None
    if not targets:
        return _send(root, args, attachment=attachment)
    deliveries = []
    for key, endpoint in targets.items():
        single = copy(args)
        single.to, single.client, single.session = endpoint["device"], endpoint["client"], endpoint["session"]
        single.codex_home, single.claude_home = endpoint.get("codex_home"), endpoint.get("claude_home")
        single.queue = args.queue or endpoint.get("delivery") == "queue"
        if redirect:
            single.reply_to = None
        if len(targets) > 1:
            single.id = stable_id("copy", author_key, args.id, key)
        # Check every address and mode before any group member is notified.
        sessions.validate(single.client, single.session, single.steer, single.idle_only, single.codex_home, single.claude_home)
        mode = select_delivery(sessions.descriptor(single.client), single.steer, single.queue, single.idle_only)
        if len(targets) > 1 and mode == "unsupported_requires_binding":
            raise ValueError("A named recipient has no supported immediate route: " + single.client + " " + single.session)
        if args.delivery_report:
            from src.runtime import mail_routes
            from src.runtime.device_names import REPORT_CARRIERS
            selected = mail_routes.candidates(root, single.to, args.transport, delivery_report=True)
            if single.to == origin or not all(carrier in REPORT_CARRIERS for carrier in selected):
                raise ValueError("A delivery report requires a paired report carrier for every selected recipient")
        deliveries.append(single)

    def send_one(single):
        try:
            return _send(root, single, attachment=attachment, group=group, linked_to=link)
        except (OSError, RuntimeError, ValueError) as exc:
            if len(deliveries) == 1:
                raise
            return {"ok": False, "id": single.id, "host": single.to, "client": single.client, "session": single.session,
                    "status": "delivery_error", "error_message": str(exc)}

    if len(deliveries) == 1:
        return send_one(deliveries[0])
    fields = ("id", "host", "client", "session", "ok", "status", "transport", "route_attempts", "inbox_status", "session_status",
              "current_turn_accepted", "return_session_status", "return_address_known", "bidirectional_ready",
              "return_error_code", "return_next_action", "error_code", "error_message", "session_accepted",
              "communication_intent", "intent_delivery")
    fingerprint = stable_id("plan", plan_group(group, root), args.text, args.subject, args.kind, link, (attachment or {}).get("sha256"),
                            device_key(args.return_host, root), args.return_client, args.return_session, args.return_codex_home,
                            args.return_claude_home, args.return_queue, args.one_way, args.steer, args.queue, args.idle_only)
    if args.delivery_report:
        fingerprint = stable_id("delivery-report", fingerprint)
    if args.intent is not None:
        fingerprint = stable_id("intent", fingerprint, args.intent)
    done = batch_state(root, actor, args.id, fingerprint)

    def submit(single):
        if single.id in done:
            saved = done[single.id]
            return {"preflight": {"status": "not_recorded"}, "communication_intent": args.intent,
                    "intent_delivery": "unspecified" if args.intent is None else "unverified",
                    **saved, "previously_accepted": True}
        raw = send_one(single)
        result = {k: raw[k] for k in fields if k in raw}
        result["preflight"] = _batch_preflight(raw.get("preflight"))
        result.setdefault("communication_intent", args.intent)
        result.setdefault("intent_delivery", "unspecified" if args.intent is None else "unverified")
        if result.get("ok"):
            directory = transport.directory(root)
            receipt = {**result, "host": directory.mail_device(result["host"])} if directory.legacy_clients else result
            batch_state(root, actor, args.id, fingerprint, receipt=receipt)
        return result

    with ThreadPoolExecutor(max_workers=min(4, len(deliveries))) as pool:
        futures = [pool.submit(copy_context().run, submit, single) for single in deliveries]
        result = [future.result() for future in futures]
    ok = all(r.get("ok") for r in result)
    status = "group_delivered" if ok else "group_incomplete"
    if ok and any(r.get("status") in {"broker_acknowledged", "queue_acknowledged"} for r in result):
        status = "group_submitted"
    return {"ok": ok, "status": status, "id": args.id,
            "group": group, "deliveries": result, "sender_endpoint": actor}
