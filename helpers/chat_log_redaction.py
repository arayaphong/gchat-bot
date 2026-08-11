from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
MAX_DEPTH = "[MAX_DEPTH]"
UNSUPPORTED = "[UNSUPPORTED]"
_KEY_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_SENSITIVE_KEYS = {
    "authorization",
    "authorizationcode",
    "authorizationeventobject",
    "accesstoken",
    "clientsecret",
    "cookie",
    "credentials",
    "historyactionhandle",
    "idtoken",
    "oauthtoken",
    "password",
    "privatekey",
    "privatekeyid",
    "refreshtoken",
    "sessionkey",
    "setcookie",
}
_SENSITIVE_MARKERS = (
    "accesstoken",
    "apikey",
    "authorizationcode",
    "credential",
    "handle",
    "idtoken",
    "oauthtoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "sessionkey",
    "token",
)


def _normalized_key(key: str) -> str:
    return _KEY_SEPARATOR_RE.sub("", key.lower())


def _is_sensitive_key(normalized: str) -> bool:
    return normalized in _SENSITIVE_KEYS or any(
        marker in normalized for marker in _SENSITIVE_MARKERS
    )


def _safe_key(raw_key: object, ordinal: int) -> str:
    if isinstance(raw_key, str):
        return raw_key
    # Never call str() on an untrusted object. Its representation can contain a
    # secret or raise an exception while handling an otherwise valid event.
    return f"[NON_STRING_KEY_{ordinal}]"


def redact_chat_log_value(value: object, *, _depth: int = 0) -> Any:
    """Return a JSON-safe copy with Chat credentials/action handles removed."""

    if _depth >= 32:
        return MAX_DEPTH

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for ordinal, (raw_key, item) in enumerate(value.items()):
            key = _safe_key(raw_key, ordinal)
            if not isinstance(raw_key, str):
                redacted[key] = UNSUPPORTED
                continue
            normalized = _normalized_key(key)
            if normalized == "parameters":
                # Parameter names are attacker-controlled too. Redacting only
                # their values can still persist an opaque handle used as a key.
                redacted[key] = REDACTED
            elif _is_sensitive_key(normalized):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_chat_log_value(item, _depth=_depth + 1)
        return redacted

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_chat_log_value(item, _depth=_depth + 1) for item in value]

    if value is None or isinstance(value, str | int | float | bool):
        return value
    # Parsed Chat JSON is composed only of the types handled above. Do not call
    # repr()/str() for unexpected objects because either may disclose secrets.
    return UNSUPPORTED


__all__ = [
    "MAX_DEPTH",
    "REDACTED",
    "UNSUPPORTED",
    "redact_chat_log_value",
]
