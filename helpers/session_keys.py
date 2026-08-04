from __future__ import annotations

import uuid

SESSION_AGENT = "main"
SESSION_CHANNEL = "gchat"
SESSION_ID_HEX_LENGTH = 6
SESSION_KEY_PREFIX = f"agent:{SESSION_AGENT}:{SESSION_CHANNEL}:"


def generate_session_key() -> str:
    return f"{SESSION_KEY_PREFIX}{uuid.uuid4().hex[:SESSION_ID_HEX_LENGTH]}"
