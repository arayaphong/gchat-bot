from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers.providers import ProviderSettings
from helpers.session_keys import (
    SESSION_AGENT,
    generate_session_key,
)
from helpers.session_manager import SessionManager


def provider_settings(session_key: str) -> ProviderSettings:
    return ProviderSettings(
        openclaw_agent=SESSION_AGENT,
        openclaw_session_key=session_key,
        openclaw_base_url="http://127.0.0.1:18789/v1",
        openclaw_model="openclaw/default",
    )


class SessionKeyTests(unittest.TestCase):
    def test_generator_uses_the_only_supported_format(self) -> None:
        fake_uuid = SimpleNamespace(hex="abcdef1234567890")
        with patch("helpers.session_keys.uuid.uuid4", return_value=fake_uuid):
            session_key = generate_session_key()

        self.assertEqual(session_key, "agent:main:gchat:abcdef")

    def test_provider_settings_no_longer_read_agent_or_session_key_from_env(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "OPENCLAW_AGENT": "other",
                    "OPENCLAW_SESSION_KEY": "agent:main:gchat:jinx",
                },
                clear=True,
            ),
            patch(
                "helpers.providers.router.generate_session_key",
                return_value="agent:main:gchat:123abc",
            ),
        ):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.openclaw_agent, "main")
        self.assertEqual(settings.openclaw_session_key, "agent:main:gchat:123abc")


class SessionManagerTests(unittest.TestCase):
    def test_nonempty_persisted_key_wins_without_format_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "session_key"
            key_file.write_text("persisted-value", encoding="utf-8")

            manager = SessionManager(
                key_file, provider_settings("agent:main:gchat:123abc")
            )

            self.assertEqual(manager.settings.openclaw_session_key, "persisted-value")
            self.assertEqual(key_file.read_text(encoding="utf-8"), "persisted-value")

    def test_missing_or_empty_file_is_initialized(self) -> None:
        for existing_value in (None, ""):
            with (
                self.subTest(existing_value=existing_value),
                tempfile.TemporaryDirectory() as directory,
            ):
                key_file = Path(directory) / "session_key"
                if existing_value is not None:
                    key_file.write_text(existing_value, encoding="utf-8")

                manager = SessionManager(
                    key_file, provider_settings("agent:main:gchat:123abc")
                )

                self.assertEqual(
                    manager.settings.openclaw_session_key,
                    "agent:main:gchat:123abc",
                )
                self.assertEqual(
                    key_file.read_text(encoding="utf-8"),
                    "agent:main:gchat:123abc",
                )

    def test_rotate_uses_the_central_generator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "session_key"
            manager = SessionManager(
                key_file, provider_settings("agent:main:gchat:123abc")
            )

            with patch(
                "helpers.session_manager.generate_session_key",
                return_value="agent:main:gchat:fedcba",
            ):
                settings = manager.rotate()

            self.assertEqual(settings.openclaw_session_key, "agent:main:gchat:fedcba")
            self.assertEqual(
                key_file.read_text(encoding="utf-8"), "agent:main:gchat:fedcba"
            )


if __name__ == "__main__":
    unittest.main()
