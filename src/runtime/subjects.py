"""Short, deterministic mailbox subjects. No model call is needed to supply one."""

import re
import unicodedata


def _width(char):
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def normalize_subject(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A subject must be nonempty")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in value):
        raise ValueError("Write the subject on one line without control characters")
    text = " ".join(value.split())
    if sum(map(_width, text)) > 96:
        raise ValueError("Use a short subject (up to 48 full-width or 96 half-width characters)")
    return text


def preview_subject(body):
    if not isinstance(body, str):
        return None
    lines = [" ".join(s.split()).strip() for s in re.split(r"[\r\n。！？!?]|(?<=\.)\s+", body)]
    lines = [s for s in lines if s]
    if not lines:
        return "简短消息"
    line = next((s for s in lines if s.casefold().strip("，,：:.") not in
                 {"你好", "您好", "hi", "hello", "大家好"}), lines[0])
    clean = "".join(ch for ch in line if unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Zl", "Zp"})
    if not clean.strip():
        return "简短消息"
    if sum(map(_width, clean)) <= 48:
        return clean
    used, result = 0, []
    for ch in clean:
        used += _width(ch)
        if used > 46:
            break
        result.append(ch)
    return "".join(result).rstrip() + "…"
