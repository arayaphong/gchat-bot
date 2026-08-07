from __future__ import annotations

import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import BUSY_TEXT
from helpers.providers import OpenClawClient, ProviderSettings
from helpers.providers.model_selection import ModelSelection


class NewSessionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current_settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:c0ffee",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )
        self.new_settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:decade",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )
        self.gateway = Mock()
        self.session_manager = Mock()
        self.session_manager.settings = self.current_settings
        self.session_manager.abort_current.return_value = (True, "aborted")
        self.session_manager.rotate_with_model.return_value = self.new_settings
        self.session_watcher = Mock()
        self.attachment_service = Mock()
        self.attachment_service.cleanup.return_value = {
            "removed": 0,
            "failed": [],
        }
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

    def test_new_is_rejected_while_an_attachment_download_is_in_flight(self) -> None:
        download_started = threading.Event()
        release_download = threading.Event()
        downloaded = [
            {
                "fp": "/tmp/report.pdf",
                "meta": {"contentName": "report.pdf"},
            }
        ]

        def blocked_download(_attachments: object) -> list[dict[str, object]]:
            download_started.set()
            if not release_download.wait(timeout=2):
                raise TimeoutError("test did not release the attachment download")
            return downloaded

        self.attachment_service.download_with_meta.side_effect = blocked_download
        selection = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )
        self.openclaw_client.get_model_selection.return_value = selection

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
                "spaces/one",
                "threads/two",
                "Alice",
                "inspect",
                [{"contentName": "report.pdf"}],
            )
            self.assertTrue(download_started.wait(timeout=1))

            try:
                self.orchestrator.dispatch(
                    "spaces/one",
                    "threads/two",
                    "Alice",
                    "/new",
                    [],
                )
                for worker in started_threads[1:]:
                    worker.join(timeout=1)

                self.gateway.send_followup.assert_called_once_with(
                    "spaces/one",
                    "threads/two",
                    BUSY_TEXT,
                    "jinx_system",
                )
                self.openclaw_client.get_model_selection.assert_not_called()
                self.session_manager.abort_current.assert_not_called()
                self.session_manager.rotate_with_model.assert_not_called()
            finally:
                release_download.set()
                for worker in started_threads:
                    worker.join(timeout=2)

        self.assertEqual(len(started_threads), 1)
        self.assertFalse(started_threads[0].is_alive())
        self.openclaw_client.send_turn.assert_called_once_with(
            "inspect",
            "Alice",
            downloaded,
            "agent:main:gchat:c0ffee",
            None,
        )

    def test_idle_new_ignores_attachments_while_holding_the_gate(self) -> None:
        selection = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        def select_model(_session_key: str) -> ModelSelection:
            self.assertTrue(self.orchestrator.is_processing)
            return selection

        self.openclaw_client.get_model_selection.side_effect = select_model

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
                "spaces/one",
                "threads/two",
                "Alice",
                "/new",
                [{"contentName": "ignored.pdf"}],
            )

        self.assertFalse(self.orchestrator.is_processing)
        self.attachment_service.download_with_meta.assert_not_called()
        self.openclaw_client.send_turn.assert_not_called()
        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/current"
        )
        self.assertEqual(len(self.gateway.send_followup.call_args_list), 2)
        ignored_notice, success_notice = self.gateway.send_followup.call_args_list
        self.assertIn("/new", ignored_notice.args[2])
        self.assertIn("ignored.pdf", ignored_notice.args[2])
        self.assertIn("เริ่มเซสชั่นใหม่", success_notice.args[2])

    def test_new_creates_the_session_with_the_current_session_model(self) -> None:
        selection = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        self.openclaw_client.get_model_selection.return_value = selection
        self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.openclaw_client.get_model_selection.assert_called_once_with(
            "agent:main:gchat:c0ffee"
        )
        self.session_manager.abort_current.assert_called_once_with(
            "spaces/one", "threads/two"
        )
        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/current"
        )
        self.session_manager.rotate.assert_not_called()
        self.session_watcher.start.assert_called_once_with()
        self.session_watcher.prepare_session.assert_called_once_with(
            "agent:main:gchat:decade"
        )
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn("provider/current", message)
        self.assertEqual(self.gateway.send_followup.call_args.args[3], "jinx_system")

    def test_new_uses_the_effective_default_when_session_has_no_override(self) -> None:
        selection = ModelSelection(
            default_model="provider/default",
            session_model=None,
        )

        self.openclaw_client.get_model_selection.return_value = selection
        self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/default"
        )

    def test_model_lookup_failure_does_not_abort_or_rotate_the_session(self) -> None:
        self.openclaw_client.get_model_selection.side_effect = (
            subprocess.TimeoutExpired(["openclaw"], 15)
        )
        self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.session_manager.abort_current.assert_not_called()
        self.session_manager.rotate_with_model.assert_not_called()
        self.session_manager.rotate.assert_not_called()
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn("ไม่สามารถเริ่มเซสชั่นใหม่", message)
        self.assertIn("เซสชั่นเดิมยังคงใช้งานอยู่", message)

    def test_create_failure_never_falls_back_to_an_unmodeled_rotation(self) -> None:
        self.session_manager.rotate_with_model.side_effect = RuntimeError(
            "gateway rejected *model*"
        )
        selection = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        self.openclaw_client.get_model_selection.return_value = selection
        self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.session_manager.abort_current.assert_called_once()
        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/current"
        )
        self.session_manager.rotate.assert_not_called()
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn(r"gateway rejected \*model\*", message)
        self.assertIn("เซสชั่นเดิมยังคงใช้งานอยู่", message)


if __name__ == "__main__":
    unittest.main()
