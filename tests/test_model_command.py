from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock

from helpers.message_orchestrator import MessageOrchestrator
from helpers.model_commands import is_model_command, parse_model_key
from helpers.orchestrator_messages import BUSY_TEXT, format_model_validation_failure
from helpers.providers import OpenClawClient
from helpers.session_keys import ChatSessionContext

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"
SESSION_KEY = "agent:main:gchat:one:two"


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

    def test_validation_failure_escapes_every_markdown_metacharacter(self) -> None:
        self.assertEqual(
            format_model_validation_failure(r"\`*{}_[]<>#"),
            r"❌ ไม่สามารถตรวจสอบโมเดลได้: \\\`\*\{\}\_\[\]\<\>\#",
        )


class ModelCommandValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ChatSessionContext.for_thread(SPACE, THREAD)
        self.gateway = Mock()
        self.attachment_service = Mock()
        self.session_manager = Mock()
        self.session_manager.set_model.return_value = SESSION_KEY
        self.session_watcher = Mock()
        self.openclaw_client = Mock(spec=OpenClawClient)
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
            session_watcher=self.session_watcher,
        )

    def run_locked(
        self,
        text: str,
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        self.assertTrue(self.orchestrator._processing_lock.acquire(blocking=False))
        self.orchestrator._handle_message(
            self.context,
            "Alice",
            text,
            attachments or [],
        )
        self.assertFalse(self.orchestrator._processing_lock.locked())

    def test_usable_exact_key_creates_a_fresh_model_session_once(self) -> None:
        self.openclaw_client.list_models.return_value = [
            {
                "key": "minimax/MiniMax-M3",
                "available": True,
                "missing": False,
            }
        ]

        self.run_locked(
            " /MODEL\tminimax/MiniMax-M3 ",
            attachments=[{"contentName": "ignored.png"}],
        )

        self.openclaw_client.list_models.assert_called_once_with()
        self.session_manager.set_model.assert_called_once_with(
            SESSION_KEY,
            "minimax/MiniMax-M3",
        )
        self.session_watcher.start.assert_called_once_with()
        self.session_watcher.prepare_session.assert_called_once_with(
            SESSION_KEY,
            SPACE,
            THREAD,
            THREAD,
        )
        self.openclaw_client.send_turn.assert_not_called()
        self.attachment_service.download_with_meta.assert_not_called()
        self.assertEqual(len(self.gateway.send_followup.call_args_list), 2)
        ignored_notice, reply = self.gateway.send_followup.call_args_list
        self.assertEqual(ignored_notice.args[3], "jinx_system")
        self.assertIn("ignored.png", ignored_notice.args[2])
        self.assertEqual(
            reply.args[:2],
            ("spaces/one", "spaces/one/threads/two"),
        )
        self.assertIn("เปลี่ยนโมเดล", reply.args[2])
        self.assertIn("minimax/MiniMax-M3", reply.args[2])
        self.assertEqual(reply.args[3], "jinx_system")

    def test_session_creation_failure_keeps_the_command_out_of_providers(self) -> None:
        self.openclaw_client.list_models.return_value = [
            {"key": "provider/model", "available": True, "missing": False}
        ]
        self.session_manager.set_model.side_effect = RuntimeError(
            "gateway rejected *model*"
        )

        self.run_locked("/model provider/model")

        self.session_manager.set_model.assert_called_once_with(
            SESSION_KEY,
            "provider/model",
        )
        self.openclaw_client.send_turn.assert_not_called()
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn("ไม่สามารถเปลี่ยนโมเดล", message)
        self.assertIn(r"gateway rejected \*model\*", message)
        self.assertEqual(self.gateway.send_followup.call_args.args[3], "jinx_system")

    def test_unknown_and_case_mismatched_keys_are_rejected_locally(self) -> None:
        self.openclaw_client.list_models.return_value = [
            {"key": "minimax/MiniMax-M3", "available": True, "missing": False}
        ]

        for model_key in ("unknown/model", "minimax/minimax-m3"):
            with self.subTest(model_key=model_key):
                self.gateway.reset_mock()
                self.run_locked(f"/model {model_key}")

                self.openclaw_client.send_turn.assert_not_called()
                self.session_manager.set_model.assert_not_called()
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
                self.openclaw_client.list_models.return_value = [
                    {"key": "provider/model", **fields}
                ]
                self.run_locked("/model provider/model")

                self.openclaw_client.send_turn.assert_not_called()
                self.session_manager.set_model.assert_not_called()
                message = self.gateway.send_followup.call_args.args[2]
                self.assertIn("ไม่พร้อมใช้งาน", message)
                self.assertEqual(
                    self.gateway.send_followup.call_args.args[3], "jinx_system"
                )

    def test_invalid_syntax_never_loads_the_catalog_or_calls_the_provider(self) -> None:
        for text in ("/model", "/model   ", "/model one two", "/model one\ntwo"):
            with self.subTest(text=text):
                self.gateway.reset_mock()
                self.run_locked(text, attachments=[{"contentName": "ignored.png"}])

                self.openclaw_client.list_models.assert_not_called()
                self.openclaw_client.send_turn.assert_not_called()
                self.session_manager.set_model.assert_not_called()
                self.attachment_service.download_with_meta.assert_not_called()
                self.assertIn(
                    "/model <model-key>", self.gateway.send_followup.call_args.args[2]
                )

    def test_catalog_and_cli_failures_fail_closed(self) -> None:
        failures: list[BaseException] = [
            RuntimeError("openclaw models list คืนค่ารหัส 2: bad command"),
            ValueError("invalid JSON"),
            TypeError("รูปแบบข้อมูลจาก openclaw ไม่ถูกต้อง"),
            FileNotFoundError(),
            subprocess.TimeoutExpired(["openclaw"], 15),
        ]

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.gateway.reset_mock()
                self.openclaw_client.list_models.side_effect = failure
                self.run_locked("/model provider/model")
                self.openclaw_client.list_models.side_effect = None

                self.openclaw_client.send_turn.assert_not_called()
                self.session_manager.set_model.assert_not_called()
                message = self.gateway.send_followup.call_args.args[2]
                self.assertTrue(message.startswith("❌ ไม่สามารถตรวจสอบโมเดลได้:"))
                self.assertEqual(
                    self.gateway.send_followup.call_args.args[3], "jinx_system"
                )

    def test_duplicate_model_keys_fail_closed(self) -> None:
        duplicate = {"key": "provider/model", "available": True, "missing": False}
        self.openclaw_client.list_models.return_value = [duplicate, duplicate.copy()]

        self.run_locked("/model provider/model")

        self.openclaw_client.send_turn.assert_not_called()
        self.session_manager.set_model.assert_not_called()
        self.assertTrue(
            self.gateway.send_followup.call_args.args[2].startswith(
                "❌ ไม่สามารถตรวจสอบโมเดลได้:"
            )
        )

    def test_busy_model_command_does_not_start_validation(self) -> None:
        self.assertTrue(self.orchestrator._processing_lock.acquire(blocking=False))
        try:
            self.orchestrator.dispatch(
                "spaces/one",
                "spaces/one/threads/two",
                "Alice",
                "/model provider/model",
                [],
                context=self.context,
            )

            self.openclaw_client.list_models.assert_not_called()
            self.session_manager.set_model.assert_not_called()
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
