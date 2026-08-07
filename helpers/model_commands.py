from __future__ import annotations

import re

MODEL_COMMAND_RE = re.compile(r"^/model(?:\s|$)", re.IGNORECASE)
_MODEL_COMMAND_WITH_KEY_RE = re.compile(
    r"^/model[ \t]+(?P<model_key>[^ \t\r\n]+)[ \t]*$",
    re.IGNORECASE,
)


def is_model_command(text: str) -> bool:
    return MODEL_COMMAND_RE.match(text.strip()) is not None


def parse_model_key(text: str) -> str | None:
    match = _MODEL_COMMAND_WITH_KEY_RE.fullmatch(text.strip())
    if match is None:
        return None
    return match.group("model_key")
