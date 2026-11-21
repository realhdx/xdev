"""Match approved roles against the tool schemas of an existing standard MCP stdio server."""

import asyncio
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from src.adapters import host_catalog

IDENT = re.compile(r"[\w./:-]{1,128}", re.ASCII)
FIELDS = {
    "resolve": ({"session"}, {"session", "original", "instance"}),
    "submit": ({"session", "notice_id", "text"}, {"session", "notice_id", "accepted", "placement"}),
}


def validate(binding):
    from src.adapters import portable, sessions

    if set(binding) - {"type", "argv", "server_name", "client", "roles", "placements", "accept_declared_roles"}:
        raise ValueError("Unknown MCP catalog configuration field")
    portable._argv(binding)
    sessions.client_name(binding.get("client"))
    if not isinstance(binding.get("server_name"), str) or not 1 <= len(binding["server_name"]) <= 128:
        raise ValueError("Pin the MCP server's declared implementation name")
    roles = binding.get("roles", {})
    if type(binding.get("accept_declared_roles", False)) is not bool or (
        binding.get("accept_declared_roles") and roles
    ):
        raise ValueError("Select a trusted local role map or explicitly allow provider role metadata")
    if not isinstance(roles, dict) or set(roles) - FIELDS.keys():
        raise ValueError("Configure resolve and submit roles from trusted host documentation")
    for role, mapping in roles.items():
        if (
            not isinstance(mapping, dict)
            or set(mapping) != {"tool", "inputs", "outputs"}
            or not isinstance(mapping["tool"], str)
            or not IDENT.fullmatch(mapping["tool"])
        ):
            raise ValueError("Each mapped role needs its tool, input and output field names")
        expected_in, expected_out = FIELDS[role]
        for key, required in (("inputs", expected_in), ("outputs", expected_out)):
            names = mapping[key]
            allowed = required | (
                {"mode", "message_id", "instance"} if role == "submit" and key == "inputs" else set()
            )
            if (
                not isinstance(names, dict)
                or not required <= names.keys() <= allowed
                or not all(isinstance(n, str) and IDENT.fullmatch(n) for n in names.values())
                or len(set(names.values())) != len(names)
            ):
                raise ValueError("Invalid or aliased semantic fields in MCP role")
    placements = binding.get("placements", [])
    if (
        not isinstance(placements, list)
        or not all(isinstance(s, str) and s in host_catalog.PLACEMENTS for s in placements)
        or len(set(placements)) != len(placements)
        or ("submit" in roles and not placements)
    ):
        raise ValueError("Declare the placements the mapped submit operation supports")
    if len(placements) > 1 and "mode" not in roles.get("submit", {}).get("inputs", {}):
        raise ValueError("A multi-mode MCP endpoint needs a mapped mode argument")


def _declared(binding, tools):
    """Only a locally opted-in service can supply role/field semantics."""
    roles = {}
    placements = []
    for tool in tools.values():
        metadata = (tool.meta or {}).get("io.xdev/receiver")
        if not isinstance(metadata, dict) or metadata.get("client") != binding["client"]:
            continue
        role = metadata.get("role")
        if metadata.get("version") != 1 or type(metadata.get("version")) is not int or role not in FIELDS:
            continue
        if role in roles:
            raise ValueError("Ambiguous MCP role declarations")
        roles[role] = {"tool": tool.name, "inputs": metadata.get("inputs"), "outputs": metadata.get("outputs")}
        if role == "submit":
            placements = metadata.get("placements", [])
    resolved = {**binding, "accept_declared_roles": False, "roles": roles, "placements": placements}
    validate(resolved)
    if binding.get("placements") and not set(placements).issubset(binding["placements"]):
        raise ValueError("The advertised roles exceed the local placement grant")
    return resolved


