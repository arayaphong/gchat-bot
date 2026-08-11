from __future__ import annotations

import subprocess
import threading
import unittest
import uuid
from unittest.mock import Mock, call, patch

from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import (
    BUSY_TEXT,
    NEW_SESSION_DM_FAILURE_TEMPLATE,
    NEW_SESSION_DM_SUCCESS_TEMPLATE,
    NEW_THREAD_PREPARING_TEXT,
    NEW_THREAD_REDIRECT_TEXT,
)
from helpers.providers import OpenClawClient
from helpers.providers.model_selection import ModelSelection
from helpers.session_keys import ChatSessionContext

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"
COMMAND_ID = "spaces/one/messages/new-command"
NEW_THREAD = "spaces/one/threads/new-root"
CURRENT_CONTEXT = ChatSessionContext.for_thread(SPACE, THREAD)
NEW_CONTEXT = ChatSessionContext.for_thread(SPACE, NEW_THREAD)
ROOT_CONTEXT = ChatSessionContext.from_event(
    SPACE,
    THREAD,
    is_direct_message=True,
    thread_reply=False,
)
SPACE_ROOT_CONTEXT = ChatSessionContext.from_event(
    SPACE,
    THREAD,
    is_direct_message=False,
    thread_reply=False,
)


def new_request_id(purpose: str = "") -> str:
    name = f"gchat-bot:/new:{COMMAND_ID}"
    if purpose:
        name = f"{name}:{purpose}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


class NewSessionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = Mock()
        self.gateway.create_root_thread.return_value = NEW_THREAD
        self.session_manager = Mock()
        self.session_manager.ensure_with_model.return_value = NEW_CONTEXT.session_key
        self.session_manager.abort.return_value = (True, "")
        self.session_watcher = Mock()
        self.attachment_service = Mock()
        self.openclaw_client = Mock(spec=OpenClawClient)
        self.openclaw_client.has_local_file_access.return_value = True
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
            session_watcher=self.session_watcher,
        )

    def test_new_is_locked_instead_of_bypassing_the_processing_gate(self) -> None:
        self.assertNotIn("/new", self.orchestrator._bypass_commands)
        handler = self.orchestrator._locked_commands["/new"]

        self.assertIs(handler.__self__, self.orchestrator)
        self.assertIs(handler.__func__, self.orchestrator._handle_new_session.__func__)

    def test_new_is_rejected_while_a_turn_is_in_flight(self) -> None:
        turn_started = threading.Event()
        release_turn = threading.Event()

        def blocked_turn(*_args: object) -> object:
            turn_started.set()
            if not release_turn.wait(timeout=2):
                raise TimeoutError("test did not release the turn")
            return object()

        self.openclaw_client.send_turn.side_effect = blocked_turn
        real_thread = threading.Thread
        started_threads: list[threading.Thread] = []

        def recording_thread(
            *, target: object, args: tuple[object, ...], daemon: bool
        ) -> threading.Thread:
            worker = real_thread(target=target, args=args, daemon=daemon)  # type: ignore[arg-type]
            started_threads.append(worker)
            return worker

        with patch(
            "helpers.message_orchestrator.threading.Thread",
            side_effect=recording_thread,
        ):
            self.orchestrator.dispatch(
                SPACE,
                THREAD,
                "Alice",
                "inspect",
                [],
                context=CURRENT_CONTEXT,
            )
            self.assertTrue(turn_started.wait(timeout=1))
            try:
                self.orchestrator.dispatch(
                    SPACE,
                    THREAD,
                    "Alice",
                    "/new",
                    [],
                    context=CURRENT_CONTEXT,
                    command_id=COMMAND_ID,
                )
                self.gateway.send_followup.assert_called_once_with(
                    SPACE,
                    THREAD,
                    BUSY_TEXT,
                    "jinx_system",
                )
                self.gateway.create_root_thread.assert_not_called()
                self.session_manager.ensure_with_model.assert_not_called()
            finally:
                release_turn.set()
                for worker in started_threads:
                    worker.join(timeout=2)

        self.openclaw_client.send_turn.assert_called_once_with(
            "inspect",
            "Alice",
            [],
            CURRENT_CONTEXT.session_key,
            None,
        )

    def test_idle_new_ignores_attachments_while_holding_the_gate(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

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

        with patch("helpers.message_orchestrator.threading.Thread", ImmediateThread):
            self.orchestrator.dispatch(
                SPACE,
                THREAD,
                "Alice",
                "/new",
                [{"contentName": "ignored.pdf"}],
                context=CURRENT_CONTEXT,
                command_id=COMMAND_ID,
            )

        self.assertFalse(self.orchestrator.is_processing)
        self.attachment_service.download_with_meta.assert_not_called()
        self.session_manager.ensure_with_model.assert_called_once_with(
            NEW_CONTEXT.session_key,
            "provider/current",
        )
        ignored_notice, success_notice, redirect_notice = (
            self.gateway.send_followup.call_args_list
        )
        self.assertIn("ignored.pdf", ignored_notice.args[2])
        self.assertEqual(success_notice.args[:2], (SPACE, NEW_THREAD))
        self.assertEqual(redirect_notice.args[:2], (SPACE, THREAD))

    def test_new_creates_deterministic_thread_session_with_current_model(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)

        self.assertEqual(
            self.openclaw_client.get_model_selection.call_args_list,
            [
                call(CURRENT_CONTEXT.session_key),
                call(NEW_CONTEXT.session_key),
            ],
        )
        self.gateway.create_root_thread.assert_called_once_with(
            SPACE,
            NEW_THREAD_PREPARING_TEXT,
            "jinx_system",
            request_id=new_request_id(),
        )
        self.session_manager.ensure_with_model.assert_called_once_with(
            NEW_CONTEXT.session_key,
            "provider/current",
        )
        self.session_manager.abort.assert_called_once_with(
            CURRENT_CONTEXT.session_key,
            space=SPACE,
            thread=THREAD,
        )
        self.session_watcher.start.assert_called_once_with()
        self.session_watcher.prepare_session.assert_called_once_with(
            NEW_CONTEXT.session_key,
            SPACE,
            NEW_THREAD,
            NEW_THREAD,
        )
        self.assertEqual(
            self.session_watcher.mock_calls,
            [
                call.prepare_session(
                    NEW_CONTEXT.session_key,
                    SPACE,
                    NEW_THREAD,
                    NEW_THREAD,
                ),
                call.start(),
            ],
        )
        success_notice, redirect_notice = self.gateway.send_followup.call_args_list
        self.assertEqual(success_notice.args[:2], (SPACE, NEW_THREAD))
        self.assertIn("provider/current", success_notice.args[2])
        self.assertEqual(
            success_notice.kwargs,
            {"request_id": new_request_id("success")},
        )
        self.assertEqual(redirect_notice.args[:2], (SPACE, THREAD))
        self.assertEqual(redirect_notice.args[2], NEW_THREAD_REDIRECT_TEXT)
        self.assertEqual(
            redirect_notice.kwargs,
            {"request_id": new_request_id("redirect")},
        )

    def test_redelivery_reports_existing_target_model_and_reuses_notice_ids(
        self,
    ) -> None:
        self.openclaw_client.get_model_selection.side_effect = [
            ModelSelection(
                default_model="provider/default",
                session_model="provider/source-first",
            ),
            ModelSelection(
                default_model="provider/default",
                session_model="provider/target-existing",
            ),
            ModelSelection(
                default_model="provider/default",
                session_model="provider/source-changed",
            ),
            ModelSelection(
                default_model="provider/default",
                session_model="provider/target-existing",
            ),
        ]

        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)
        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)

        self.assertEqual(
            self.gateway.create_root_thread.call_args_list,
            [
                call(
                    SPACE,
                    NEW_THREAD_PREPARING_TEXT,
                    "jinx_system",
                    request_id=new_request_id(),
                ),
                call(
                    SPACE,
                    NEW_THREAD_PREPARING_TEXT,
                    "jinx_system",
                    request_id=new_request_id(),
                ),
            ],
        )
        self.assertEqual(
            self.session_manager.ensure_with_model.call_args_list,
            [
                call(NEW_CONTEXT.session_key, "provider/source-first"),
                call(NEW_CONTEXT.session_key, "provider/source-changed"),
            ],
        )
        success_notices = self.gateway.send_followup.call_args_list[::2]
        redirect_notices = self.gateway.send_followup.call_args_list[1::2]
        self.assertEqual(len(success_notices), 2)
        self.assertEqual(len(redirect_notices), 2)
        for notice in success_notices:
            self.assertIn("provider/target-existing", notice.args[2])
            self.assertNotIn("provider/source", notice.args[2])
            self.assertEqual(
                notice.kwargs,
                {"request_id": new_request_id("success")},
            )
        for notice in redirect_notices:
            self.assertEqual(
                notice.kwargs,
                {"request_id": new_request_id("redirect")},
            )

    def test_new_uses_default_model_when_source_has_no_override(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model=None,
        )

        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)

        self.session_manager.ensure_with_model.assert_called_once_with(
            NEW_CONTEXT.session_key,
            "provider/default",
        )

    def test_new_from_named_space_root_uses_its_real_thread_as_source(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model=None,
        )

        self.orchestrator._handle_new_session(SPACE_ROOT_CONTEXT, COMMAND_ID)

        self.assertEqual(
            self.openclaw_client.get_model_selection.call_args_list[0],
            call(SPACE_ROOT_CONTEXT.session_key),
        )
        self.session_manager.ensure_with_model.assert_called_once_with(
            "agent:main:gchat:one:new-root",
            "provider/default",
        )
        self.session_manager.abort.assert_called_once_with(
            SPACE_ROOT_CONTEXT.session_key,
            space=SPACE,
            thread=THREAD,
        )
        success_notice, redirect_notice = self.gateway.send_followup.call_args_list
        self.assertEqual(success_notice.args[:2], (SPACE, NEW_THREAD))
        self.assertEqual(redirect_notice.args[:2], (SPACE, THREAD))

    def test_new_resets_the_same_direct_message_key_and_reports_actual_model(
        self,
    ) -> None:
        self.openclaw_client.get_model_selection.side_effect = [
            ModelSelection(
                default_model="provider/default",
                session_model="provider/source",
            ),
            ModelSelection(
                default_model="provider/default",
                session_model="provider/effective-after-reset",
            ),
        ]
        self.session_manager.reset.return_value = ROOT_CONTEXT.session_key

        self.orchestrator._handle_new_session(ROOT_CONTEXT, COMMAND_ID)

        self.assertEqual(
            self.openclaw_client.get_model_selection.call_args_list,
            [
                call(ROOT_CONTEXT.session_key),
                call(ROOT_CONTEXT.session_key),
            ],
        )
        self.gateway.create_root_thread.assert_not_called()
        self.session_manager.ensure_with_model.assert_not_called()
        self.session_manager.reset.assert_called_once_with(
            ROOT_CONTEXT.session_key,
            "provider/source",
        )
        self.session_manager.abort.assert_not_called()
        self.session_watcher.start.assert_called_once_with()
        self.session_watcher.prepare_session.assert_called_once_with(
            ROOT_CONTEXT.session_key,
            SPACE,
            THREAD,
            THREAD,
        )
        self.assertEqual(
            self.session_watcher.mock_calls,
            [
                call.prepare_session(
                    ROOT_CONTEXT.session_key,
                    SPACE,
                    THREAD,
                    THREAD,
                ),
                call.start(),
            ],
        )
        self.gateway.send_followup.assert_called_once_with(
            SPACE,
            THREAD,
            NEW_SESSION_DM_SUCCESS_TEMPLATE.format(
                model="provider/effective-after-reset"
            ),
            "jinx_system",
            request_id=new_request_id("dm-success"),
        )

    def test_new_dm_encodes_openclaw_key_but_preserves_google_route_case(
        self,
    ) -> None:
        mixed_space = "spaces/AAQAjEa3Dp8"
        mixed_thread = "spaces/AAQAjEa3Dp8/threads/Zz9"
        mixed_context = ChatSessionContext.from_event(
            mixed_space,
            mixed_thread,
            is_direct_message=True,
            thread_reply=False,
        )
        expected_key = "agent:main:gchat:%41%41%51%41j%45a3%44p8:%5az9"
        self.openclaw_client.get_model_selection.side_effect = [
            ModelSelection(
                default_model="provider/default",
                session_model="provider/current",
            ),
            ModelSelection(
                default_model="provider/default",
                session_model="provider/current",
            ),
        ]
        self.session_manager.reset.return_value = expected_key

        self.orchestrator._handle_new_session(mixed_context, COMMAND_ID)

        self.assertEqual(mixed_context.session_key, expected_key)
        self.assertEqual(expected_key, expected_key.lower())
        self.session_manager.reset.assert_called_once_with(
            expected_key,
            "provider/current",
        )
        self.session_watcher.prepare_session.assert_called_once_with(
            expected_key,
            mixed_space,
            mixed_thread,
            mixed_thread,
        )
        self.gateway.send_followup.assert_called_once()
        self.assertEqual(
            self.gateway.send_followup.call_args.args[:2],
            (mixed_space, mixed_thread),
        )

    def test_new_direct_message_reset_failure_uses_an_ambiguous_state_notice(
        self,
    ) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )
        self.session_manager.reset.side_effect = RuntimeError("gateway unavailable")

        self.orchestrator._handle_new_session(ROOT_CONTEXT, COMMAND_ID)

        self.session_manager.reset.assert_called_once_with(
            ROOT_CONTEXT.session_key,
            "provider/current",
        )
        self.gateway.create_root_thread.assert_not_called()
        self.session_manager.ensure_with_model.assert_not_called()
        self.session_manager.abort.assert_not_called()
        self.session_watcher.prepare_session.assert_not_called()
        self.gateway.send_followup.assert_called_once()
        notice = self.gateway.send_followup.call_args
        self.assertEqual(notice.args[:2], (SPACE, THREAD))
        self.assertEqual(
            notice.args[2],
            NEW_SESSION_DM_FAILURE_TEMPLATE.format(reason="gateway unavailable"),
        )
        self.assertNotIn("เซสชั่นเดิมยังคงใช้งานอยู่", notice.args[2])
        self.assertEqual(
            notice.kwargs,
            {"request_id": new_request_id("dm-failure")},
        )

    def test_model_lookup_failure_does_not_create_thread_or_session(self) -> None:
        self.openclaw_client.get_model_selection.side_effect = (
            subprocess.TimeoutExpired(["openclaw"], 15)
        )

        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)

        self.gateway.create_root_thread.assert_not_called()
        self.session_manager.ensure_with_model.assert_not_called()
        self.session_manager.abort.assert_not_called()
        self.assertIn(
            "เซสชั่นเดิมยังคงใช้งานอยู่",
            self.gateway.send_followup.call_args.args[2],
        )
        self.assertEqual(
            self.gateway.send_followup.call_args.kwargs,
            {"request_id": new_request_id("failure:source")},
        )

    def test_root_thread_creation_failure_preserves_source_session(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )
        self.gateway.create_root_thread.side_effect = RuntimeError(
            "Chat API unavailable"
        )

        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)

        self.session_manager.ensure_with_model.assert_not_called()
        self.session_manager.abort.assert_not_called()
        self.session_watcher.prepare_session.assert_not_called()
        self.gateway.send_followup.assert_called_once()
        self.assertIn(
            "Chat API unavailable", self.gateway.send_followup.call_args.args[2]
        )
        self.assertEqual(
            self.gateway.send_followup.call_args.kwargs,
            {"request_id": new_request_id("failure:source")},
        )

    def test_session_creation_failure_notifies_source_and_created_thread(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )
        self.session_manager.ensure_with_model.side_effect = RuntimeError(
            "gateway rejected *model*"
        )

        self.orchestrator._handle_new_session(CURRENT_CONTEXT, COMMAND_ID)

        self.session_manager.abort.assert_not_called()
        self.assertEqual(len(self.gateway.send_followup.call_args_list), 2)
        source_failure, new_thread_failure = self.gateway.send_followup.call_args_list
        self.assertEqual(source_failure.args[:2], (SPACE, THREAD))
        self.assertEqual(new_thread_failure.args[:2], (SPACE, NEW_THREAD))
        for notice in (source_failure, new_thread_failure):
            self.assertIn(r"gateway rejected \*model\*", notice.args[2])
        self.assertEqual(
            source_failure.kwargs,
            {"request_id": new_request_id("failure:source")},
        )
        self.assertEqual(
            new_thread_failure.kwargs,
            {"request_id": new_request_id("failure:target")},
        )

    def test_notices_omit_request_id_without_a_command_id(self) -> None:
        self.openclaw_client.get_model_selection.return_value = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        self.orchestrator._handle_new_session(CURRENT_CONTEXT)

        success_notice, redirect_notice = self.gateway.send_followup.call_args_list
        self.assertEqual(success_notice.kwargs, {})
        self.assertEqual(redirect_notice.kwargs, {})


if __name__ == "__main__":
    unittest.main()
