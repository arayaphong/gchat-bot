from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from helpers.providers.router import ProviderSettings, ask_provider


class ProviderRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:current",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )

    def test_kimiclaw_is_default_provider_for_every_model(self) -> None:
        with patch(
            "helpers.providers.router.ask_kimiclaw",
            return_value={"text": "reply", "files": []},
        ) as kimiclaw:
            result = ask_provider("hello", "Alice", [], self.settings)

        self.assertEqual(result, ("reply", "kimiclaw", []))
        kimiclaw.assert_called_once_with(
            "hello", "Alice", [], self.settings.openclaw_session_key, None
        )

    def test_provider_defaults_to_kimiclaw_from_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.provider, "kimiclaw")

    def test_provider_is_normalized_from_environment(self) -> None:
        with patch.dict(os.environ, {"GCHAT_PROVIDER": " OPENCLAW "}, clear=True):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.provider, "openclaw")

    def test_openclaw_can_be_selected_explicitly(self) -> None:
        settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:current",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
            provider="openclaw",
        )
        openclaw_result = {"text": "HTTP reply", "files": []}

        with (
            patch("helpers.providers.router.ask_kimiclaw") as kimiclaw,
            patch(
                "helpers.providers.router.ask_openclaw_direct",
                return_value=openclaw_result,
            ) as openclaw,
        ):
            result = ask_provider("hello", "Alice", [], settings)

        self.assertEqual(result, ("HTTP reply", "openclaw", []))
        kimiclaw.assert_not_called()
        openclaw.assert_called_once_with(
            "hello",
            "Alice",
            [],
            settings.openclaw_agent,
            settings.openclaw_session_key,
            settings.openclaw_base_url,
            settings.openclaw_model,
            None,
        )

    def test_invalid_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not supported"):
            ProviderSettings(
                openclaw_agent="main",
                openclaw_session_key="agent:main:gchat:current",
                openclaw_base_url="http://127.0.0.1:18789/v1",
                openclaw_model="openclaw/default",
                provider="unknown",
            )

    def test_quoted_message_and_files_are_forwarded(self) -> None:
        files = [{"path": "/tmp/photo.png", "mimeType": "image/png"}]
        quoted = {"sender": "Bob", "text": "previous message"}

        with patch(
            "helpers.providers.router.ask_kimiclaw",
            return_value={"text": "reply", "files": [{"path": "/tmp/out.txt"}]},
        ) as kimiclaw:
            result = ask_provider("inspect", "Alice", files, self.settings, quoted)

        self.assertEqual(
            result,
            ("reply", "kimiclaw", [{"path": "/tmp/out.txt"}]),
        )
        kimiclaw.assert_called_once_with(
            "inspect",
            "Alice",
            files,
            self.settings.openclaw_session_key,
            quoted,
        )

    def test_model_command_uses_kimiclaw_command_path(self) -> None:
        settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:current",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
            provider="openclaw",
        )
        with patch(
            "helpers.providers.router.ask_kimiclaw",
            return_value={"text": "updated", "files": []},
        ) as kimiclaw:
            result = ask_provider(
                "/model minimax/MiniMax-M3",
                "Alice",
                [],
                settings,
            )

        self.assertEqual(result, ("updated", "kimiclaw", []))
        kimiclaw.assert_called_once_with(
            "/model minimax/MiniMax-M3",
            "Alice",
            [],
            settings.openclaw_session_key,
            None,
        )


if __name__ == "__main__":
    unittest.main()