def _checked(mapping, tool, role):
    if tool is None:
        return "mapped_tool_absent"
    readonly = getattr(tool.annotations, "readOnlyHint", None)
    if (role == "resolve" and readonly is not True) or (role == "submit" and readonly is True):
        return "tool_effect_mismatch"
    # A supplied schema is data. Never dereference a supplier URL while checking a tool.
    for doc in (tool.inputSchema, tool.outputSchema):
        if doc is not None and _external_ref(doc):
            return "external_schema_reference"
    schema = tool.inputSchema
    props = schema.get("properties", {})
    required = schema.get("required", [])
    fields = set(mapping["inputs"].values())
    if (
        schema.get("type") != "object"
        or not isinstance(props, dict)
        or not isinstance(required, list)
        or not all(isinstance(k, str) for k in required)
        or set(required) - fields
        or any(not isinstance(props.get(field), dict) or props[field].get("type") != "string" for field in fields)
    ):
        return "input_schema_mismatch"
    return None


def _external_ref(value):
    if isinstance(value, dict):
        if any(
            k in value and (not isinstance(value[k], str) or not value[k].startswith("#"))
            for k in ("$ref", "$dynamicRef", "$recursiveRef")
        ):
            return True
        return any(_external_ref(v) for v in value.values())
    return isinstance(value, list) and any(_external_ref(v) for v in value)


def _output(result, mapping):
    if result.isError:
        raise ValueError("Mapped tool returned an error")
    value = result.structuredContent
    if value is None and len(result.content) == 1 and result.content[0].type == "text":
        text = result.content[0].text
        if len(text) <= 65536:
            value = json.loads(text)
    if not isinstance(value, dict) or not set(mapping["outputs"].values()) <= value.keys():
        raise ValueError("Mapped tool did not return its declared evidence")
    return {field: value[key] for field, key in mapping["outputs"].items()}


def _accepts(schema, arguments):
    # Shipped with the optional MCP SDK. Use a closed registry even when a
    # provider embeds a base URI in an otherwise self-contained schema.
    from jsonschema.validators import validator_for
    from referencing import Registry

    validator = validator_for(schema)
    validator.check_schema(schema)
    return validator(schema, registry=Registry()).is_valid(arguments)


