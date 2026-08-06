from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from helpers.session_keys import (
    SESSION_AGENT,
    generate_session_key,
)

from .openclaw_logcheck import check_run_errors
from .openclaw_provider import ask_openclaw_direct

LOCAL_FILE_ACCESS_ENV = "OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS"
LOCAL_FILE_ACCESS_POLICIES = frozenset({"auto", "allow", "deny"})


def _extract_error_reason(err: Exception) -> str:
    detail = str(err).replace("\n", " ").strip()

    def from_payload(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        error_obj = payload.get("error", {})
        if isinstance(error_obj, dict):
            message = error_obj.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return ""

    candidates = [detail]
    if "):" in detail:
        candidates.append(detail.split("):", 1)[1].strip())

    for candidate in candidates:
        try:
            reason = from_payload(json.loads(candidate))
            if reason:
                return reason
        except json.JSONDecodeError:
            continue

    return detail[:500]


@dataclass(frozen=True)
class ProviderSettings:
    openclaw_agent: str
    openclaw_session_key: str
    openclaw_base_url: str
    openclaw_model: str

    def __post_init__(self) -> None:
        if self.openclaw_agent != SESSION_AGENT:
            raise ValueError(f"openclaw_agent must be {SESSION_AGENT!r}")

    @staticmethod
    def from_env() -> ProviderSettings:
        return ProviderSettings(
            openclaw_agent=SESSION_AGENT,
            openclaw_session_key=generate_session_key(),
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )


def provider_has_local_file_access(settings: ProviderSettings) -> bool:
    policy = os.environ.get(LOCAL_FILE_ACCESS_ENV, "auto").strip().lower()
    if policy not in LOCAL_FILE_ACCESS_POLICIES:
        expected = ", ".join(sorted(LOCAL_FILE_ACCESS_POLICIES))
        raise ValueError(f"{LOCAL_FILE_ACCESS_ENV} must be one of: {expected}")
    if policy == "allow":
        return True
    if policy == "deny":
        return False

    endpoint = settings.openclaw_base_url
    try:
        host = (urlsplit(endpoint).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ask_provider(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    settings: ProviderSettings,
    quoted_message: dict[str, str] | None = None,
) -> tuple[str, str]:
    try:
        provider = "openclaw"
        sent_at = datetime.now()
        result = ask_openclaw_direct(
            text,
            user,
            files_with_meta,
            settings.openclaw_agent,
            settings.openclaw_session_key,
            settings.openclaw_base_url,
            settings.openclaw_model,
            quoted_message,
        )
        print(f"🔀 [provider] provider={provider}")
        run_id = result.get("run_id", "")
        for line in check_run_errors(run_id, sent_at):
            print(f"⚠️ [logcheck] run={run_id}: {line}")
        return result.get("text", ""), provider
    except Exception as e:
        reason = _extract_error_reason(e)
        raise RuntimeError(f"เกิดข้อผิดพลาด: {reason}") from e
