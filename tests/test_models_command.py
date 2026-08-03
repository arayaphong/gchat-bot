from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import format_models_summary
from helpers.providers.openclaw_cli import list_models


class ModelsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = Mock()
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=SimpleNamespace(),
            attachment_service=SimpleNamespace(),
        )

    def test_cli_uses_exact_models_list_json_arguments(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        with patch(
            "helpers.providers.openclaw_cli._run", return_value=completed
        ) as run:
            self.assertIs(list_models(), completed)

        run.assert_called_once_with(["models", "list", "--json"])

    def test_models_is_registered_as_a_bypass_command(self) -> None:
        handler = self.orchestrator._bypass_commands["/models"]

        self.assertIs(handler.__self__, self.orchestrator)
        self.assertIs(handler.__func__, self.orchestrator._handle_models.__func__)

    def test_success_sends_every_model_as_the_administrator(self) -> None:
        models = [
            {
                "key": "minimax/MiniMax-M3",
                "name": "MiniMax-M3",
                "input": "text+image",
                "contextWindow": 512000,
                "local": False,
                "available": True,
                "tags": ["default", "configured"],
                "missing": False,
            },
            {
                "key": "moonshot/kimi-k2.5",
                "name": "kimi-k2.5",
                "input": "text",
                "contextWindow": 200000,
                "local": False,
                "available": False,
                "tags": [],
                "missing": False,
            },
        ]
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"count": len(models), "models": models}),
            stderr="",
        )

        with patch("helpers.message_orchestrator.list_models_cli", return_value=result):
            self.orchestrator._handle_models("spaces/one", "spaces/one/threads/two")

        self.gateway.send_followup.assert_called_once()
        space, thread, summary, provider = self.gateway.send_followup.call_args.args
        self.assertEqual(space, "spaces/one")
        self.assertEqual(thread, "spaces/one/threads/two")
        self.assertEqual(provider, "jinx_system")
        self.assertIn("โมเดลทั้งหมด 2 รายการ", summary)
        self.assertIn("minimax/MiniMax-M3", summary)
        self.assertIn("moonshot/kimi-k2.5", summary)
        self.assertIn("512,000", summary)
        self.assertIn("default, configured", summary)

    def test_empty_model_list_is_a_valid_administrator_response(self) -> None:
        result = subprocess.CompletedProcess(
            [], 0, stdout='{"count": 0, "models": []}', stderr=""
        )

        with patch("helpers.message_orchestrator.list_models_cli", return_value=result):
            self.orchestrator._handle_models("spaces/one", "threads/two")

        self.gateway.send_followup.assert_called_once_with(
            "spaces/one",
            "threads/two",
            "📚 ไม่พบโมเดลที่ตั้งค่าไว้",
            "jinx_system",
        )

    def test_cli_and_payload_errors_are_sent_as_the_administrator(self) -> None:
        failures = [
            subprocess.CompletedProcess([], 2, stdout="", stderr="bad command"),
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"models": {}}', stderr=""),
            FileNotFoundError(),
            subprocess.TimeoutExpired(["openclaw"], 15),
        ]

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.gateway.reset_mock()
                behavior = (
                    {"side_effect": failure}
                    if isinstance(failure, BaseException)
                    else {"return_value": failure}
                )
                with patch("helpers.message_orchestrator.list_models_cli", **behavior):
                    self.orchestrator._handle_models("spaces/one", "threads/two")

                self.gateway.send_followup.assert_called_once()
                _, _, message, provider = self.gateway.send_followup.call_args.args
                self.assertTrue(message.startswith("❌ ไม่สามารถแสดงรายการโมเดลได้:"))
                self.assertEqual(provider, "jinx_system")

    def test_formatter_keeps_malformed_entries_without_crashing(self) -> None:
        summary = format_models_summary(
            [
                None,
                {
                    "name": "Odd *Model*",
                    "key": None,
                    "contextWindow": True,
                    "available": None,
                    "tags": "configured",
                },
            ]
        )

        self.assertIn("รายการที่ 1", summary)
        self.assertIn(r"Odd \*Model\*", summary)
        self.assertIn("context: True", summary)
        self.assertIn("ไม่ทราบสถานะ", summary)


if __name__ == "__main__":
    unittest.main()