async def _operation(api, binding, session, notice, expected):
    init = await api.initialize()
    if init.serverInfo.name != binding["server_name"]:
        raise ValueError("MCP server identity mismatch")
    info = {"client": binding["client"], "supported": False, "source": "mcp_tools_list"}
    if init.capabilities.tools is None:
        info["reason"] = "server_has_no_tools"
        return info, None
    tools = {}
    cursor = None
    for _ in range(8):
        page = await api.list_tools(cursor=cursor)
        for tool in page.tools:
            if tool.name in tools or len(tools) >= 128:
                raise ValueError("Duplicate or oversized MCP tool catalog")
            tools[tool.name] = tool
        cursor = page.nextCursor
        if not cursor:
            break
    else:
        raise ValueError("MCP catalog pagination exceeded limit")
    info["tool_count"] = len(tools)
    info["tools"] = [
        {
            "name": t.name,
            "read_only_hint": getattr(t.annotations, "readOnlyHint", None),
            "input_fields": sorted(k for k in t.inputSchema.get("properties", {}) if IDENT.fullmatch(k))[:64],
        }
        for t in tools.values()
        if IDENT.fullmatch(t.name) and isinstance(t.inputSchema.get("properties", {}), dict)
    ]
    if binding.get("accept_declared_roles"):
        binding = _declared(binding, tools)
    roles = binding.get("roles", {})
    if not roles:
        info["reason"] = (
            "read_only_catalog"
            if tools and all(getattr(t.annotations, "readOnlyHint", None) is True for t in tools.values())
            else "no_semantic_mapping"
        )
        return info, None
    for role, mapping in roles.items():
        reason = _checked(mapping, tools.get(mapping["tool"]), role)
        if reason:
            info["reason"] = reason
            return info, None
    if "resolve" not in roles or "submit" not in roles:
        info["reason"] = "read_only_mapping" if "resolve" in roles else "missing_original_lookup"
        return info, None
    info.update(supported=True, template="mcp.tools.v1", placements=sorted(binding["placements"]))
    if session is None:
        return info, None
    resolve = roles["resolve"]
    lookup = {resolve["inputs"]["session"]: session}
    if not _accepts(tools[resolve["tool"]].inputSchema, lookup):
        info.update(status="not_found", reason="lookup_arguments_outside_schema")
        return info, None
    read = _output(await api.call_tool(resolve["tool"], lookup), resolve)
    if read["session"] != session or type(read["original"]) is not bool:
        raise ValueError("MCP lookup did not verify the addressed session")
    if not read["original"]:
        info["status"] = "not_found"
        return info, None
    instance = read["instance"]
    if not isinstance(instance, str) or not IDENT.fullmatch(instance):
        raise ValueError("MCP lookup lacks an original host instance")
    # Bind the inventory actually observed to the route. Re-openings verify both
    # the same original host and the same mapped tool contracts before writing.
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                init.serverInfo.name,
                init.serverInfo.version,
                sorted(binding["placements"]),
                [
                    [r, m, tools[m["tool"]].inputSchema, tools[m["tool"]].outputSchema]
                    for r, m in sorted(roles.items())
                ],
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    identity = (instance, fingerprint)
    if expected is not None and expected != identity:
        info.update(status="unverified", reason="host_or_schema_changed_before_submit")
        return info, None
    info["status"] = "exact_original"
    if notice is None:
        return info, identity
    mode = notice["mode"]
    from src.adapters.notices import body as notice_body

    sender = notice["sender"]
    summary = {
        "envelope_version": 2,
        "from": sender["device"],
        "to": notice["device"],
        "subject": notice.get("subject"),
        "sender_identity_source": "explicit",
        "sender_endpoint": sender if "session" in sender else None,
    }
    values = {
        "session": session,
        "notice_id": notice["notice_id"],
        "text": notice_body(summary, notice["message_id"], notice["client"], session),
        "mode": mode,
        "instance": instance,
        "message_id": notice["message_id"],
    }
    mapping = roles["submit"]
    args = {actual: values[semantic] for semantic, actual in mapping["inputs"].items()}
    if not _accepts(tools[mapping["tool"]].inputSchema, args):
        info.update(status="unverified", reason="notice_arguments_outside_schema")
        return info, None
    ack = _output(await api.call_tool(mapping["tool"], args), mapping)
    if (
        ack["session"] != session
        or ack["notice_id"] != notice["notice_id"]
        or ack["accepted"] is not True
        or ack["placement"] not in binding["placements"]
    ):
        raise ValueError("Unverified MCP target or placement acknowledgement")
    return info, {
        **{k: notice[k] for k in ("protocol", "client", "session", "message_id", "notice_id")},
        "status": "accepted",
        "placement": ack["placement"],
    }


async def _run(root, binding, session, notice, expected, log):
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        raise ValueError("Install the optional MCP client SDK for MCP catalog discovery") from exc
    argv = binding["argv"]
    params = StdioServerParameters(command=argv[0], args=argv[1:], cwd=root)
    async with (
        # On Windows a cold stdio SDK may need to load its runtime before it
        # answers initialize. Keep a finite budget for the whole conversation.
        asyncio.timeout(40),
        stdio_client(params, errlog=log) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as api,
    ):
        return await _operation(api, binding, session, notice, expected)


def call(root, binding, session=None, notice=None, expected=None):
    try:
        with open(os.devnull, "w") as log:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_run(root, binding, session, notice, expected, log))
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, _run(root, binding, session, notice, expected, log)).result()
    except (ImportError, ExceptionGroup, OSError, RuntimeError, TimeoutError) as exc:
        raise ValueError("MCP catalog or tool response was not verified") from exc
