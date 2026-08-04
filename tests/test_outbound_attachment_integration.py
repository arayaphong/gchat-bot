from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers.chat_gateway import FileDeliveryResult
from helpers.chat_target_store import ChatTarget
from helpers.outbound_attachment_watcher import (
    DeliveryDisposition,
    FinalDeliveryFailure,
    OutboundAttachment,
    OutboundDeliveryResult,
)
from helpers.processing_gate import ProcessingGate

with patch("pathlib.Path.mkdir"):
    import app as app_module

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"


class OutboundAttachmentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.staged_path = Path(temporary_directory.name) / "report.pdf"
        self.staged_path.write_bytes(b"report")
        self.processing_lock_file = Path(temporary_directory.name) / "processing.lock"
        self.processing_gate = ProcessingGate(self.processing_lock_file)
        self.attachment = OutboundAttachment(
            source_path=Path("/home/arme/.openclaw/workspace/uploads/report.pdf"),
            source_root=Path("/home/arme/.openclaw/workspace/uploads"),
            staged_path=self.staged_path,
            display_name="report.pdf",
            size=self.staged_path.stat().st_size,
            sha256="sha256",
            delivery_id="c0295845-822c-5436-80ef-c53558e5546d",
        )

    def test_delivery_waits_for_the_model_reply_and_for_a_chat_target(self) -> None:
        busy_lease = self.processing_gate.try_acquire()
        self.assertIsNotNone(busy_lease)
        with patch.object(app_module, "processing_gate", self.processing_gate):
            self.assertIs(
                app_module._deliver_outbound_attachment(self.attachment),
                DeliveryDisposition.DEFERRED,
            )
        assert busy_lease is not None
        busy_lease.release()

        with (
            patch.object(app_module, "processing_gate", self.processing_gate),
            patch.object(app_module.target_store, "get", return_value=None),
            patch.object(app_module.gateway, "send_file") as send_file,
        ):
            self.assertIs(
                app_module._deliver_outbound_attachment(self.attachment),
                DeliveryDisposition.DEFERRED,
            )
        send_file.assert_not_called()

    def test_delivery_uses_staged_file_and_fixed_target(self) -> None:
        result = FileDeliveryResult(
            file_path=self.staged_path,
            display_name="report.pdf",
        )
        peer_gate = ProcessingGate(self.processing_lock_file)

        def send_file(*_args: object, **_kwargs: object) -> FileDeliveryResult:
            self.assertIsNone(peer_gate.try_acquire())
            return result

        with (
            patch.object(app_module, "processing_gate", self.processing_gate),
            patch.object(
                app_module.target_store,
                "get",
                return_value=ChatTarget(SPACE, THREAD),
            ),
            patch.object(
                app_module.gateway, "send_file", side_effect=send_file
            ) as send_file_mock,
        ):
            disposition = app_module._deliver_outbound_attachment(self.attachment)

        self.assertEqual(
            disposition,
            OutboundDeliveryResult(DeliveryDisposition.DELIVERED),
        )
        send_file_mock.assert_called_once_with(
            SPACE,
            THREAD,
            self.staged_path,
            filename="report.pdf",
            request_id=self.attachment.delivery_id,
            drive_file_id="",
            web_view_link="",
        )

    def test_gateway_failure_is_returned_to_the_retry_worker(self) -> None:
        result = FileDeliveryResult(
            file_path=self.staged_path,
            display_name="report.pdf",
            error=TimeoutError("Drive timed out"),
        )
        with (
            patch.object(app_module, "processing_gate", self.processing_gate),
            patch.object(
                app_module.target_store,
                "get",
                return_value=ChatTarget(SPACE, THREAD),
            ),
            patch.object(app_module.gateway, "send_file", return_value=result),
        ):
            disposition = app_module._deliver_outbound_attachment(self.attachment)

        self.assertEqual(
            disposition,
            OutboundDeliveryResult(DeliveryDisposition.FAILED),
        )

    def test_final_failure_is_jinx_only_and_notification_failure_is_retryable(
        self,
    ) -> None:
        failure = FinalDeliveryFailure(
            attachment=self.attachment,
            attempts=3,
            error_category="delivery_failed",
        )
        peer_gate = ProcessingGate(self.processing_lock_file)

        def notify_failure(*_args: object, **_kwargs: object) -> bool:
            self.assertIsNone(peer_gate.try_acquire())
            return True

        with (
            patch.object(app_module, "processing_gate", self.processing_gate),
            patch.object(
                app_module.target_store,
                "get",
                return_value=ChatTarget(SPACE, THREAD),
            ),
            patch.object(
                app_module.gateway, "send_followup", side_effect=notify_failure
            ) as notify,
        ):
            app_module._notify_outbound_attachment_failure(failure)

        self.assertEqual(notify.call_args.args[0:2], (SPACE, THREAD))
        self.assertEqual(notify.call_args.args[3], "jinx_system")
        self.assertIn("report.pdf", notify.call_args.args[2])
        self.assertRegex(
            notify.call_args.kwargs["request_id"],
            r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
        )

        with (
            patch.object(app_module, "processing_gate", self.processing_gate),
            patch.object(
                app_module.target_store,
                "get",
                return_value=ChatTarget(SPACE, THREAD),
            ),
            patch.object(app_module.gateway, "send_followup", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            app_module._notify_outbound_attachment_failure(failure)

    def test_failure_notification_is_deferred_while_provider_lease_is_held(
        self,
    ) -> None:
        failure = FinalDeliveryFailure(
            attachment=self.attachment,
            attempts=0,
            error_category="empty_file",
        )
        provider_lease = self.processing_gate.try_acquire()
        self.assertIsNotNone(provider_lease)

        with (
            patch.object(app_module, "processing_gate", self.processing_gate),
            patch.object(app_module.gateway, "send_followup") as notify,
            self.assertRaisesRegex(RuntimeError, "gate is busy"),
        ):
            app_module._notify_outbound_attachment_failure(failure)

        notify.assert_not_called()
        assert provider_lease is not None
        provider_lease.release()


if __name__ == "__main__":
    unittest.main()
