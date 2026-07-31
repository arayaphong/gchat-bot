from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .kimi_provider import ask_kimi_direct
from .openclaw_provider import ask_openclaw_direct

log = logging.getLogger(__name__)


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


def _fallback_notice(fallback: str, err: Exception) -> str:
    reason = _extract_error_reason(err)
    return (
        "พี่ Jinx ยังไม่พร้อมจ้า\n"
        f"เพราะว่า: {reason}\n"
        f"เลยสลับไป {fallback} ชั่วคราว"
    )


@dataclass(frozen=True)
class ProviderSettings:
    fallback_provider: str
    enable_provider_fallback: bool
    openclaw_agent: str
    openclaw_session_key: str
    openclaw_base_url: str
    openclaw_model: str
    kimi_base_url: str

    @staticmethod
    def from_env() -> ProviderSettings:
        return ProviderSettings(
            fallback_provider=os.environ.get("FALLBACK_PROVIDER", "kimi")
            .strip()
            .lower(),
            enable_provider_fallback=os.environ.get("ENABLE_PROVIDER_FALLBACK", "true")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            openclaw_agent=os.environ.get("OPENCLAW_AGENT", "main"),
            openclaw_session_key=os.environ.get(
                "OPENCLAW_SESSION_KEY", "agent:main:cli:default:gchat:jinx"
            ),
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
            kimi_base_url=os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
        )


def ask_with_provider_fallback(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    settings: ProviderSettings,
    *,
    auth_debug: bool = False,
    on_notice: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    primary = "openclaw"
    fallback = (
        settings.fallback_provider
        if settings.fallback_provider in {"openclaw", "kimi"}
        else "kimi"
    )

    def call_provider(provider: str) -> str:
        if provider == "openclaw":
            return ask_openclaw_direct(
                text,
                user,
                files_with_meta,
                settings.openclaw_agent,
                settings.openclaw_session_key,
                settings.openclaw_base_url,
                settings.openclaw_model,
            )
        return ask_kimi_direct(
            text,
            user,
            files_with_meta,
            auth_debug=auth_debug,
            base_url=settings.kimi_base_url,
        )

    try:
        primary_reply = call_provider(primary)
        return primary_reply, primary
    except Exception as e:
        log.warning(
            "primary provider %s failed, falling back to %s: %s", primary, fallback, e
        )
        if not settings.enable_provider_fallback or fallback == primary:
            reason = _extract_error_reason(e)
            raise RuntimeError(
                f"พี่ Jinx ยังไม่พร้อมจ้า\nเพราะว่า: {reason}"
            ) from e

        notice = _fallback_notice(fallback, e)
        if on_notice:
            on_notice(notice)
        try:
            fallback_reply = call_provider(fallback)
        except Exception as fallback_err:
            reason = _extract_error_reason(fallback_err)
            raise RuntimeError(
                f"ดูเหมือน {fallback} จะมีปัญหา เพราะว่า {reason}"
            ) from fallback_err
        return fallback_reply, fallback
