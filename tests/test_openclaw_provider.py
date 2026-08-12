from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers.providers.openclaw_provider import (
    NO_ASSISTANT_TEXT_INFO,
    _load_gateway_token,
    ask_openclaw_direct,
    build_openclaw_prompt,
    parse_openclaw_response,
)
from helpers.providers.openclaw_ws import OpenclawDispatchError


class OpenClawProviderTests(unittest.TestCase):
    def test_prompt_announces_the_exact_thread_upload_directory(self) -> None:
        upload_directory = Path(
            "/home/arme/.openclaw/workspace/uploads/thread-0123456789abcdef"
        )
        prompt = build_openclaw_prompt(
            "make an image",
            "Alice",
            [],
            outbound_upload_directory=upload_directory,
        )

        self.assertIn("[THREAD_UPLOAD_DIRECTORY]", prompt)
        self.assertIn(str(upload_directory), prompt)
        self.assertIn("[/THREAD_UPLOAD_DIRECTORY]", prompt)
        self.assertIn("Do not write files directly", prompt)

    def test_slash_command_is_still_forwarded_without_prompt_decoration(self) -> None:
        prompt = build_openclaw_prompt(
            "/help",
            "Alice",
            [],
            outbound_upload_directory=Path("/tmp/thread-output"),
        )

        self.assertEqual(prompt, "/help")

    def test_empty_response_text_becomes_information(self) -> None:
        result = parse_openclaw_response({"choices": [{"message": {"content": ""}}]})

        self.assertEqual(result, {"text": NO_ASSISTANT_TEXT_INFO})

    def test_tool_call_is_not_interpreted(self) -> None:
        result = parse_openclaw_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "send_file",
                                        "arguments": '{"filePath": "/tmp/report.txt"}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        )

        self.assertEqual(result, {"text": "done"})

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

    def test_dispatch_routes_to_the_configured_session_over_ws(self) -> None:
        with (
            patch(
                "helpers.providers.openclaw_provider._load_gateway_token",
                return_value="gateway-token",
            ),
            patch("helpers.providers.openclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.openclaw_provider.dispatch_agent_run",
                return_value="run-123",
            ) as dispatch,
        ):
            result = ask_openclaw_direct(
                "hello",
                "Alice",
                [],
                "agent:main:gchat:c0ffee",
                "http://127.0.0.1:18789/v1",
                "openclaw/default",
            )

        self.assertEqual(result, {"text": "", "run_id": "run-123"})
        dispatch.assert_called_once_with(
            base_url="http://127.0.0.1:18789/v1",
            token="gateway-token",
            session_key="agent:main:gchat:c0ffee",
            channel="googlechat",
            message="Alice: hello",
        )

    def test_dispatch_failure_is_wrapped_in_runtime_error(self) -> None:
        with (
            patch(
                "helpers.providers.openclaw_provider._load_gateway_token",
                return_value="gateway-token",
            ),
            patch("helpers.providers.openclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.openclaw_provider.dispatch_agent_run",
                side_effect=OpenclawDispatchError("connect rejected: AUTH"),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            ask_openclaw_direct(
                "hello",
                "Alice",
                [],
                "agent:main:gchat:c0ffee",
                "http://127.0.0.1:18789/v1",
                "openclaw/default",
            )

        self.assertIn("connect rejected: AUTH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
