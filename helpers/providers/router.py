from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .kimiclaw_provider import MODEL_COMMAND_RE, ask_kimiclaw
from .openclaw_provider import ask_openclaw_direct

SUPPORTED_PROVIDERS = frozenset({"kimiclaw", "openclaw"})


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
    provider: str = "kimiclaw"

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str):
            raise TypeError("provider must be a string")
        provider = self.provider.strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ValueError(
                f"provider {self.provider!r} is not supported; expected one of: "
                f"{supported}"
            )
        object.__setattr__(self, "provider", provider)

    @staticmethod
    def from_env() -> ProviderSettings:
        return ProviderSettings(
            openclaw_agent=os.environ.get("OPENCLAW_AGENT", "main"),
            openclaw_session_key=os.environ.get(
                "OPENCLAW_SESSION_KEY", "agent:main:gchat:jinx"
            ),
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
            provider=os.environ.get("GCHAT_PROVIDER", "kimiclaw"),
        )


def ask_provider(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    settings: ProviderSettings,
    quoted_message: dict[str, str] | None = None,
) -> tuple[str, str, list[dict[str, str]]]:
    try:
        if settings.provider == "kimiclaw" or MODEL_COMMAND_RE.match(text.strip()):
            provider = "kimiclaw"
            result = ask_kimiclaw(
                text,
                user,
                files_with_meta,
                settings.openclaw_session_key,
                quoted_message,
            )
        else:
            provider = "openclaw"
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
        # result is dict {text, files}
        return result.get("text", ""), provider, result.get("files", [])
    except Exception as e:
        reason = _extract_error_reason(e)
        raise RuntimeError(f"เกิดข้อผิดพลาด: {reason}") from e
