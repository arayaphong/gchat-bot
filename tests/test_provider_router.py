from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from helpers.providers.router import ProviderSettings, ask_provider


class ProviderRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:c0ffee",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )

    def test_openclaw_is_the_default_provider(self) -> None:
        with patch(
            "helpers.providers.router.ask_openclaw_direct",
            return_value={"text": "reply"},
        ) as openclaw:
            result = ask_provider("hello", "Alice", [], self.settings)

        self.assertEqual(result, ("reply", "openclaw"))
        openclaw.assert_called_once_with(
            "hello",
            "Alice",
            [],
            self.settings.openclaw_agent,
            self.settings.openclaw_session_key,
            self.settings.openclaw_base_url,
            self.settings.openclaw_model,
            None,
        )

    def test_provider_defaults_to_openclaw_from_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.provider, "openclaw")

    def test_provider_is_normalized_from_environment(self) -> None:
        with patch.dict(os.environ, {"GCHAT_PROVIDER": " OPENCLAW "}, clear=True):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.provider, "openclaw")

    def test_retired_or_unknown_provider_is_rejected(self) -> None:
        for provider in ("kimiclaw", "unknown"):
            with (
                self.subTest(provider=provider),
                self.assertRaisesRegex(ValueError, "not supported"),
            ):
                ProviderSettings(
                    openclaw_agent="main",
                    openclaw_session_key="agent:main:gchat:c0ffee",
                    openclaw_base_url="http://127.0.0.1:18789/v1",
                    openclaw_model="openclaw/default",
                    provider=provider,
                )

    def test_quoted_message_and_inbound_files_are_forwarded(self) -> None:
        files = [{"path": "/tmp/photo.png", "mimeType": "image/png"}]
        quoted = {"sender": "Bob", "text": "previous message"}

        with patch(
            "helpers.providers.router.ask_openclaw_direct",
            return_value={"text": "reply"},
        ) as openclaw:
            result = ask_provider("inspect", "Alice", files, self.settings, quoted)

        self.assertEqual(result, ("reply", "openclaw"))
        openclaw.assert_called_once_with(
            "inspect",
            "Alice",
            files,
            self.settings.openclaw_agent,
            self.settings.openclaw_session_key,
            self.settings.openclaw_base_url,
            self.settings.openclaw_model,
            quoted,
        )

    def test_model_shaped_text_does_not_override_the_selected_provider(self) -> None:
        with patch(
            "helpers.providers.router.ask_openclaw_direct",
            return_value={"text": "reply"},
        ) as openclaw:
            result = ask_provider(
                "/model minimax/MiniMax-M3",
                "Alice",
                [],
                self.settings,
            )

        self.assertEqual(result, ("reply", "openclaw"))
        openclaw.assert_called_once_with(
            "/model minimax/MiniMax-M3",
            "Alice",
            [],
            self.settings.openclaw_agent,
            self.settings.openclaw_session_key,
            self.settings.openclaw_base_url,
            self.settings.openclaw_model,
            None,
        )


if __name__ == "__main__":
    unittest.main()
