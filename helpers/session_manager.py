from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from helpers.providers import ProviderSettings
from helpers.providers.openclaw_cli import abort_session as abort_session_cli
from helpers.providers.openclaw_cli import create_session as create_session_cli
from helpers.session_keys import generate_session_key


class SessionManager:
    def __init__(self, session_key_file: Path, initial_settings: ProviderSettings) -> None:
        self._session_key_file = session_key_file
        self._lock = threading.Lock()
        saved_key = self._read_key_file()
        if saved_key:
            self._settings = replace(initial_settings, openclaw_session_key=saved_key)
        else:
            self._write_key_file(initial_settings.openclaw_session_key)
            self._settings = initial_settings

    @property
    def settings(self) -> ProviderSettings:
        return self._settings

    def _read_key_file(self) -> str:
        if not self._session_key_file.exists():
            return ""
        return self._session_key_file.read_text(encoding="utf-8").strip()

    def _write_key_file(self, session_key: str) -> None:
        tmp = self._session_key_file.with_name(
            f".{self._session_key_file.name}.{uuid.uuid4().hex}.tmp"
        )
        tmp.write_text(session_key, encoding="utf-8")
        os.replace(tmp, self._session_key_file)

    def rotate(self) -> ProviderSettings:
        new_key = generate_session_key()
        with self._lock:
            self._write_key_file(new_key)
            self._settings = replace(self._settings, openclaw_session_key=new_key)
        return self._settings

    def rotate_with_model(self, model: str) -> ProviderSettings:
        """Create a modeled gateway session, then make its key current."""
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")

        selected_model = model.strip()
        with self._lock:
            new_key = generate_session_key()
            result = create_session_cli(
                new_key,
                self._settings.openclaw_agent,
                selected_model,
            )
            print(
                f"🆕 [model-session] returncode={result.returncode} "
                f"stdout={result.stdout.strip()[:500]!r} "
                f"stderr={result.stderr.strip()[:500]!r}"
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

            def returned_session_keys(value: object) -> list[str]:
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
                        for key in returned_session_keys(nested_value)
                    ],
                ]

            if new_key not in returned_session_keys(payload):
                raise RuntimeError(
                    "openclaw sessions.create ส่ง session key กลับมาไม่ตรงกัน"
                )

            self._write_key_file(new_key)
            self._settings = replace(
                self._settings,
                openclaw_session_key=new_key,
            )
            return self._settings

    def abort_current(self, space: str, thread: str) -> tuple[bool, str]:
        session_key = self._settings.openclaw_session_key
        try:
            result = abort_session_cli(session_key)
            print(
                f"🛑 [abort] returncode={result.returncode} "
                f"stdout={result.stdout.strip()[:500]!r} "
                f"stderr={result.stderr.strip()[:500]!r} "
                f"(space={space}, thread={thread})"
            )
            if result.returncode == 0:
                return True, ""
            return False, (result.stderr or result.stdout or "").strip()[:500]
        except Exception as e:  # noqa: BLE001
            print(f"❌ [error] abort failed: {e} (space={space}, thread={thread})")
            return False, str(e)
