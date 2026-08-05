from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers.providers import ProviderSettings
from helpers.providers.openclaw_cli import create_session
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

    def test_create_session_cli_sets_the_initial_model(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        with patch(
            "helpers.providers.openclaw_cli._run",
            return_value=completed,
        ) as run:
            result = create_session(
                "agent:main:gchat:decade",
                "main",
                "minimax/MiniMax-M3",
            )

        self.assertIs(result, completed)
        run.assert_called_once_with(
            [
                "gateway",
                "call",
                "sessions.create",
                "--json",
                "--params",
                json.dumps(
                    {
                        "key": "agent:main:gchat:decade",
                        "agentId": "main",
                        "model": "minimax/MiniMax-M3",
                    }
                ),
            ]
        )


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

    def test_rotate_with_model_creates_before_persisting_the_new_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "session_key"
            old_key = "agent:main:gchat:123abc"
            new_key = "agent:main:gchat:decade"
            manager = SessionManager(key_file, provider_settings(old_key))

            def create_remote_session(
                session_key: str,
                agent: str,
                model: str,
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(manager.settings.openclaw_session_key, old_key)
                self.assertEqual(key_file.read_text(encoding="utf-8"), old_key)
                self.assertEqual(
                    (session_key, agent, model),
                    (new_key, "main", "minimax/MiniMax-M3"),
                )
                return subprocess.CompletedProcess(
                    [], 0, stdout='{"key":"agent:main:gchat:decade"}', stderr=""
                )

            with (
                patch(
                    "helpers.session_manager.generate_session_key",
                    return_value=new_key,
                ),
                patch(
                    "helpers.session_manager.create_session_cli",
                    side_effect=create_remote_session,
                ) as create_remote,
            ):
                settings = manager.rotate_with_model(" minimax/MiniMax-M3 ")

            create_remote.assert_called_once_with(
                new_key,
                "main",
                "minimax/MiniMax-M3",
            )
            self.assertEqual(settings.openclaw_session_key, new_key)
            self.assertEqual(manager.settings.openclaw_session_key, new_key)
            self.assertEqual(key_file.read_text(encoding="utf-8"), new_key)

    def test_rotate_with_model_failure_preserves_the_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "session_key"
            old_key = "agent:main:gchat:123abc"
            manager = SessionManager(key_file, provider_settings(old_key))
            failed = subprocess.CompletedProcess(
                [], 2, stdout="", stderr="unknown method: sessions.create"
            )

            with (
                patch(
                    "helpers.session_manager.generate_session_key",
                    return_value="agent:main:gchat:decade",
                ),
                patch(
                    "helpers.session_manager.create_session_cli",
                    return_value=failed,
                ),
                self.assertRaisesRegex(RuntimeError, "unknown method"),
            ):
                manager.rotate_with_model("minimax/MiniMax-M3")

            self.assertEqual(manager.settings.openclaw_session_key, old_key)
            self.assertEqual(key_file.read_text(encoding="utf-8"), old_key)

    def test_rotate_with_model_rejects_invalid_success_payloads(self) -> None:
        invalid_outputs = (
            "",
            "not json",
            "[]",
            '{"ok":false,"key":"agent:main:gchat:decade"}',
            '{"ok":true,"key":"agent:main:gchat:different"}',
        )

        for stdout in invalid_outputs:
            with (
                self.subTest(stdout=stdout),
                tempfile.TemporaryDirectory() as directory,
            ):
                key_file = Path(directory) / "session_key"
                old_key = "agent:main:gchat:123abc"
                manager = SessionManager(key_file, provider_settings(old_key))
                result = subprocess.CompletedProcess(
                    [], 0, stdout=stdout, stderr=""
                )

                with (
                    patch(
                        "helpers.session_manager.generate_session_key",
                        return_value="agent:main:gchat:decade",
                    ),
                    patch(
                        "helpers.session_manager.create_session_cli",
                        return_value=result,
                    ),
                    self.assertRaises((RuntimeError, TypeError)),
                ):
                    manager.rotate_with_model("minimax/MiniMax-M3")

                self.assertEqual(manager.settings.openclaw_session_key, old_key)
                self.assertEqual(key_file.read_text(encoding="utf-8"), old_key)

    def test_rotate_with_model_write_failure_keeps_in_memory_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "session_key"
            old_key = "agent:main:gchat:123abc"
            manager = SessionManager(key_file, provider_settings(old_key))
            created = subprocess.CompletedProcess(
                [],
                0,
                stdout='{"ok":true,"key":"agent:main:gchat:decade"}',
                stderr="",
            )

            with (
                patch(
                    "helpers.session_manager.generate_session_key",
                    return_value="agent:main:gchat:decade",
                ),
                patch(
                    "helpers.session_manager.create_session_cli",
                    return_value=created,
                ),
                patch.object(
                    manager,
                    "_write_key_file",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                manager.rotate_with_model("minimax/MiniMax-M3")

            self.assertEqual(manager.settings.openclaw_session_key, old_key)
            self.assertEqual(key_file.read_text(encoding="utf-8"), old_key)


if __name__ == "__main__":
    unittest.main()
