from __future__ import annotations

import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from helpers.providers.openclaw_client import OpenClawClient
from helpers.providers.provider_settings import ProviderSettings
from helpers.session_keys import generate_session_key


class SessionManager:
    def __init__(
        self,
        session_key_file: Path,
        initial_settings: ProviderSettings,
        *,
        openclaw_client: OpenClawClient,
    ) -> None:
        self._session_key_file = session_key_file
        self._openclaw_client = openclaw_client
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
            self._openclaw_client.create_session(new_key, selected_model)

            self._write_key_file(new_key)
            self._settings = replace(
                self._settings,
                openclaw_session_key=new_key,
            )
            return self._settings

    def abort_current(self, space: str, thread: str) -> tuple[bool, str]:
        session_key = self._settings.openclaw_session_key
        try:
            result = self._openclaw_client.abort_session(session_key)
            if result.ok:
                return True, ""
            return False, result.reason
        except Exception as e:  # noqa: BLE001
            print(f"❌ [error] abort failed: {e} (space={space}, thread={thread})")
            return False, str(e)
