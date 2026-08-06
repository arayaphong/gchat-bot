from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


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

    session = next(
        (
            candidate
            for candidate in payload["sessions"]
            if isinstance(candidate, dict) and candidate.get("key") == session_key
        ),
        None,
    )
    if session is None:
        return None

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
