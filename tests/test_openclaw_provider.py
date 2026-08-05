from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.providers.openclaw_provider import (
    _load_gateway_token,
    ask_openclaw_direct,
)


class OpenClawProviderTests(unittest.TestCase):
    def test_gateway_token_prefers_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENCLAW_GATEWAY_TOKEN": " environment-token ",
                "OPENCLAW_CONFIG_FILE": "/does/not/exist",
            },
            clear=True,
        ):
            self.assertEqual(_load_gateway_token(), "environment-token")

    def test_gateway_token_falls_back_to_configured_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "openclaw.json"
            config_file.write_text(
                json.dumps({"gateway": {"auth": {"token": "config-token"}}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"OPENCLAW_CONFIG_FILE": str(config_file)},
                clear=True,
            ):
                self.assertEqual(_load_gateway_token(), "config-token")

    def test_request_routes_to_the_explicit_agent_and_session(self) -> None:
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = '{"choices":[{"message":{"content":"reply"}}]}'
        response.json.return_value = {"choices": [{"message": {"content": "reply"}}]}

        with (
            patch(
                "helpers.providers.openclaw_provider._load_gateway_token",
                return_value="gateway-token",
            ),
            patch("helpers.providers.openclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.openclaw_provider.requests.post",
                return_value=response,
            ) as post,
        ):
            result = ask_openclaw_direct(
                "hello",
                "Alice",
                [],
                "main",
                "agent:main:gchat:c0ffee",
                "http://127.0.0.1:18789/v1",
                "openclaw/default",
            )

        self.assertEqual(result, {"text": ""})
        request = post.call_args
        self.assertEqual(request.args[0], "http://127.0.0.1:18789/v1/chat/completions")
        self.assertEqual(request.kwargs["json"]["model"], "openclaw/default")
        self.assertEqual(
            request.kwargs["headers"]["x-openclaw-session-key"],
            "agent:main:gchat:c0ffee",
        )
        self.assertEqual(request.kwargs["headers"]["x-openclaw-agent-id"], "main")
        self.assertNotIn("x-openclaw-model", request.kwargs["headers"])

    def test_successful_non_json_response_body_is_ignored(self) -> None:
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.text = "accepted"
        response.json.side_effect = ValueError("not json")

        with (
            patch(
                "helpers.providers.openclaw_provider._load_gateway_token",
                return_value="gateway-token",
            ),
            patch("helpers.providers.openclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.openclaw_provider.requests.post",
                return_value=response,
            ),
        ):
            result = ask_openclaw_direct(
                "hello",
                "Alice",
                [],
                "main",
                "agent:main:gchat:c0ffee",
                "http://127.0.0.1:18789/v1",
                "openclaw/default",
            )

        self.assertEqual(result, {"text": ""})


if __name__ == "__main__":
    unittest.main()
