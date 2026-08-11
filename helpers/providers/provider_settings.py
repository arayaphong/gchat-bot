from __future__ import annotations

from dataclasses import dataclass

from helpers.session_keys import SESSION_AGENT


@dataclass(frozen=True)
class ProviderSettings:
    openclaw_agent: str
    openclaw_base_url: str
    openclaw_model: str

    def __post_init__(self) -> None:
        if self.openclaw_agent != SESSION_AGENT:
            raise ValueError(f"openclaw_agent must be {SESSION_AGENT!r}")

    @staticmethod
    def from_env() -> ProviderSettings:
        return ProviderSettings(
            openclaw_agent=SESSION_AGENT,
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )
