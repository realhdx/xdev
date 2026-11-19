"""Validate and dispatch notices to an AI client on this device only."""

import re
import importlib
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID


ADAPTERS = {
    "codex": {"backend": "registered_native_app_tools", "existing_session": True,
              "default_delivery": "send_now_when_registered",
              "unregistered": "requires_activation_no_implicit_queue",
              "requires_loaded_client": True, "requires_host_registration_for_send_now": True,
              "explicit_queue": True, "model_processing": "unverified"},
    "opencode": {"backend": "native_session_prompt", "existing_session": True,
                 "delivery_options": ["steer", "queue", "idle_only"], "default_delivery": "steer",
                 "model_processing": "unverified"},
    "claude": {"backend": "native_named_pipe_candidate", "existing_session": True,
               "native_version": "2.1.325", "peer_protocol": 1, "platform": "native_windows",
               "supported_image": "Desktop claude-code/2.1.325/<12-hex-cache>/claude.exe only",
               "requires_live_registration": True, "offline": "requires_activation",
               "write_without_ack": "unknown", "admission_evidence": "original_trace_user_marker",
               "gui_fallback": "explicit_existing_chat_workflow_required_not_automated",
               "model_processing": "unverified"},
    "dsh": {"backend": "same_host_session_controller_plugin", "existing_session": True,
            "requires_loaded_plugin": True, "requires_active_root_session": True,
            "delivery_options": ["steer", "queue", "idle_only"], "default_delivery": "steer",
            "steer_timing": "next_native_step_does_not_abort_running_tools", "offline": "requires_activation",
            "admission_evidence": "native_accepted_true", "model_processing": "unverified"},
    "workbuddy": {"backend": "native_host_mod_input_surface_candidate", "existing_session": True,
                  "default_delivery": "unsupported_requires_binding",
                  "requires_loaded_plugin": True, "requires_exact_session_binding": True,
                  "delivery_options": ["queue"], "offline": "requires_activation",
                  "admission_evidence": "native_input_surface_ack_only",
                  "engine_queue_admission": "unverified", "model_processing": "unverified"},
    "antigravity": {"backend": "official_agentapi_sidecar", "existing_session": True,
                    "default_delivery": "unsupported_requires_binding", "delivery_options": ["queue"],
                    "requires_loaded_sidecar": True, "requires_exact_session_binding": True,
                    "busy_placement": "unverified", "model_processing": "unverified",
                    "acceptance_rules": [{"scope": "original_session", "all": {
                        "ok": True, "status": ["admitted", "already_admitted"],
                        "admission_evidence": "native_original_session_notice",
                        "native_write_confirmed": True, "delivery_scope": "original_session"}}]},
}
for _channel, _client in (("claude-channel", "claude"), ("codebuddy-channel", "workbuddy")):
    ADAPTERS[_channel] = {"backend": "official_mcp_channel", "physical_client": _client,
                         "existing_session": True, "requires_startup_opt_in": True,
                         "default_delivery": "unsupported_requires_binding", "delivery_options": ["queue"],
                         "write_without_ack": "unknown", "model_processing": "unverified",
                         "acceptance_rules": [{"scope": "original_session", "all": {
                             "ok": True, "status": "already_admitted",
                             "admission_evidence": "official_channel_receipt_tool",
                             "read_status": "explicit_native_tool_receipt"}}]}
CLIENTS = tuple(ADAPTERS)

# All shared routing decisions consume descriptors. Provider modules implement
# native I/O only; adding a client does not change the mailbox or transport.
_BINDINGS = {
    "codex": ("uuid", "", None, "codex_home", True),
    "opencode": ("pattern", "", "steer", None, False),
    "claude": ("uuid", "", None, "claude_home", False),
    "dsh": ("uuid", "session-", "steer", None, False),
    "workbuddy": ("uuid", "", None, None, False),
    "antigravity": ("uuid", "", None, None, False),
    "claude-channel": ("uuid", "", None, None, False),
    "codebuddy-channel": ("uuid", "", None, None, False),
}
for _name, (_kind, _prefix, _flag, _profile, _force_queue) in _BINDINGS.items():
    ADAPTERS[_name].update(module="src.adapters." + _name.replace("-", "_"), session_kind=_kind,
                           session_prefix=_prefix, native_send_now_flag=_flag,
                           profile_field=_profile, force_queue_option=_force_queue)
    ADAPTERS[_name].setdefault("default_delivery", "send_now")
    if _flag:
        ADAPTERS[_name]["delivery_options"] = ["send_now", "queue", "idle_only"]
    else:
        ADAPTERS[_name].setdefault("delivery_options", ["send_now", "queue"])
ADAPTERS["opencode"]["session_pattern"] = r"ses_[A-Za-z0-9_-]{1,100}"


