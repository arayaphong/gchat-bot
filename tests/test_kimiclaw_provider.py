from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from helpers.providers.kimiclaw_gateway import (
    GatewayResult,
    KimiclawGatewayError,
)
from helpers.providers.kimiclaw_provider import (
    _parse_gateway_result,
    ask_kimiclaw,
)


class KimiclawProviderTests(unittest.TestCase):
    def test_python_gateway_receives_prompt_and_exact_session_key(self) -> None:
        with (
            patch("helpers.providers.kimiclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.kimiclaw_provider.run_gateway_request",
                return_value=GatewayResult(text="สวัสดี", run_id="run-1"),
            ) as run,
        ):
            result = ask_kimiclaw(
                "hello",
                "Alice",
                [],
                "agent:main:gchat:abc123",
            )

        self.assertEqual(result, {"text": "สวัสดี", "files": []})
        prompt, session_key = run.call_args.args
        self.assertIn("Alice: hello", prompt)
        self.assertEqual(session_key, "agent:main:gchat:abc123")
        self.assertEqual(run.call_args.kwargs, {"mode": "agent"})

    def test_model_command_uses_command_pipeline_without_user_prefix(self) -> None:
        with (
            patch("helpers.providers.kimiclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.kimiclaw_provider.run_gateway_request",
                return_value=GatewayResult(text="updated", run_id=None),
            ) as run,
        ):
            ask_kimiclaw(
                "/model moonshot/kimi-k2.6",
                "Alice",
                [],
                "agent:main:gchat:abc123",
            )

        self.assertEqual(run.call_args.args[0], "/model moonshot/kimi-k2.6")
        self.assertEqual(run.call_args.kwargs, {"mode": "command"})

    def test_file_tags_are_returned_through_the_existing_contract(self) -> None:
        result = _parse_gateway_result(
            GatewayResult(
                text="เรียบร้อย [[ATTACH:/tmp/report.txt]]",
                run_id="run-1",
            )
        )

        self.assertEqual(result["text"], "เรียบร้อย")
        self.assertEqual(result["files"][0]["filePath"], "/tmp/report.txt")

    def test_failed_agent_run_is_aborted_and_reported(self) -> None:
        aborted = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        gateway_error = KimiclawGatewayError(
            "timeout",
            "KIMICLAW_TIMEOUT",
            "no final response",
            "run-1",
            should_abort=True,
        )

        with (
            patch("helpers.providers.kimiclaw_provider.append_jsonl"),
            patch(
                "helpers.providers.kimiclaw_provider.abort_session",
                return_value=aborted,
            ) as abort,
            patch(
                "helpers.providers.kimiclaw_provider.run_gateway_request",
                side_effect=gateway_error,
            ),
            self.assertRaisesRegex(RuntimeError, "KIMICLAW_TIMEOUT"),
        ):
            ask_kimiclaw("hello", "Alice", [], "agent:main:gchat:abc123")

        abort.assert_called_once_with("agent:main:gchat:abc123")

    def test_connect_failure_does_not_abort_session(self) -> None:
        gateway_error = KimiclawGatewayError(
            "connect",
            "UNAUTHORIZED",
            "bad token",
            should_abort=False,
        )

        with (
            patch("helpers.providers.kimiclaw_provider.append_jsonl"),
            patch("helpers.providers.kimiclaw_provider.abort_session") as abort,
            patch(
                "helpers.providers.kimiclaw_provider.run_gateway_request",
                side_effect=gateway_error,
            ),
            self.assertRaisesRegex(RuntimeError, "UNAUTHORIZED"),
        ):
            ask_kimiclaw("hello", "Alice", [], "agent:main:gchat:abc123")

        abort.assert_not_called()


if __name__ == "__main__":
    unittest.main()
