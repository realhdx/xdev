"""Negotiate mail capabilities with explicitly configured, read-only local catalogs."""

import re
import subprocess
from uuid import uuid4

from src.runtime import transport

PROTOCOL = "xdev.host-catalog.v1"
TEMPLATE = "xdev.receiver.v1"
PLACEMENTS = {"current_turn", "next_turn", "original_session"}
MAX_CATALOGS = 8
MAX_CLIENTS = 128


def sources(root, client=None):
    from src.adapters import portable, sessions

    configured = transport.config(root).get("session_host_catalogs", {})
    if not isinstance(configured, dict) or len(configured) > MAX_CATALOGS:
        raise ValueError("Configure at most eight approved local session_host_catalogs")
    found = {}
    for name, binding in configured.items():
        sessions.client_name(name)
        if isinstance(binding, dict) and binding.get("type") == "mcp-stdio":
            from src.adapters import mcp_catalog

            mcp_catalog.validate(binding)
            if client is None or client == binding["client"]:
                found[name] = binding
            continue
        if not isinstance(binding, dict) or set(binding) - {"argv", "clients"}:
            raise ValueError("Catalog entries accept a fixed local argv and an optional client allowlist")
        argv = portable._argv(binding)
        allow = binding.get("clients")
        if allow is not None:
            if not isinstance(allow, list) or not 1 <= len(allow) <= MAX_CLIENTS:
                raise ValueError("Catalog clients must list the permitted client identities")
            for value in allow:
                sessions.client_name(value)
            if len(set(allow)) != len(allow):
                raise ValueError("Duplicate catalog client grant")
        if not allow or client is None or client in allow:
            found[name] = {"argv": argv, "clients": allow}
    return found


def _call(argv, root, provider, op, *, timeout=3, **payload):
    from src.adapters import portable

    probe_id = uuid4().hex
    frame = {"protocol": PROTOCOL, "provider": provider, "op": op, "request_id": probe_id, **payload}
    answer = portable._invoke(argv, frame, root, timeout=timeout, maximum=65536)
    required = {"protocol": PROTOCOL, "provider": provider, "op": op, "request_id": probe_id}
    for field in ("client", "session", "instance"):
        if field in payload:
            required[field] = payload[field]
    if not isinstance(answer, dict) or any(answer.get(key) != value for key, value in required.items()):
        raise ValueError("Catalog response did not match the requested provider, operation or exact address")
    return answer


def _entries(answer, allowed):
    from src.adapters import sessions

    instance = answer.get("instance")
    if not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", instance):
        raise ValueError("Missing host instance identity")
    rows = answer.get("clients")
    if answer.get("status") != "ok" or not isinstance(rows, list) or len(rows) > MAX_CLIENTS:
        raise ValueError("Missing or oversized host catalog")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Invalid client declaration")
        name = sessions.client_name(row.get("client"))
        if name in result:
            raise ValueError("Duplicate client in provider catalog")
        # A general tool directory can contain unrelated or newer templates.
        # Only a negotiated, versioned semantic contract can receive a letter.
        template = row.get("template")
        if template != TEMPLATE:
            result[name] = {"supported": False}
            continue
        values = row.get("placements")
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= len(PLACEMENTS)
            or not all(isinstance(v, str) and v in PLACEMENTS for v in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("Invalid or repeated delivery placement")
        result[name] = {"supported": True, "template": TEMPLATE, "placements": sorted(values)}
    return instance, {key: value for key, value in result.items() if allowed is None or key in allowed}


def discover(root, client=None, session=None):
    """Returns public evidence and, separately, the one local dispatch route if resolved."""
    from src.adapters import sessions

    if client is not None:
        sessions.client_name(client)
    if session is not None:
        if client is None:
            raise ValueError("An exact session requires a client")
        session = sessions.canonical_session(client, session)
    evidence = []
    routes = []
    incomplete = False
    for name, binding in sources(root, client).items():
        entry = {"provider": name}
        evidence.append(entry)
        try:
            if binding.get("type") == "mcp-stdio":
                from src.adapters import mcp_catalog

                info, identity = mcp_catalog.call(root, binding, session)
                entry.update(status=info.get("status", "advertised"), clients={binding["client"]: info})
                if identity is not None:
                    routes.append(
                        {
                            "provider": name,
                            "mcp": binding,
                            "identity": identity,
                            "session": session,
                            "placements": info["placements"],
                        }
                    )
                continue
            argv, allowed = binding["argv"], binding["clients"]
            answer = _call(argv, root, name, "catalog")
            instance, clients = _entries(answer, allowed)
            entry.update(
                status="advertised",
                clients=clients if client is None else {client: clients[client]} if client in clients else {},
            )
            if session is None or client not in clients or not clients[client]["supported"]:
                continue
            resolved = _call(argv, root, name, "resolve", instance=instance, client=client, session=session)
            if resolved.get("status") in ("not_found", "inactive"):
                entry["status"] = resolved["status"]
                continue
            if resolved.get("status") != "ready" or resolved.get("original") is not True:
                raise ValueError("Host could not verify ownership of the original chat")
            entry["status"] = "exact_original"
            routes.append(
                {
                    "provider": name,
                    "argv": argv,
                    "client": client,
                    "session": session,
                    "instance": instance,
                    "placements": clients[client]["placements"],
                }
            )
        except (ValueError, TypeError, OSError, RecursionError, subprocess.TimeoutExpired):
            incomplete = True
            entry["status"] = "unverified"
            entry["reason"] = "provider_response_unverified"
    if len(routes) > 1:
        status = "ambiguous"
    elif incomplete:
        status = "incomplete"
    elif routes:
        status = "resolved"
    else:
        status = "catalogued" if session is None else "unresolved"
    return (
        {
            "ok": status in ("resolved", "catalogued"),
            "status": status,
            "providers": evidence,
            "probe": "read_only",
            "wake_requests": 0,
        },
        routes[0] if status == "resolved" else None,
    )


def submit(root, route, notice):
    from src.adapters import portable

    requested = "next_turn" if notice["mode"] == "queue" else "current_turn"
    if requested not in route["placements"] and not (
        requested == "next_turn" and "original_session" in route["placements"]
    ):
        return {"status": "placement_unavailable"}
    if "mcp" in route:
        from src.adapters import mcp_catalog

        info, ack = mcp_catalog.call(root, route["mcp"], route["session"], notice, route["identity"])
        # A normal non-admission here follows read-only checks. Exceptions after
        # tools/call may follow a write, and remain unknown at the caller.
        return (
            ack
            if ack is not None
            else {"status": "requires_activation", "reason": info.get("reason", info.get("status"))}
        )
    answer = _call(
        route["argv"],
        root,
        route["provider"],
        "submit",
        timeout=portable.TIMEOUT,
        instance=route["instance"],
        client=route["client"],
        session=route["session"],
        template=TEMPLATE,
        notice=notice,
    )
    ack = answer.get("ack")
    if not portable._matches_notice(ack, notice):
        return None
    if ack.get("status") == "accepted" and ack.get("placement") not in route["placements"]:
        return None
    return ack
