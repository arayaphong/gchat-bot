from __future__ import annotations

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

    def test_requests_use_openclaw(self) -> None:
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

    def test_provider_settings_use_openclaw_defaults(self) -> None:
        with patch(
            "helpers.providers.router.generate_session_key",
            return_value="agent:main:gchat:decade",
        ):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.openclaw_agent, "main")
        self.assertEqual(settings.openclaw_session_key, "agent:main:gchat:decade")
        self.assertEqual(settings.openclaw_base_url, "http://127.0.0.1:18789/v1")
        self.assertEqual(settings.openclaw_model, "openclaw/default")

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

    def test_model_shaped_text_still_uses_openclaw(self) -> None:
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
