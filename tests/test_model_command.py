from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from helpers import session_manager
from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import BUSY_TEXT, format_attachment_busy
from helpers.providers import OpenClawClient, openclaw_cli
from helpers.session_keys import ChatSessionContext

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"
SESSION_KEY = "agent:main:gchat:one:two"


class ImmediateThread:
    def __init__(
        self,
        *,
        target: object,
        args: tuple[object, ...],
        daemon: bool,
    ) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self) -> None:
        self.target(*self.args)  # type: ignore[operator]


class ForwardedModelTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ChatSessionContext.for_thread(SPACE, THREAD)
        self.gateway = Mock()
        self.attachment_service = Mock()
        self.session_manager = Mock()
        self.session_watcher = Mock()
        self.openclaw_client = Mock(spec=OpenClawClient)
        self.openclaw_client.has_local_file_access.return_value = True
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
            session_watcher=self.session_watcher,
        )

    def dispatch_sync(
        self,
        text: str,
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        with patch(
            "helpers.message_orchestrator.threading.Thread",
            ImmediateThread,
        ):
            self.orchestrator.dispatch(
                SPACE,
                THREAD,
                "Alice",
                text,
                attachments or [],
                context=self.context,
            )

    def reset_dependencies(self) -> None:
        self.gateway.reset_mock()
        self.attachment_service.reset_mock()
        self.session_manager.reset_mock()
        self.session_watcher.reset_mock()
        self.openclaw_client.reset_mock()
        self.openclaw_client.has_local_file_access.return_value = True

    def test_every_model_shaped_text_is_forwarded_unchanged_as_a_normal_turn(
        self,
    ) -> None:
        messages = (
            "/model",
            "/model   ",
            "/model provider/model",
            " /MODEL\tminimax/MiniMax-M3 ",
            "/MODEL",
            "/model one two",
            "/model one\ntwo",
            "/modelx provider/model",
            "/model-provider/model",
        )

        for message in messages:
            with self.subTest(message=message):
                self.reset_dependencies()

                self.dispatch_sync(message)

                self.openclaw_client.send_turn.assert_called_once_with(
                    message,
                    "Alice",
                    [],
                    SESSION_KEY,
                    None,
                )
                self.openclaw_client.list_models.assert_not_called()
                self.assertEqual(self.session_manager.method_calls, [])
                self.assertEqual(
                    self.session_watcher.mock_calls,
                    [
                        call.prepare_session(SESSION_KEY, SPACE, THREAD, THREAD),
                        call.start(),
                    ],
                )
                self.gateway.send_followup.assert_not_called()

    def test_model_text_with_attachment_uses_the_normal_attachment_pipeline(
        self,
    ) -> None:
        attachment = {"contentName": "model-reference.png"}
        downloaded = [
            {
                "fp": "/tmp/model-reference.png",
                "meta": {
                    "contentName": "model-reference.png",
                    "savedSize": 12,
                },
            }
        ]
        self.attachment_service.download_with_meta.return_value = downloaded

        self.dispatch_sync("/model provider/model", [attachment])

        self.attachment_service.download_with_meta.assert_called_once_with([attachment])
        self.openclaw_client.has_local_file_access.assert_called_once_with()
        self.openclaw_client.send_turn.assert_called_once_with(
            "/model provider/model",
            "Alice",
            downloaded,
            SESSION_KEY,
            None,
        )
        self.attachment_service.cleanup.assert_not_called()
        self.gateway.send_followup.assert_not_called()

    def test_busy_model_text_uses_the_same_busy_semantics_as_other_turns(self) -> None:
        scenarios = (
            ([], BUSY_TEXT),
            ([{"contentName": "model-reference.png"}], format_attachment_busy(1)),
        )

        for attachments, expected_text in scenarios:
            with self.subTest(attachments=attachments):
                self.reset_dependencies()
                self.assertTrue(
                    self.orchestrator._processing_lock.acquire(blocking=False)
                )
                try:
                    self.orchestrator.dispatch(
                        SPACE,
                        THREAD,
                        "Alice",
                        "/model provider/model",
                        attachments,
                        context=self.context,
                    )
                finally:
                    self.orchestrator._processing_lock.release()

                self.gateway.send_followup.assert_called_once_with(
                    SPACE,
                    THREAD,
                    expected_text,
                    "jinx_system",
                )
                self.attachment_service.download_with_meta.assert_not_called()
                self.openclaw_client.send_turn.assert_not_called()
                self.openclaw_client.list_models.assert_not_called()
                self.assertEqual(self.session_manager.method_calls, [])
                self.assertEqual(self.session_watcher.method_calls, [])

    def test_model_mutation_apis_remain_absent(self) -> None:
        self.assertFalse(hasattr(OpenClawClient, "patch_session_model"))
        self.assertFalse(hasattr(session_manager.SessionManager, "set_model"))
        self.assertFalse(hasattr(openclaw_cli, "patch_session_model"))


if __name__ == "__main__":
    unittest.main()
