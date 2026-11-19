"""Data-only delivery policy shared by receiver adapters.

``send_now`` means native admission into the active workflow, not proof that a
model has read or processed the notice. Provider adapters still own identity,
authorization, deduplication, native ACK validation, and translating this mode
to their API. A descriptor never names executable code.

Descriptors reuse capabilities' ``default_delivery``, ``delivery_options`` and
``explicit_queue``. Optional ``acceptance_rules`` are a list of data objects:
``{"scope": "current_turn", "all": {"native.ack": True, ...}}``. A list value
in ``all`` means one of those scalar values, not an expression. Dotted keys
only traverse dictionaries. Rules cannot execute or import anything.
"""

import math
import re


MODES = ("send_now", "queue", "idle_only", "unsupported_requires_binding")
SCOPES = ("current_turn", "next_turn", "original_session", "surface", "unknown")
_ALIASES = {"send_now": "send_now", "steer": "send_now",
            "send_now_when_registered": "send_now", "queue": "queue", "idle_only": "idle_only"}
_SUCCESS = ["admitted", "already_admitted"]

# These are adapter-return contracts, not client-name branches. In particular,
# a legacy admitted result without mode/evidence remains unknown.
DEFAULT_ACCEPTANCE_RULES = [
    {"scope": "next_turn", "all": {"ok": True, "status": ["queued", "already_queued"]}},
    {"scope": "surface", "all": {"ok": True, "status": "input_surface_accepted",
                                    "admission_evidence": "native_input_surface_ack"}},
    {"scope": "current_turn", "all": {"ok": True, "status": _SUCCESS,
        "delivery_mode": "send_now", "admission_evidence": "native_send_now_accepted"}},
    {"scope": "current_turn", "all": {"ok": True, "status": _SUCCESS,
        "delivery_mode": "steer", "admission.accepted": True}},
    {"scope": "current_turn", "all": {"ok": True, "status": _SUCCESS,
        "delivery_mode": "steer", "promotion.to_placement": "steering",
        "promotion.status": ["promoted", "already_steering"]}},
    {"scope": "current_turn", "all": {"ok": True, "status": _SUCCESS, "delivery": "steer"}},
    {"scope": "next_turn", "all": {"ok": True, "status": _SUCCESS,
        "delivery_mode": "queue", "admission.accepted": True}},
    {"scope": "next_turn", "all": {"ok": True, "status": _SUCCESS, "delivery": "queue"}},
]


def _data(value, depth=0):
    """Reject executable objects, excessive nesting, and non-JSON scalars."""
    if depth > 8:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return len(value) <= 128 and all(_data(item, depth + 1) for item in value)
    if type(value) is dict:
        return (len(value) <= 128 and all(type(key) is str and _data(item, depth + 1)
                                        for key, item in value.items()))
    return False


def select_delivery(descriptor, steer=False, queue=False, idle_only=False):
    """Choose a canonical mode without doing I/O or silently queueing.

    False/unknown capabilities fail closed. A descriptor whose only available
    operation is queue still needs the caller's explicit ``queue=True``.
    Invalid/conflicting flags raise ValueError before any delivery side effect.
    """
    flags = (steer, queue, idle_only)
    if any(type(flag) is not bool for flag in flags) or sum(flags) > 1:
        raise ValueError("Delivery options must be booleans and mutually exclusive")
    if type(descriptor) is not dict or not _data(descriptor):
        return "unsupported_requires_binding"
    options = descriptor.get("delivery_options", [])
    if type(options) is not list or any(type(option) is not str for option in options):
        return "unsupported_requires_binding"
    supported = {_ALIASES[option] for option in options if option in _ALIASES}
    configured_default = descriptor.get("default_delivery")
    default = _ALIASES.get(configured_default) if type(configured_default) is str else None
    if descriptor.get("explicit_queue") is True:
        supported.add("queue")
    selected = ("send_now" if steer else "queue" if queue else "idle_only" if idle_only
                else "send_now" if default == "send_now" else None)
    if selected is None or selected not in supported or (selected == "queue" and not queue):
        return "unsupported_requires_binding"
    return selected


def _scalar(value):
    return value is None or type(value) in (str, bool, int, float)


def _same(left, right):
    # Python's True == 1 must not turn a malformed ACK into acceptance.
    return type(left) is type(right) and left == right


def _rules(descriptor):
    if descriptor is None:
        return DEFAULT_ACCEPTANCE_RULES
    if type(descriptor) is not dict or not _data(descriptor):
        return []
    rules = descriptor.get("acceptance_rules", DEFAULT_ACCEPTANCE_RULES)
    if type(rules) is not list or len(rules) > 64:
        return []
    valid = []
    for rule in rules:
        if (type(rule) is not dict or set(rule) != {"scope", "all"}
                or rule.get("scope") not in SCOPES[:-1]
                or type(rule.get("all")) is not dict or not 1 <= len(rule["all"]) <= 32):
            return []
        for key, expected in rule["all"].items():
            if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,5}", key)
                    or not (_scalar(expected) or type(expected) is list and 1 <= len(expected) <= 32
                            and all(_scalar(value) for value in expected))):
                return []
        valid.append(rule)
    return valid


def _matched(result, conditions):
    evidence = {}
    for path, expected in conditions.items():
        observed = result
        for key in path.split("."):
            if type(observed) is not dict or key not in observed:
                return None
            observed = observed[key]
        choices = expected if type(expected) is list else [expected]
        if not any(_same(observed, choice) for choice in choices):
            return None
        evidence[path] = observed
    return evidence


def acceptance(result, requestedmode, descriptor=None):
    """Normalize native admission evidence, never model-read/processing status.

    Optional data rules describe a new provider's already validated return
    contract. Conflicting evidence fails closed. Requested mode is recorded;
    it cannot upgrade observed queue/surface evidence to current-turn admission.
    """
    if requestedmode not in MODES:
        raise ValueError("Unknown canonical delivery mode")
    answer = {"requested_mode": requestedmode, "scope": "unknown", "current_turn": False,
              "next_turn": False, "original_session": False, "surface": False, "evidence": {},
              "model_processing": "unverified"}
    if type(result) is not dict or result.get("ok") is not True or requestedmode == "unsupported_requires_binding":
        return answer
    if result.get("status") in ("unknown", "requires_activation", "not_registered", "requires_binding"):
        return answer
    if result.get("outcome") == "unknown" and result.get("status") != "input_surface_accepted":
        return answer
    # A custom descriptor cannot upgrade an explicit queue/surface ACK. These
    # terms are reserved cross-adapter contracts even for third-party rules.
    if result.get("status") in ("queued", "already_queued"):
        answer.update(scope="next_turn", next_turn=True,
                      evidence={"ok": True, "status": result["status"]})
        return answer
    surface = result.get("status") == "input_surface_accepted"
    matches = {}
    for rule in _rules(descriptor):
        evidence = _matched(result, rule["all"])
        if evidence is not None:
            matches.setdefault(rule["scope"], {}).update(evidence)
    if len(matches) == 1:
        scope, evidence = next(iter(matches.items()))
        if not surface or scope == "surface":
            if result.get("outcome") != "unknown" or scope == "surface":
                answer.update(scope=scope, evidence=evidence)
                answer[scope] = True
    elif matches:
        answer["evidence"] = {"conflicting_scopes": sorted(matches)}
    return answer
