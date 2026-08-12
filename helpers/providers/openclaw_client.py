from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from helpers.providers.model_selection import (
    ModelSelection,
    find_session_model,
    load_cli_json,
)
from helpers.providers.openclaw_cli import abort_session as abort_session_cli
from helpers.providers.openclaw_cli import create_session as create_session_cli
from helpers.providers.openclaw_cli import get_default_model as get_default_model_cli
from helpers.providers.openclaw_cli import list_models as list_models_cli
from helpers.providers.openclaw_cli import list_sessions as list_sessions_cli
from helpers.providers.openclaw_cli import reset_session as reset_session_cli
from helpers.providers.openclaw_logcheck import check_run_errors
from helpers.providers.openclaw_provider import ask_openclaw_direct
from helpers.thread_uploads import thread_upload_directory

LOCAL_FILE_ACCESS_ENV = "OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS"
LOCAL_FILE_ACCESS_POLICIES = frozenset({"auto", "allow", "deny"})


@dataclass(frozen=True)
class SendTurnResult:
    text: str
    run_id: str


@dataclass(frozen=True)
class AbortResult:
    ok: bool
    reason: str = ""


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


def _returned_session_keys(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    keys = [
        candidate
        for field in ("key", "sessionKey")
        if isinstance((candidate := value.get(field)), str)
    ]
    nested = [
        nested_value
        for field in ("payload", "result", "session", "entry")
        if isinstance((nested_value := value.get(field)), dict)
    ]
    return [
        *keys,
        *[
            key
            for nested_value in nested
            for key in _returned_session_keys(nested_value)
        ],
    ]


class OpenClawClient:
    """Facade for all OpenClaw HTTP and CLI operations used by the bot."""

    def __init__(
        self,
        *,
        agent: str,
        base_url: str,
        model: str,
        outbound_upload_root: Path | None = None,
    ) -> None:
        self._agent = agent
        self._base_url = base_url
        self._model = model
        self._outbound_upload_root = (
            outbound_upload_root.expanduser().resolve(strict=False)
            if outbound_upload_root is not None
            else None
        )

    def send_turn(
        self,
        text: str,
        user: str,
        files_with_meta: list[dict[str, Any]],
        session_key: str,
        quoted_message: dict[str, str] | None = None,
    ) -> SendTurnResult:
        try:
            sent_at = datetime.now(timezone.utc).astimezone()
            arguments = (
                text,
                user,
                files_with_meta,
                session_key,
                self._base_url,
                self._model,
                quoted_message,
            )
            if self._outbound_upload_root is None:
                result = ask_openclaw_direct(*arguments)
            else:
                result = ask_openclaw_direct(
                    *arguments,
                    outbound_upload_directory=thread_upload_directory(
                        self._outbound_upload_root,
                        session_key,
                    ),
                )
            print("🔀 [provider] provider=openclaw")
            run_id = result.get("run_id", "")
            for line in check_run_errors(run_id, sent_at):
                print(f"⚠️ [logcheck] run={run_id}: {line}")
            return SendTurnResult(
                text=result.get("text", ""),
                run_id=run_id,
            )
        except Exception as error:
            reason = _extract_error_reason(error)
            raise RuntimeError(f"เกิดข้อผิดพลาด: {reason}") from error

    def create_session(self, session_key: str, model: str) -> str:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")

        selected_model = model.strip()
        result = create_session_cli(session_key, self._agent, selected_model)
        print(
            f"🆕 [model-session] returncode={result.returncode} "
            f"stdout={(result.stdout or '').strip()[:500]!r} "
            f"stderr={(result.stderr or '').strip()[:500]!r}"
        )
        if result.returncode != 0:
            detail = " ".join((result.stderr or result.stdout or "").split())[:500]
            reason = f"openclaw sessions.create คืนค่ารหัส {result.returncode}"
            if detail:
                reason = f"{reason}: {detail}"
            raise RuntimeError(reason)

        raw_output = (result.stdout or "").lstrip("\ufeff").strip()
        try:
            payload = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError(
                "openclaw sessions.create ส่ง JSON กลับมาไม่ถูกต้อง"
            ) from error
        if not isinstance(payload, dict):
            raise TypeError("รูปแบบผลลัพธ์จาก openclaw sessions.create ไม่ถูกต้อง")
        if payload.get("ok") is False:
            raise RuntimeError("openclaw sessions.create รายงานว่าสร้าง session ไม่สำเร็จ")
        if session_key not in _returned_session_keys(payload):
            raise RuntimeError("openclaw sessions.create ส่ง session key กลับมาไม่ตรงกัน")
        return session_key

    def reset_session(self, session_key: str) -> str:
        """Start fresh history while keeping the exact deterministic key."""

        result = reset_session_cli(session_key)
        print(
            f"🔄 [reset-session] returncode={result.returncode} "
            f"stdout={(result.stdout or '').strip()[:500]!r} "
            f"stderr={(result.stderr or '').strip()[:500]!r}"
        )
        if result.returncode != 0:
            detail = " ".join((result.stderr or result.stdout or "").split())[:500]
            reason = f"openclaw sessions.reset คืนค่ารหัส {result.returncode}"
            if detail:
                reason = f"{reason}: {detail}"
            raise RuntimeError(reason)

        raw_output = (result.stdout or "").lstrip("\ufeff").strip()
        try:
            payload = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError(
                "openclaw sessions.reset ส่ง JSON กลับมาไม่ถูกต้อง"
            ) from error
        if not isinstance(payload, dict):
            raise TypeError("รูปแบบผลลัพธ์จาก openclaw sessions.reset ไม่ถูกต้อง")
        if payload.get("ok") is False:
            raise RuntimeError("openclaw sessions.reset รายงานว่า reset session ไม่สำเร็จ")
        if session_key not in _returned_session_keys(payload):
            raise RuntimeError("openclaw sessions.reset ส่ง session key กลับมาไม่ตรงกัน")
        return session_key

    def abort_session(self, session_key: str) -> AbortResult:
        result = abort_session_cli(session_key)
        print(
            f"🛑 [abort] returncode={result.returncode} "
            f"stdout={(result.stdout or '').strip()[:500]!r} "
            f"stderr={(result.stderr or '').strip()[:500]!r}"
        )
        if result.returncode == 0:
            return AbortResult(ok=True)
        reason = (result.stderr or result.stdout or "").strip()[:500]
        return AbortResult(ok=False, reason=reason)

    def list_models(self) -> list[Any]:
        models_payload = load_cli_json(list_models_cli(), "openclaw models list")
        if not isinstance(models_payload, dict) or not isinstance(
            models_payload.get("models"), list
        ):
            raise TypeError("รูปแบบข้อมูลจาก openclaw ไม่ถูกต้อง")
        return models_payload["models"]

    def get_default_model(self) -> str:
        default_model = load_cli_json(
            get_default_model_cli(),
            "openclaw config get agents.defaults.model.primary",
        )
        if not isinstance(default_model, str) or not default_model.strip():
            raise TypeError("รูปแบบ default model จาก openclaw ไม่ถูกต้อง")
        return default_model.strip()

    def get_session_model(self, session_key: str) -> str | None:
        sessions_payload = self._load_sessions_payload()
        return find_session_model(sessions_payload, session_key)

    @staticmethod
    def _load_sessions_payload() -> dict[str, Any]:
        sessions_payload = load_cli_json(
            list_sessions_cli(),
            "openclaw sessions list",
        )
        if not isinstance(sessions_payload, dict) or not isinstance(
            sessions_payload.get("sessions"), list
        ):
            raise TypeError("รูปแบบข้อมูล sessions จาก openclaw ไม่ถูกต้อง")
        return sessions_payload

    def has_session(self, session_key: str) -> bool:
        sessions = self._load_sessions_payload()["sessions"]
        matches = [
            candidate
            for candidate in sessions
            if isinstance(candidate, dict) and candidate.get("key") == session_key
        ]
        if len(matches) > 1:
            raise TypeError("openclaw ส่ง session key ซ้ำกัน")
        return bool(matches)

    def get_model_selection(self, session_key: str) -> ModelSelection:
        return ModelSelection(
            default_model=self.get_default_model(),
            session_model=self.get_session_model(session_key),
        )

    def has_local_file_access(self) -> bool:
        policy = os.environ.get(LOCAL_FILE_ACCESS_ENV, "auto").strip().lower()
        if policy not in LOCAL_FILE_ACCESS_POLICIES:
            expected = ", ".join(sorted(LOCAL_FILE_ACCESS_POLICIES))
            raise ValueError(f"{LOCAL_FILE_ACCESS_ENV} must be one of: {expected}")
        if policy == "allow":
            return True
        if policy == "deny":
            return False

        try:
            host = (urlsplit(self._base_url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
