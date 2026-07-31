from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from .kimi_provider import ask_kimi_direct
from .openclaw_provider import ask_openclaw_direct

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSettings:
    primary_provider: str
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
            primary_provider=os.environ.get("PRIMARY_PROVIDER", "openclaw")
            .strip()
            .lower(),
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
) -> tuple[str, str]:
    primary = (
        settings.primary_provider
        if settings.primary_provider in {"openclaw", "kimi"}
        else "openclaw"
    )
    fallback = (
        settings.fallback_provider
        if settings.fallback_provider in {"openclaw", "kimi"}
        else ("kimi" if primary == "openclaw" else "openclaw")
    )

    try:
        if primary == "openclaw":
            return (
                ask_openclaw_direct(
                    text,
                    user,
                    files_with_meta,
                    settings.openclaw_agent,
                    settings.openclaw_session_key,
                    settings.openclaw_base_url,
                    settings.openclaw_model,
                ),
                "openclaw",
            )
        return (
            ask_kimi_direct(
                text,
                user,
                files_with_meta,
                auth_debug=auth_debug,
                base_url=settings.kimi_base_url,
            ),
            "kimi",
        )
    except Exception as e:
        log.warning("primary provider %s failed, falling back to %s: %s", primary, fallback, e)
        if not settings.enable_provider_fallback or fallback == primary:
            raise

        if fallback == "openclaw":
            return (
                ask_openclaw_direct(
                    text,
                    user,
                    files_with_meta,
                    settings.openclaw_agent,
                    settings.openclaw_session_key,
                    settings.openclaw_base_url,
                    settings.openclaw_model,
                ),
                "openclaw",
            )
        return (
            ask_kimi_direct(
                text,
                user,
                files_with_meta,
                auth_debug=auth_debug,
                base_url=settings.kimi_base_url,
            ),
            "kimi",
        )
