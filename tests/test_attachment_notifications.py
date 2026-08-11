from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers.chat_target_store import (
    ChatTarget,
    ChatTargetConflictError,
    FixedChatTargetStore,
)
from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import (
    format_attachment_download_result,
    format_attachment_limit,
    format_attachment_remote_unavailable,
    format_outbound_attachment_failure,
)
from helpers.outbound_attachment_watcher import (
    AttachmentSubmissionDisposition,
    AttachmentSubmissionResult,
)
from helpers.processing_gate import ProcessingGate
from helpers.providers import OpenClawClient, SendTurnResult
from helpers.providers.openclaw_provider import (
    NO_ASSISTANT_TEXT_INFO,
    build_openclaw_prompt,
)
from helpers.services import AttachmentService
from helpers.session_keys import ChatSessionContext
from helpers.session_trajectory_watcher import AssistantTrajectoryMessage

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"
CONTEXT = ChatSessionContext.for_thread(SPACE, THREAD)
SESSION_KEY = CONTEXT.session_key


class AttachmentNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        local_access = patch.dict(
            "os.environ",
            {"OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS": "allow"},
            clear=False,
        )
        local_access.start()
        self.addCleanup(local_access.stop)
        self.gateway = Mock()
        self.attachment_service = Mock()
        self.session_manager = Mock()
        self.session_manager.set_model.return_value = SESSION_KEY
        self.session_watcher = Mock()
        self.openclaw_client = Mock(spec=OpenClawClient)
        self.openclaw_client.has_local_file_access.return_value = True
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
            max_attachments_per_message=2,
            session_watcher=self.session_watcher,
        )

    def run_locked(
        self,
        text: str,
        attachments: list[dict[str, object]],
    ) -> None:
        self.assertTrue(self.orchestrator._processing_lock.acquire(blocking=False))
        self.orchestrator._handle_message(
            CONTEXT,
            "Alice",
            text,
            attachments,
        )
        self.assertFalse(self.orchestrator._processing_lock.locked())

    def system_texts(self) -> list[str]:
        return [
            call.args[2]
            for call in self.gateway.send_followup.call_args_list
            if len(call.args) >= 4 and call.args[3] == "jinx_system"
        ]

    def test_outbound_failure_notice_escapes_the_filename(self) -> None:
        notice = format_outbound_attachment_failure("bad*[name].pdf", 3)

        self.assertIn(r"bad\*\[name\].pdf", notice)
        self.assertIn("3 ครั้ง", notice)
        self.assertIn(
            "ไฟล์ว่างเปล่า",
            format_outbound_attachment_failure("empty.txt", 0, "empty_file"),
        )

    def test_only_limit_and_failed_download_are_jinx_notifications(self) -> None:
        attachments = [
            {"contentName": "accepted.txt"},
            {"contentName": "oversized.bin"},
            {"contentName": "ignored.pdf"},
        ]
        downloaded = [
            {
                "fp": "/home/arme/.openclaw/workspace/downloads/accepted.txt",
                "meta": {
                    "contentName": "accepted.txt",
                    "contentType": "text/plain",
                    "savedSize": 12,
                },
            },
            {
                "fp": None,
                "meta": {
                    "contentName": "oversized.bin",
                    "contentType": "application/octet-stream",
                    "errorCode": "too_large",
                    "error": "attachment exceeds 20 byte limit",
                },
            },
        ]
        events: list[tuple[str, object]] = []

        def send_followup(_space: str, _thread: str, text: str, provider: str) -> None:
            events.append((f"notify:{provider}", text))

        def download(selected: list[dict[str, object]]) -> list[dict[str, object]]:
            events.append(("download", selected))
            return downloaded

        def provider(*_args: object) -> SendTurnResult:
            events.append(("provider", None))
            return SendTurnResult(text="done", run_id="chatcmpl_done")

        self.gateway.send_followup.side_effect = send_followup
        self.attachment_service.download_with_meta.side_effect = download

        self.openclaw_client.send_turn.side_effect = provider
        self.run_locked("inspect", attachments)

        self.attachment_service.download_with_meta.assert_called_once_with(
            attachments[:2]
        )
        self.openclaw_client.send_turn.assert_called_once_with(
            "inspect",
            "Alice",
            downloaded,
            SESSION_KEY,
            None,
        )
        self.session_watcher.start.assert_called_once_with()
        self.session_watcher.prepare_session.assert_called_once_with(
            SESSION_KEY,
            SPACE,
            THREAD,
            THREAD,
        )
        self.attachment_service.cleanup.assert_not_called()

        download_index = next(
            i for i, event in enumerate(events) if event[0] == "download"
        )
        provider_index = next(
            i for i, event in enumerate(events) if event[0] == "provider"
        )
        limit_notice = format_attachment_limit(3, 2, ["ignored.pdf"])
        outcome_notice = format_attachment_download_result(
            2,
            [("oversized.bin", "ไฟล์มีขนาดเกินขีดจำกัดของระบบ")],
        )
        self.assertLess(
            events.index(("notify:jinx_system", limit_notice)),
            download_index,
            "Jinx must report attachments skipped by the limit before downloading",
        )
        self.assertLess(
            events.index(("notify:jinx_system", outcome_notice)),
            provider_index,
            "Jinx must report a failed download before provider work",
        )
        self.assertEqual(
            self.system_texts(),
            [limit_notice, outcome_notice],
            "Jinx must stay silent for download progress",
        )

        notices = "\n".join(self.system_texts())
        self.assertNotIn("accepted.txt", notices)
        self.assertIn("oversized.bin", notices)
        self.assertIn("ignored.pdf", notices)
        self.assertNotIn("20 byte limit", notices)

    def test_successful_download_is_silent_for_jinx_and_leaves_files_in_place(
        self,
    ) -> None:
        downloaded = [
            {
                "fp": "/home/arme/.openclaw/workspace/downloads/report.pdf",
                "meta": {"contentName": "report.pdf", "savedSize": 42},
            }
        ]
        self.attachment_service.download_with_meta.return_value = downloaded

        self.openclaw_client.send_turn.return_value = SendTurnResult(
            text="done",
            run_id="chatcmpl_done",
        )
        self.run_locked("inspect", [{"contentName": "report.pdf"}])

        self.openclaw_client.send_turn.assert_called_once_with(
            "inspect",
            "Alice",
            downloaded,
            SESSION_KEY,
            None,
        )
        self.attachment_service.cleanup.assert_not_called()
        self.assertEqual(self.system_texts(), [])
        self.gateway.send_followup.assert_not_called()

    def test_provider_response_text_is_never_delivered_directly(self) -> None:
        self.openclaw_client.send_turn.return_value = SendTurnResult(
            text=NO_ASSISTANT_TEXT_INFO,
            run_id="chatcmpl_no_text",
        )
        self.run_locked("create the file", [])

        self.assertEqual(self.system_texts(), [])
        self.gateway.send_followup.assert_not_called()

    def test_provider_failure_reports_error_and_leaves_downloads_in_place(
        self,
    ) -> None:
        downloaded = [
            {
                "fp": "/home/arme/.openclaw/workspace/downloads/report.pdf",
                "meta": {"contentName": "report.pdf", "savedSize": 42},
            }
        ]
        self.attachment_service.download_with_meta.return_value = downloaded

        self.openclaw_client.send_turn.side_effect = RuntimeError(
            "provider unavailable"
        )
        self.run_locked("inspect", [{"contentName": "report.pdf"}])

        self.attachment_service.cleanup.assert_not_called()
        notices = "\n".join(self.system_texts())
        self.assertIn("provider unavailable", notices)
        self.assertTrue(self.system_texts()[0].startswith("❌"))

    def test_busy_request_with_attachments_gets_attachment_specific_jinx_notice(
        self,
    ) -> None:
        attachments = [
            {"contentName": "one.txt"},
            {"contentName": "two.txt"},
            {"contentName": "three.txt"},
        ]
        self.assertTrue(self.orchestrator._processing_lock.acquire(blocking=False))
        try:
            self.orchestrator.dispatch(
                SPACE,
                THREAD,
                "Alice",
                "inspect",
                attachments,
                context=CONTEXT,
            )
        finally:
            self.orchestrator._processing_lock.release()

        self.attachment_service.download_with_meta.assert_not_called()
        self.gateway.send_followup.assert_called_once()
        _, _, text, provider = self.gateway.send_followup.call_args.args
        self.assertEqual(provider, "jinx_system")
        self.assertIn("3", text)

    def test_provider_and_reply_hold_the_shared_process_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "processing.lock"
            processing_gate = ProcessingGate(lock_file)
            peer_gate = ProcessingGate(lock_file)
            orchestrator = MessageOrchestrator(
                gateway=self.gateway,
                session_manager=self.session_manager,
                attachment_service=self.attachment_service,
                openclaw_client=self.openclaw_client,
                processing_gate=processing_gate,
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

            def provider(*_args: object) -> SendTurnResult:
                self.assertIsNone(peer_gate.try_acquire())
                return SendTurnResult(text="done", run_id="chatcmpl_done")

            def send_followup(*_args: object) -> bool:
                self.assertIsNone(peer_gate.try_acquire())
                return True

            self.gateway.send_followup.side_effect = send_followup
            self.openclaw_client.send_turn.side_effect = provider
            with patch(
                "helpers.message_orchestrator.threading.Thread", ImmediateThread
            ):
                orchestrator.dispatch(
                    SPACE,
                    THREAD,
                    "Alice",
                    "hello",
                    [],
                    context=CONTEXT,
                )

            available_after_reply = peer_gate.try_acquire()
            self.assertIsNotNone(available_after_reply)
            assert available_after_reply is not None
            available_after_reply.release()

    def test_model_command_attachments_are_explicitly_ignored_by_jinx(self) -> None:
        self.openclaw_client.list_models.return_value = [
            {
                "key": "minimax/MiniMax-M3",
                "available": True,
                "missing": False,
            }
        ]

        self.run_locked(
            "/model minimax/MiniMax-M3",
            [{"contentName": "ignored.png"}],
        )

        self.attachment_service.download_with_meta.assert_not_called()
        self.attachment_service.cleanup.assert_not_called()
        self.session_manager.set_model.assert_called_once_with(
            SESSION_KEY,
            "minimax/MiniMax-M3",
        )
        self.openclaw_client.send_turn.assert_not_called()
        notices = "\n".join(self.system_texts())
        self.assertIn("ignored.png", notices)
        self.assertIn("เปลี่ยนโมเดล", notices)

    def test_bypass_command_attachments_are_ignored_before_command_runs(self) -> None:
        manager = Mock()
        manager.abort.return_value = (True, "")
        orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
            max_attachments_per_message=2,
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
            orchestrator.dispatch(
                SPACE,
                THREAD,
                "Alice",
                "/abort",
                [{"contentName": "ignored-by-abort.txt"}],
                context=CONTEXT,
            )

        manager.abort.assert_called_once_with(
            SESSION_KEY,
            space=SPACE,
            thread=THREAD,
        )
        self.attachment_service.download_with_meta.assert_not_called()
        notices = "\n".join(self.system_texts())
        self.assertIn("ignored-by-abort.txt", notices)

    def test_download_setup_failure_is_reported_without_leaking_a_signed_uri(
        self,
    ) -> None:
        self.attachment_service.download_with_meta.side_effect = RuntimeError(
            "GET https://files.example/private?token=super-secret failed"
        )

        self.run_locked("inspect", [{"contentName": "private.txt"}])

        self.openclaw_client.send_turn.assert_not_called()
        self.attachment_service.cleanup.assert_not_called()
        notices = "\n".join(self.system_texts())
        self.assertNotIn("super-secret", notices)
        self.assertNotIn("https://files.example", notices)

    def test_remote_openclaw_gateway_rejects_unshared_local_downloads(self) -> None:
        downloaded = [
            {
                "fp": "/home/arme/.openclaw/workspace/downloads/private.txt",
                "meta": {"contentName": "private.txt", "savedSize": 12},
            }
        ]
        self.attachment_service.download_with_meta.return_value = downloaded

        self.openclaw_client.has_local_file_access.return_value = False
        self.run_locked(
            "inspect",
            [{"contentName": "private.txt"}],
        )

        self.openclaw_client.send_turn.assert_not_called()
        self.attachment_service.cleanup.assert_not_called()
        self.assertIn(
            format_attachment_remote_unavailable(["private.txt"]),
            self.system_texts(),
        )

    def test_invalid_local_access_policy_fails_closed(self) -> None:
        downloaded = [
            {
                "fp": "/home/arme/.openclaw/workspace/downloads/private.txt",
                "meta": {"contentName": "private.txt", "savedSize": 12},
            }
        ]
        self.attachment_service.download_with_meta.return_value = downloaded

        self.openclaw_client.has_local_file_access.side_effect = ValueError(
            "OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS must be one of: allow, auto, deny"
        )
        self.run_locked("inspect", [{"contentName": "private.txt"}])

        self.openclaw_client.send_turn.assert_not_called()
        self.attachment_service.cleanup.assert_not_called()
        self.assertIn(
            format_attachment_remote_unavailable(["private.txt"]),
            self.system_texts(),
        )


class AttachmentIngressTests(unittest.TestCase):
    def test_app_shares_one_openclaw_client_across_consumers(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        self.assertIs(
            app_module.session_manager._openclaw_client,
            app_module.openclaw_client,
        )
        self.assertIs(
            app_module.orchestrator._openclaw_client,
            app_module.openclaw_client,
        )

    def test_app_disables_unscoped_watch_and_allows_media_under_home_and_tmp(
        self,
    ) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        self.assertEqual(
            app_module.OUTBOUND_ATTACHMENT_CONFIG.watched_source_dirs,
            (),
        )
        self.assertEqual(
            app_module.OUTBOUND_ATTACHMENT_CONFIG.source_dirs,
            (
                Path.home().resolve(strict=False),
                Path("/tmp").resolve(strict=False),
            ),
        )

    def test_trajectory_message_is_sent_to_its_embedded_chat_route(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread=THREAD,
            timestamp="2026-08-05T12:00:00Z",
            text="completed answer",
            delivery_id="delivery-id",
        )
        with (
            patch.object(app_module.target_store, "get") as get_target,
            patch.object(
                app_module.gateway,
                "send_followup",
                return_value=True,
            ) as send,
        ):
            delivered = app_module._deliver_session_message(message)

        self.assertTrue(delivered)
        get_target.assert_not_called()
        send.assert_called_once_with(
            SPACE,
            THREAD,
            "completed answer",
            "openclaw",
            request_id="delivery-id",
        )

    def test_dm_trajectory_message_keeps_an_empty_reply_thread(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread="",
            timestamp=None,
            text="root answer",
            delivery_id="root-delivery-id",
        )
        with patch.object(
            app_module.gateway,
            "send_followup",
            return_value=True,
        ) as send:
            delivered = app_module._deliver_session_message(message)

        self.assertTrue(delivered)
        send.assert_called_once_with(
            SPACE,
            "",
            "root answer",
            "openclaw",
            request_id="root-delivery-id",
        )

    def test_trajectory_media_is_staged_after_stripped_text_is_sent(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        events: list[str] = []
        attachment_out = Mock()
        attachment_out.submit_explicit.side_effect = lambda *_args, **_kwargs: (
            events.append("media")
            or AttachmentSubmissionResult(AttachmentSubmissionDisposition.ACCEPTED)
        )
        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread=THREAD,
            timestamp=None,
            text="เสร็จแล้วครับ",
            delivery_id="delivery-id",
            media_paths=("/allowed/spider cat.png", "/allowed/second.png"),
        )

        with (
            patch.object(
                app_module.gateway,
                "send_followup",
                side_effect=lambda *_args, **_kwargs: events.append("text") or True,
            ) as send,
            patch.object(
                app_module,
                "outbound_attachment_service",
                attachment_out,
            ),
        ):
            delivered = app_module._deliver_session_message(message)

        self.assertTrue(delivered)
        self.assertEqual(events, ["text", "media", "media"])
        self.assertEqual(send.call_args.args[2], "เสร็จแล้วครับ")
        self.assertEqual(
            [call.args[0] for call in attachment_out.submit_explicit.call_args_list],
            ["/allowed/spider cat.png", "/allowed/second.png"],
        )
        self.assertEqual(
            [
                call.kwargs["idempotency_key"]
                for call in attachment_out.submit_explicit.call_args_list
            ],
            [
                "jinx-session-media:delivery-id:0",
                "jinx-session-media:delivery-id:1",
            ],
        )
        for media_call in attachment_out.submit_explicit.call_args_list:
            self.assertEqual(media_call.kwargs["destination_space"], SPACE)
            self.assertEqual(media_call.kwargs["destination_thread"], THREAD)

    def test_dm_trajectory_media_keeps_an_empty_reply_thread(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        attachment_out = Mock()
        attachment_out.submit_explicit.return_value = AttachmentSubmissionResult(
            AttachmentSubmissionDisposition.ACCEPTED
        )
        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread="",
            timestamp=None,
            text="",
            delivery_id="root-media",
            media_paths=("/allowed/root.png",),
        )

        with patch.object(
            app_module,
            "outbound_attachment_service",
            attachment_out,
        ):
            delivered = app_module._deliver_session_message(message)

        self.assertTrue(delivered)
        attachment_out.submit_explicit.assert_called_once_with(
            "/allowed/root.png",
            idempotency_key="jinx-session-media:root-media:0",
            destination_space=SPACE,
            destination_thread="",
        )

    def test_media_only_message_does_not_send_blank_chat_text(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        attachment_out = Mock()
        attachment_out.submit_explicit.return_value = AttachmentSubmissionResult(
            AttachmentSubmissionDisposition.ACCEPTED
        )
        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread=THREAD,
            timestamp=None,
            text="",
            delivery_id="media-only",
            media_paths=("/allowed/image.png",),
        )

        with (
            patch.object(app_module.gateway, "send_followup") as send,
            patch.object(
                app_module,
                "outbound_attachment_service",
                attachment_out,
            ),
        ):
            delivered = app_module._deliver_session_message(message)

        self.assertTrue(delivered)
        send.assert_not_called()
        attachment_out.submit_explicit.assert_called_once()

    def test_rejected_media_path_sends_safe_failure_without_leaking_path(
        self,
    ) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        attachment_out = Mock()
        attachment_out.submit_explicit.return_value = AttachmentSubmissionResult(
            AttachmentSubmissionDisposition.REJECTED,
            "path_not_allowed",
        )
        local_path = "/private/secret/image.png"
        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread=THREAD,
            timestamp=None,
            text="",
            delivery_id="rejected-media",
            media_paths=(local_path,),
        )

        with (
            patch.object(
                app_module.gateway,
                "send_followup",
                return_value=True,
            ) as send,
            patch.object(
                app_module,
                "outbound_attachment_service",
                attachment_out,
            ),
        ):
            delivered = app_module._deliver_session_message(message)

        self.assertTrue(delivered)
        send.assert_called_once()
        self.assertNotIn(local_path, send.call_args.args[2])
        self.assertEqual(send.call_args.args[3], "jinx_system")
        self.assertRegex(
            send.call_args.kwargs["request_id"],
            r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
        )

    def test_transient_media_ingress_failure_retries_trajectory(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        attachment_out = Mock()
        attachment_out.submit_explicit.return_value = AttachmentSubmissionResult(
            AttachmentSubmissionDisposition.UNAVAILABLE,
            "database_busy",
        )
        message = AssistantTrajectoryMessage(
            session_key=SESSION_KEY,
            space=SPACE,
            reply_thread=THREAD,
            timestamp=None,
            text="completed answer",
            delivery_id="retry-id",
            media_paths=("/allowed/image.png",),
        )

        with (
            patch.object(app_module.gateway, "send_followup", return_value=True),
            patch.object(
                app_module,
                "outbound_attachment_service",
                attachment_out,
            ),
        ):
            delivered = app_module._deliver_session_message(message)

        self.assertFalse(delivered)

    def test_chat_route_forwards_every_attachment_for_orchestrator_accounting(
        self,
    ) -> None:
        # app has a module-level download directory creation. It is irrelevant to
        # this request-boundary test and is suppressed to keep the test hermetic.
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        attachments = [{"contentName": f"file-{index}.txt"} for index in range(10)]
        payload = {
            "message": {
                "name": "spaces/one/messages/request-one",
                "text": "inspect",
                "threadReply": False,
                "attachment": attachments,
                "space": {"name": SPACE, "spaceType": "SPACE"},
                "thread": {"name": THREAD},
                "sender": {
                    "name": "users/alice",
                    "displayName": "Alice",
                    "email": "alice@gmail.com",
                },
            }
        }
        events: list[str] = []

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(
                app_module.target_store,
                "remember",
                side_effect=lambda *_args: events.append("remember"),
            ) as remember,
            patch.object(
                app_module.orchestrator,
                "dispatch",
                side_effect=lambda *_args, **_kwargs: events.append("dispatch"),
            ) as dispatch,
            patch.object(
                app_module,
                "_start_outbound_attachment_service",
                return_value=True,
            ) as start_watcher,
        ):
            response = app_module.app.test_client().post(
                "/chat",
                data=json.dumps(payload),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        remember.assert_called_once_with(SPACE, THREAD)
        start_watcher.assert_called_once_with()
        dispatch.assert_called_once()
        self.assertEqual(events, ["remember", "dispatch"])
        self.assertEqual(dispatch.call_args.args[4], attachments)
        self.assertEqual(
            dispatch.call_args.kwargs["command_id"],
            "spaces/one/messages/request-one",
        )
        context = dispatch.call_args.kwargs["context"]
        self.assertEqual(context.space, SPACE)
        self.assertEqual(context.thread, THREAD)
        self.assertEqual(context.reply_thread, THREAD)
        self.assertEqual(context.session_key, SESSION_KEY)
        self.assertFalse(context.is_direct_message)
        self.assertEqual(dispatch.call_args.args[:2], (SPACE, THREAD))

    def test_explicit_classic_non_message_events_are_acknowledged_only(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        for event_type in (
            "ADDED_TO_SPACE",
            "REMOVED_FROM_SPACE",
            "CARD_CLICKED",
            "WIDGET_UPDATED",
            "APP_COMMAND",
        ):
            with (
                self.subTest(event_type=event_type),
                patch.object(
                    app_module.auth_verifier, "verify", return_value=True
                ),
                patch.object(app_module.gateway, "record_incoming"),
                patch.object(app_module.gateway, "ack", return_value={}) as ack,
                patch.object(app_module.target_store, "remember") as remember,
                patch.object(
                    app_module, "_start_outbound_attachment_service"
                ) as start_watcher,
                patch.object(app_module.gateway, "send_followup") as notify,
                patch.object(app_module.orchestrator, "dispatch") as dispatch,
            ):
                response = app_module.app.test_client().post(
                    "/chat",
                    json={
                        "type": event_type,
                        "space": {"name": SPACE, "spaceType": "SPACE"},
                        # Even a lifecycle event carrying message context must not
                        # be mistaken for a user turn.
                        "message": {
                            "text": "must not dispatch",
                            "space": {"name": SPACE},
                            "thread": {"name": THREAD},
                        },
                    },
                )

            self.assertEqual(response.status_code, 200)
            ack.assert_called_once_with()
            remember.assert_not_called()
            start_watcher.assert_not_called()
            notify.assert_not_called()
            dispatch.assert_not_called()

    def test_addon_non_message_union_payloads_are_acknowledged_only(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        for payload_key in (
            "addedToSpacePayload",
            "removedFromSpacePayload",
            "buttonClickedPayload",
            "widgetUpdatedPayload",
            "appCommandPayload",
        ):
            with (
                self.subTest(payload_key=payload_key),
                patch.object(
                    app_module.auth_verifier, "verify", return_value=True
                ),
                patch.object(app_module.gateway, "record_incoming"),
                patch.object(app_module.gateway, "ack", return_value={}) as ack,
                patch.object(app_module.target_store, "remember") as remember,
                patch.object(
                    app_module, "_start_outbound_attachment_service"
                ) as start_watcher,
                patch.object(app_module.gateway, "send_followup") as notify,
                patch.object(app_module.orchestrator, "dispatch") as dispatch,
            ):
                response = app_module.app.test_client().post(
                    "/chat",
                    json={
                        "chat": {
                            "space": {"name": SPACE, "spaceType": "SPACE"},
                            payload_key: {
                                "space": {"name": SPACE, "spaceType": "SPACE"},
                                "message": {
                                    "text": "must not dispatch",
                                    "thread": {"name": THREAD},
                                },
                            },
                        },
                        # A conflicting legacy-looking message must not override
                        # the add-on union discriminator.
                        "message": {
                            "text": "must still not dispatch",
                            "space": {"name": SPACE},
                            "thread": {"name": THREAD},
                        },
                    },
                )

            self.assertEqual(response.status_code, 200)
            ack.assert_called_once_with()
            remember.assert_not_called()
            start_watcher.assert_not_called()
            notify.assert_not_called()
            dispatch.assert_not_called()

    def test_chat_route_derives_context_for_dm_and_thread_reply(self) -> None:
        import app as app_module

        cases = (
            (
                "direct_message_keeps_thread_identity_but_replies_flat",
                {
                    "chat": {
                        "space": {
                            "name": SPACE,
                            "spaceType": "DIRECT_MESSAGE",
                        },
                        "messagePayload": {
                            "space": {
                                "name": SPACE,
                                "spaceType": "DIRECT_MESSAGE",
                            },
                            "message": {
                                "name": "spaces/one/messages/dm-one",
                                "text": "hello",
                                "threadReply": False,
                                "thread": {"name": THREAD},
                            },
                        },
                    }
                },
                "",
                SESSION_KEY,
                True,
            ),
            (
                "named_space_thread_reply",
                {
                    "type": "MESSAGE",
                    "space": {"name": SPACE, "spaceType": "SPACE"},
                    "message": {
                        "name": "spaces/one/messages/reply-one",
                        "text": "reply",
                        "threadReply": True,
                        "thread": {"name": THREAD},
                    },
                },
                THREAD,
                "agent:main:gchat:one:two",
                False,
            ),
            (
                "legacy_dm_type_keeps_thread_identity_but_replies_flat",
                {
                    "space": {"name": SPACE, "type": "DM"},
                    "message": {
                        "name": "spaces/one/messages/legacy-dm-one",
                        "text": "hello",
                        "threadReply": True,
                        "thread": {"name": THREAD},
                    },
                },
                "",
                SESSION_KEY,
                True,
            ),
            (
                "single_user_bot_dm_keeps_thread_identity_but_replies_flat",
                {
                    "space": {
                        "name": SPACE,
                        "spaceType": "SPACE",
                        "singleUserBotDm": True,
                    },
                    "message": {
                        "name": "spaces/one/messages/single-user-dm",
                        "text": "hello",
                        "threadReply": True,
                        "thread": {"name": THREAD},
                    },
                },
                "",
                SESSION_KEY,
                True,
            ),
        )

        for (
            label,
            payload,
            expected_reply_thread,
            expected_key,
            expected_is_direct_message,
        ) in cases:
            with self.subTest(label=label):
                with (
                    patch.object(
                        app_module.auth_verifier, "verify", return_value=True
                    ),
                    patch.object(app_module.gateway, "record_incoming"),
                    patch.object(app_module.gateway, "ack", return_value={}),
                    patch.object(app_module.target_store, "remember"),
                    patch.object(
                        app_module,
                        "_start_outbound_attachment_service",
                        return_value=True,
                    ),
                    patch.object(app_module.orchestrator, "dispatch") as dispatch,
                ):
                    response = app_module.app.test_client().post(
                        "/chat",
                        json=payload,
                    )

                self.assertEqual(response.status_code, 200)
                dispatch.assert_called_once()
                context = dispatch.call_args.kwargs["context"]
                self.assertEqual(context.space, SPACE)
                self.assertEqual(context.thread, THREAD)
                self.assertEqual(context.reply_thread, expected_reply_thread)
                self.assertEqual(context.session_key, expected_key)
                self.assertEqual(
                    context.is_direct_message,
                    expected_is_direct_message,
                )
                self.assertEqual(dispatch.call_args.args[:2], (SPACE, THREAD))

    def test_message_without_full_thread_name_is_rejected_before_dispatch(
        self,
    ) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(
                app_module, "_start_outbound_attachment_service"
            ) as start_watcher,
            patch.object(app_module.gateway, "send_followup") as notify,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = app_module.app.test_client().post(
                "/chat",
                json={
                    "type": "MESSAGE",
                    "space": {"name": SPACE, "spaceType": "SPACE"},
                    "message": {
                        "name": "spaces/one/messages/missing-thread",
                        "text": "must not dispatch",
                        "threadReply": False,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        remember.assert_not_called()
        start_watcher.assert_not_called()
        dispatch.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[:2], (SPACE, ""))

    def test_cross_space_reply_thread_is_rejected_before_dispatch(self) -> None:
        import app as app_module

        cross_space_thread = "spaces/other/threads/two"
        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(
                app_module, "_start_outbound_attachment_service"
            ) as start_watcher,
            patch.object(app_module.gateway, "send_followup") as notify,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = app_module.app.test_client().post(
                "/chat",
                json={
                    "space": {"name": SPACE, "spaceType": "SPACE"},
                    "message": {
                        "text": "reply",
                        "threadReply": True,
                        "thread": {"name": cross_space_thread},
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        remember.assert_not_called()
        start_watcher.assert_not_called()
        dispatch.assert_not_called()
        notify.assert_called_once()

    def test_cross_space_root_or_dm_thread_is_rejected_before_dispatch(self) -> None:
        with patch("pathlib.Path.mkdir"):
            import app as app_module

        cross_space_thread = "spaces/other/threads/two"
        for label, space_details, thread_reply in (
            ("top_level", {"name": SPACE, "spaceType": "SPACE"}, False),
            (
                "direct_message",
                {"name": SPACE, "spaceType": "DIRECT_MESSAGE"},
                True,
            ),
        ):
            with (
                self.subTest(label=label),
                patch.object(
                    app_module.auth_verifier, "verify", return_value=True
                ),
                patch.object(app_module.gateway, "record_incoming"),
                patch.object(app_module.gateway, "ack", return_value={}),
                patch.object(app_module.target_store, "remember") as remember,
                patch.object(
                    app_module, "_start_outbound_attachment_service"
                ) as start_watcher,
                patch.object(app_module.gateway, "send_followup") as notify,
                patch.object(app_module.orchestrator, "dispatch") as dispatch,
            ):
                response = app_module.app.test_client().post(
                    "/chat",
                    json={
                        "space": space_details,
                        "message": {
                            "text": "must not dispatch",
                            "threadReply": thread_reply,
                            "thread": {"name": cross_space_thread},
                        },
                    },
                )

            self.assertEqual(response.status_code, 200)
            remember.assert_not_called()
            start_watcher.assert_not_called()
            dispatch.assert_not_called()
            notify.assert_called_once()

    def test_unauthorized_request_cannot_claim_the_outbound_target(self) -> None:
        import app as app_module

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=False),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.target_store, "remember") as remember,
        ):
            response = app_module.app.test_client().post(
                "/chat",
                json={
                    "message": {
                        "text": "hello",
                        "space": {"name": SPACE},
                        "thread": {"name": THREAD},
                    }
                },
            )

        self.assertEqual(response.status_code, 401)
        remember.assert_not_called()

    def test_conflicting_file_fallback_space_does_not_block_session_dispatch(
        self,
    ) -> None:
        import app as app_module

        conflicting_space = "spaces/other"
        conflicting_thread = "spaces/other/threads/new"
        conflict = ChatTargetConflictError(
            ChatTarget(SPACE, THREAD),
            ChatTarget(conflicting_space, conflicting_thread),
        )
        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(
                app_module.target_store, "remember", side_effect=conflict
            ) as remember,
            patch.object(
                app_module,
                "_start_outbound_attachment_service",
                return_value=True,
            ),
            patch.object(app_module.gateway, "send_followup") as notify,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = app_module.app.test_client().post(
                "/chat",
                json={
                    "message": {
                        "text": "generate a file",
                        "space": {"name": conflicting_space},
                        "thread": {"name": conflicting_thread},
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        remember.assert_called_once_with(conflicting_space, conflicting_thread)
        dispatch.assert_called_once()
        notify.assert_not_called()
        context = dispatch.call_args.kwargs["context"]
        self.assertEqual(context.space, conflicting_space)
        self.assertEqual(context.thread, conflicting_thread)
        self.assertEqual(context.reply_thread, conflicting_thread)
        self.assertEqual(context.session_key, "agent:main:gchat:other:new")
        self.assertEqual(
            dispatch.call_args.args[:2],
            (conflicting_space, conflicting_thread),
        )

    def test_same_space_thread_is_accepted_without_changing_file_target(self) -> None:
        import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            store = FixedChatTargetStore(Path(directory) / "target.json")
            fixed = store.remember(SPACE, THREAD)
            incoming_thread = "spaces/one/threads/other"
            with (
                patch.object(app_module.auth_verifier, "verify", return_value=True),
                patch.object(app_module.gateway, "record_incoming"),
                patch.object(app_module.gateway, "ack", return_value={}),
                patch.object(app_module, "target_store", store),
                patch.object(
                    app_module, "_start_outbound_attachment_service", return_value=True
                ),
                patch.object(app_module.orchestrator, "dispatch") as dispatch,
            ):
                response = app_module.app.test_client().post(
                    "/chat",
                    json={
                        "message": {
                            "text": "generate a file",
                            "space": {"name": SPACE},
                            "thread": {"name": incoming_thread},
                        }
                    },
                )

            self.assertEqual(response.status_code, 200)
            dispatch.assert_called_once()
            self.assertEqual(store.get(), fixed)
            context = dispatch.call_args.kwargs["context"]
            self.assertEqual(context.thread, incoming_thread)
            self.assertEqual(context.reply_thread, incoming_thread)
            self.assertEqual(context.session_key, "agent:main:gchat:one:other")
            self.assertEqual(dispatch.call_args.args[:2], (SPACE, incoming_thread))

    def test_watcher_start_failure_notifies_but_does_not_block_provider(self) -> None:
        import app as app_module

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(app_module.target_store, "remember"),
            patch.object(
                app_module, "_start_outbound_attachment_service", return_value=False
            ),
            patch.object(
                app_module,
                "outbound_attachment_service",
                SimpleNamespace(last_start_error=RuntimeError("inotify unavailable")),
            ),
            patch.object(app_module.gateway, "send_followup") as notify,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = app_module.app.test_client().post(
                "/chat",
                json={
                    "message": {
                        "text": "generate a file",
                        "space": {"name": SPACE},
                        "thread": {"name": THREAD},
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once()
        context = dispatch.call_args.kwargs["context"]
        self.assertEqual(context.thread, THREAD)
        self.assertEqual(context.reply_thread, THREAD)
        self.assertEqual(context.session_key, SESSION_KEY)
        self.assertEqual(notify.call_args.args[:2], (SPACE, THREAD))
        self.assertEqual(notify.call_args.args[3], "jinx_system")
        self.assertIn("ระบบตรวจจับไฟล์", notify.call_args.args[2])

    def test_watcher_startup_setup_is_cached_after_the_first_call(self) -> None:
        import app as app_module

        service = Mock()
        service.wait_until_active.return_value = True
        service.is_active = True
        service.last_start_error = None

        with (
            patch.object(app_module, "outbound_attachment_service", service),
            patch.object(app_module, "_outbound_start_initialized", False),
            patch.object(app_module, "_outbound_start_error", None),
        ):
            self.assertTrue(app_module._start_outbound_attachment_service())
            self.assertTrue(app_module._start_outbound_attachment_service())

        service.start.assert_called_once_with()
        service.wait_until_active.assert_called_once_with(timeout=5)


class AttachmentCleanupTests(unittest.TestCase):
    def test_cleanup_deletes_each_unique_download_and_reports_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "download.txt"
            path.write_text("temporary", encoding="utf-8")
            items = [
                {"fp": str(path), "meta": {"contentName": "download.txt"}},
                {"fp": str(path), "meta": {"contentName": "duplicate.txt"}},
                {"fp": None, "meta": {"contentName": "failed.txt"}},
            ]

            report = AttachmentService.cleanup(items)

            self.assertFalse(path.exists())
            self.assertEqual(report, {"removed": 1, "failed": []})

    def test_cleanup_returns_failures_instead_of_silently_swallowing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locked.txt"
            path.write_text("temporary", encoding="utf-8")

            with patch(
                "helpers.services.chat_services.Path.unlink",
                side_effect=PermissionError("permission denied"),
            ):
                report = AttachmentService.cleanup([{"fp": str(path), "meta": {}}])

            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["failed"][0]["name"], "locked.txt")
            self.assertEqual(report["failed"][0]["error"], "permission denied")

    def test_cleanup_retries_partial_downloads_recorded_by_cleanup_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.bin"
            path.write_bytes(b"partial")

            report = AttachmentService.cleanup(
                [
                    {
                        "fp": None,
                        "meta": {
                            "contentName": "partial.bin",
                            "cleanupPath": str(path),
                            "errorCode": "download_failed",
                        },
                    }
                ]
            )

            self.assertFalse(path.exists())
            self.assertEqual(report, {"removed": 1, "failed": []})

    def test_declared_oversize_is_rejected_without_creating_a_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AttachmentService(
                download_dir=Path(directory),
                max_attachment_bytes=10,
                credential_service=Mock(),
            )

            result = service._download_one(
                {
                    "contentName": "large.bin",
                    "contentType": "application/octet-stream",
                    "size": 11,
                    "uri": "https://example.invalid/large.bin",
                },
                None,
                None,
            )

            self.assertIsNone(result["fp"])
            self.assertEqual(result["meta"]["errorCode"], "too_large")
            self.assertEqual(list(Path(directory).iterdir()), [])


class ProviderLocalFileAccessTests(unittest.TestCase):
    def client(self, endpoint: str) -> OpenClawClient:
        return OpenClawClient(
            agent="main",
            base_url=endpoint,
            model="openclaw/default",
        )

    def test_auto_policy_allows_only_explicit_loopback_hosts(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS": "auto"},
            clear=False,
        ):
            self.assertTrue(
                self.client("http://127.0.0.1:18789/v1").has_local_file_access()
            )
            self.assertTrue(
                self.client("http://[::1]:18789/v1").has_local_file_access()
            )
            self.assertFalse(
                self.client("https://gateway.example/v1").has_local_file_access()
            )

    def test_explicit_policy_can_allow_a_shared_mount_or_deny_loopback(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS": "allow"},
            clear=False,
        ):
            self.assertTrue(
                self.client("https://gateway.example/v1").has_local_file_access()
            )

        with patch.dict(
            "os.environ",
            {"OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS": "deny"},
            clear=False,
        ):
            self.assertFalse(
                self.client("http://127.0.0.1:18789/v1").has_local_file_access()
            )

    def test_invalid_policy_is_rejected(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"OPENCLAW_GATEWAY_LOCAL_FILE_ACCESS": "sometimes"},
                clear=False,
            ),
            self.assertRaisesRegex(ValueError, "must be one of"),
        ):
            self.client("http://127.0.0.1:18789/v1").has_local_file_access()


class AttachmentPromptTests(unittest.TestCase):
    def test_provider_slash_command_keeps_downloaded_attachment_context(self) -> None:
        prompt = build_openclaw_prompt(
            "/help",
            "Alice",
            [
                {
                    "fp": "/home/arme/.openclaw/workspace/downloads/help.txt",
                    "meta": {
                        "contentName": "help.txt",
                        "contentType": "text/plain",
                        "savedSize": 12,
                    },
                }
            ],
        )

        self.assertIn("[FILE_META]", prompt)
        self.assertIn("/home/arme/.openclaw/workspace/downloads/help.txt", prompt)
        self.assertIn("Alice: /help", prompt)


if __name__ == "__main__":
    unittest.main()
