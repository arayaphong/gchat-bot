from __future__ import annotations

import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from helpers.providers import ProviderSettings
from helpers.providers.openclaw_cli import abort_session as abort_session_cli


class SessionManager:
    def __init__(self, session_key_file: Path, initial_settings: ProviderSettings) -> None:
        self._session_key_file = session_key_file
        self._lock = threading.Lock()
        saved_key = self._read_key_file()
        self._settings = (
            replace(initial_settings, openclaw_session_key=saved_key)
            if saved_key
            else initial_settings
        )

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

    @staticmethod
    def _generate_key(agent: str) -> str:
        short_uuid = uuid.uuid4().hex[:12]
        return f"agent:{agent}:gchat:{short_uuid}"

    def rotate(self) -> ProviderSettings:
        new_key = self._generate_key(self._settings.openclaw_agent)
        with self._lock:
            self._write_key_file(new_key)
            self._settings = replace(self._settings, openclaw_session_key=new_key)
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