def descriptor(client):
    if client not in ADAPTERS:
        raise ValueError("Supported AI clients are " + ", ".join(ADAPTERS))
    return deepcopy(ADAPTERS[client])


def canonical_session(client, session):
    spec = descriptor(client)
    if not isinstance(session, str) or not session:
        raise ValueError("Supply the selected client's existing session ID")
    if spec["session_kind"] == "uuid":
        prefix = spec.get("session_prefix", "")
        raw = session.removeprefix(prefix) if prefix else session
        try:
            canonical = str(UUID(raw))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Supply the canonical UUID of the existing " + client + " chat") from exc
        if canonical != raw.lower():
            raise ValueError("Supply the canonical UUID of the existing " + client + " chat")
        return prefix + canonical
    if not re.fullmatch(spec["session_pattern"], session):
        raise ValueError("Supply the ID of an existing " + client + " session")
    return session


def capabilities():
    return {"ok": True, "evidence_scope": "static_contract_not_host_loading_or_live_send_evidence",
            "sender_types": ["local_cli", "stdio_mcp"], "receivers": deepcopy(ADAPTERS),
            "default_policy": "current_turn_admission_required; queue_only_when_explicit",
            "stages": ["device_inbox", "current_turn", "next_turn", "original_session", "surface", "model_processing_unverified"],
            "no_automatic_permission_changes": True}


def validate(client, session, steer=False, idle_only=False, codex_home=None, claude_home=None):
    """Perform side-effect-free validation before any mailbox delivery."""
    spec = descriptor(client)
    canonical_session(client, session)
    if type(steer) is not bool or type(idle_only) is not bool or (steer and idle_only):
        raise ValueError("Invalid AI session delivery options")
    for field, value in (("codex-home", codex_home), ("claude-home", claude_home)):
        if value is not None:
            if spec.get("profile_field") != field.replace("-", "_"):
                raise ValueError(field + " is not supported by client " + client)
            if (not isinstance(value, str) or not value.strip()
                    or not (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute())):
                raise ValueError(field + " must be an absolute path on the recipient device")
    if (steer and spec.get("native_send_now_flag") != "steer") or (
            idle_only and "idle_only" not in spec["delivery_options"]):
        raise ValueError(client + " does not support steer or idle-only")


def submit(root, identifier, session, client="opencode", steer=False, idle_only=False, codex_home=None, claude_home=None, *, force_queue=False):
    """Use the client's own durable admission ledger; never infer model processing."""
    validate(client, session, steer, idle_only, codex_home, claude_home)
    session = canonical_session(client, session)
    from src.runtime import messages
    item = messages.handle(root, "wait", {"id": identifier, "timeout": 0}).get("message")
    if item is not None and item.get("kind") == "receipt":
        return {"ok": True, "status": "receipt_suppressed", "id": identifier,
                "session": session, "model_status": "not_started"}
    if item is not None and (item.get("recipient_identity") or item.get("recipient_identity_error")
                             or item.get("recipient_endpoint_error")):
        intended = item.get("recipient_identity")
        actual = messages.normalize_participant({
            "device": item["to"], "client": client, "session": session,
            **({"codex_home": codex_home} if codex_home else {}),
            **({"claude_home": claude_home} if claude_home else {}),
        })
        if (item.get("recipient_identity_error") or item.get("recipient_endpoint_error")
                or not messages.matches_participant(actual, intended, exact=True)):
            return {"ok": False, "status": "target_mismatch", "error_code": "SESSION_TARGET_MISMATCH",
                    "id": identifier, "session": session, "model_status": "not_started"}
    spec = descriptor(client)
    module = importlib.import_module(spec["module"])
    extra = []
    profile = spec.get("profile_field")
    if profile:
        extra.append({"codex_home": codex_home, "claude_home": claude_home}[profile])
    options = {}
    if force_queue:
        if not spec.get("force_queue_option"):
            raise ValueError("This provider has no separate native queue option")
        options["force_queue"] = True
    return module.submit(root, identifier, session, steer, idle_only, *extra, **options)


def notify(root, payload):
    allowed = {"id", "session", "client", "steer", "idle_only", "codex_home", "claude_home", "force_queue"}
    if payload.keys() - allowed:
        raise ValueError("Unknown AI notification arguments")
    if not isinstance(payload.get("id"), str) or not payload["id"].strip():
        raise ValueError("Supply the stored mailbox message ID")
    options = {}
    if "force_queue" in payload:
        if not descriptor(payload.get("client")).get("force_queue_option") or type(payload["force_queue"]) is not bool:
            raise ValueError("force_queue must be a boolean for a provider supporting it")
        options["force_queue"] = payload["force_queue"]
    return submit(root, payload["id"], payload.get("session"), payload.get("client"),
                  payload.get("steer", False), payload.get("idle_only", False), payload.get("codex_home"),
                  payload.get("claude_home"), **options)
