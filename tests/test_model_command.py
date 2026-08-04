from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers.message_orchestrator import MessageOrchestrator
from helpers.model_commands import is_model_command, parse_model_key
from helpers.orchestrator_messages import BUSY_TEXT, format_model_validation_failure
from helpers.providers import ProviderSettings


def models_result(models: list[object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"models": models}), stderr=""
    )


class ModelCommandParsingTests(unittest.TestCase):
    def test_command_name_is_case_insensitive_but_requires_a_boundary(self) -> None:
        self.assertTrue(is_model_command("/model provider/model"))
        self.assertTrue(is_model_command(" /MODEL\tprovider/model "))
        self.assertFalse(is_model_command("/models"))
        self.assertFalse(is_model_command("/modelx provider/model"))

    def test_parser_requires_exactly_one_non_whitespace_key(self) -> None:
        self.assertEqual(
            parse_model_key(" /MODEL\tminimax/MiniMax-M3 "),
            "minimax/MiniMax-M3",
        )
        for text in ("/model", "/model   ", "/model one two", "/model one\ntwo"):
            with self.subTest(text=text):
                self.assertIsNone(parse_model_key(text))

    def test_validation_failure_escapes_gateway_error_text(self) -> None:
        self.assertEqual(
            format_model_validation_failure("bad *model* <value>"),
            r"❌ ไม่สามารถตรวจสอบโมเดลได้: bad \*model\* \<value\>",
        )


class ModelCommandValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:c0ffee",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )
        self.gateway = Mock()
        self.attachment_service = Mock()
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=SimpleNamespace(settings=self.settings),
            attachment_service=self.attachment_service,
        )

    def run_locked(
        self,
        text: str,
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        self.assertTrue(self.orchestrator._processing_lock.acquire(blocking=False))
        self.orchestrator._handle_message(
            "spaces/one",
            "spaces/one/threads/two",
            "Alice",
            text,
            attachments or [],
            self.settings,
        )
        self.assertFalse(self.orchestrator._processing_lock.locked())

    def test_usable_exact_key_is_canonicalized_and_forwarded_once(self) -> None:
        result = models_result(
            [
                {
                    "key": "minimax/MiniMax-M3",
                    "available": True,
                    "missing": False,
                }
            ]
        )

        with (
            patch(
                "helpers.message_orchestrator.list_models_cli", return_value=result
            ) as list_models,
            patch(
                "helpers.message_orchestrator.ask_provider",
                return_value=("updated", "kimiclaw", []),
            ) as ask_provider,
        ):
            self.run_locked(
                " /MODEL\tminimax/MiniMax-M3 ",
                attachments=[{"contentName": "ignored.png"}],
            )

        list_models.assert_called_once_with()
        ask_provider.assert_called_once_with(
            "/model minimax/MiniMax-M3",
            "Alice",
            [],
            self.settings,
            None,
        )
        self.attachment_service.download_with_meta.assert_not_called()
        self.assertEqual(len(self.gateway.send_followup.call_args_list), 2)
        ignored_notice, reply = self.gateway.send_followup.call_args_list
        self.assertEqual(ignored_notice.args[3], "jinx_system")
        self.assertIn("ignored.png", ignored_notice.args[2])
        self.assertEqual(
            reply.args,
            (
                "spaces/one",
                "spaces/one/threads/two",
                "updated",
                "kimiclaw",
            ),
        )

    def test_unknown_and_case_mismatched_keys_are_rejected_locally(self) -> None:
        result = models_result(
            [{"key": "minimax/MiniMax-M3", "available": True, "missing": False}]
        )

        for model_key in ("unknown/model", "minimax/minimax-m3"):
            with self.subTest(model_key=model_key):
                self.gateway.reset_mock()
                with (
                    patch(
                        "helpers.message_orchestrator.list_models_cli",
                        return_value=result,
                    ),
                    patch("helpers.message_orchestrator.ask_provider") as ask_provider,
                ):
                    self.run_locked(f"/model {model_key}")

                ask_provider.assert_not_called()
                message = self.gateway.send_followup.call_args.args[2]
                self.assertIn("ไม่พบโมเดล", message)
                self.assertEqual(
                    self.gateway.send_followup.call_args.args[3], "jinx_system"
                )

    def test_every_non_usable_matching_model_is_rejected_locally(self) -> None:
        unusable_fields = [
            {"available": False, "missing": False},
            {"available": None, "missing": False},
            {"available": "true", "missing": False},
            {"available": True, "missing": True},
        ]

        for fields in unusable_fields:
            with self.subTest(fields=fields):
                self.gateway.reset_mock()
                result = models_result([{"key": "provider/model", **fields}])
                with (
                    patch(
                        "helpers.message_orchestrator.list_models_cli",
                        return_value=result,
                    ),
                    patch("helpers.message_orchestrator.ask_provider") as ask_provider,
                ):
                    self.run_locked("/model provider/model")

                ask_provider.assert_not_called()
                message = self.gateway.send_followup.call_args.args[2]
                self.assertIn("ไม่พร้อมใช้งาน", message)
                self.assertEqual(
                    self.gateway.send_followup.call_args.args[3], "jinx_system"
                )

    def test_invalid_syntax_never_loads_the_catalog_or_calls_the_provider(self) -> None:
        for text in ("/model", "/model   ", "/model one two", "/model one\ntwo"):
            with self.subTest(text=text):
                self.gateway.reset_mock()
                with (
                    patch(
                        "helpers.message_orchestrator.list_models_cli"
                    ) as list_models,
                    patch("helpers.message_orchestrator.ask_provider") as ask_provider,
                ):
                    self.run_locked(text, attachments=[{"contentName": "ignored.png"}])

                list_models.assert_not_called()
                ask_provider.assert_not_called()
                self.attachment_service.download_with_meta.assert_not_called()
                self.assertIn(
                    "/model <model-key>", self.gateway.send_followup.call_args.args[2]
                )

    def test_catalog_and_cli_failures_fail_closed(self) -> None:
        failures: list[object] = [
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
                with (
                    patch("helpers.message_orchestrator.list_models_cli", **behavior),
                    patch("helpers.message_orchestrator.ask_provider") as ask_provider,
                ):
                    self.run_locked("/model provider/model")

                ask_provider.assert_not_called()
                message = self.gateway.send_followup.call_args.args[2]
                self.assertTrue(message.startswith("❌ ไม่สามารถตรวจสอบโมเดลได้:"))
                self.assertEqual(
                    self.gateway.send_followup.call_args.args[3], "jinx_system"
                )

    def test_duplicate_model_keys_fail_closed(self) -> None:
        duplicate = {"key": "provider/model", "available": True, "missing": False}
        result = models_result([duplicate, duplicate.copy()])

        with (
            patch("helpers.message_orchestrator.list_models_cli", return_value=result),
            patch("helpers.message_orchestrator.ask_provider") as ask_provider,
        ):
            self.run_locked("/model provider/model")

        ask_provider.assert_not_called()
        self.assertTrue(
            self.gateway.send_followup.call_args.args[2].startswith(
                "❌ ไม่สามารถตรวจสอบโมเดลได้:"
            )
        )

    def test_busy_model_command_does_not_start_validation(self) -> None:
        self.assertTrue(self.orchestrator._processing_lock.acquire(blocking=False))
        try:
            with patch("helpers.message_orchestrator.list_models_cli") as list_models:
                self.orchestrator.dispatch(
                    "spaces/one",
                    "spaces/one/threads/two",
                    "Alice",
                    "/model provider/model",
                    [],
                )

            list_models.assert_not_called()
            self.gateway.send_followup.assert_called_once_with(
                "spaces/one",
                "spaces/one/threads/two",
                BUSY_TEXT,
                "jinx_system",
            )
        finally:
            self.orchestrator._processing_lock.release()


if __name__ == "__main__":
    unittest.main()
