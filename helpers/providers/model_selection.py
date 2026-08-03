from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from helpers.providers.openclaw_cli import get_default_model, list_sessions


@dataclass(frozen=True)
class ModelSelection:
    default_model: str
    session_model: str | None

    @property
    def effective_model(self) -> str:
        return self.session_model or self.default_model


def load_cli_json(result: subprocess.CompletedProcess[str], command_name: str) -> Any:
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:500]
        reason = f"{command_name} คืนค่ารหัส {result.returncode}"
        if detail:
            reason = f"{reason}: {detail}"
        raise RuntimeError(reason)

    raw_output = (result.stdout or "").lstrip("\ufeff").strip()
    if not raw_output:
        raise ValueError(f"{command_name} ไม่ส่งข้อมูลกลับมา")
    return json.loads(raw_output)


def find_session_model(payload: Any, session_key: str) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise TypeError("รูปแบบข้อมูล sessions จาก openclaw ไม่ถูกต้อง")

    for session in payload["sessions"]:
        if not isinstance(session, dict) or session.get("key") != session_key:
            continue

        provider = session.get("modelProvider")
        model = session.get("model")
        if (
            isinstance(provider, str)
            and provider.strip()
            and isinstance(model, str)
            and model.strip()
        ):
            return f"{provider.strip().rstrip('/')}/{model.strip().lstrip('/')}"
        raise TypeError("session ที่ตรงกับ key มีข้อมูล model ไม่ถูกต้อง")

    return None


def _get_default_model() -> str:
    default_model = load_cli_json(
        get_default_model(), "openclaw config get agents.defaults.model.primary"
    )
    if not isinstance(default_model, str) or not default_model.strip():
        raise TypeError("รูปแบบ default model จาก openclaw ไม่ถูกต้อง")
    return default_model.strip()


def _get_session_model(session_key: str) -> str | None:
    sessions_payload = load_cli_json(list_sessions(), "openclaw sessions list")
    return find_session_model(sessions_payload, session_key)


def get_model_selection(session_key: str) -> ModelSelection:
    return ModelSelection(
        default_model=_get_default_model(),
        session_model=_get_session_model(session_key),
    )
