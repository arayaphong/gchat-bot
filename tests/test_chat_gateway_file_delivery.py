from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.chat_gateway import (
    ChatGateway,
    ChatMessageResponseError,
    DriveUploadResponseError,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.services import CardPresenter

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"


class ChatGatewayFileDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.allowed_root = Path(temporary_directory.name)

        self.gateway = ChatGateway(
            credential_service=Mock(),
            card_presenter=CardPresenter(),
            chat_in_log=self.allowed_root / "chat-in.jsonl",
            chat_out_log=self.allowed_root / "chat-out.jsonl",
            file_policy=SendableFilePolicy([self.allowed_root]),
            drive_folder_id="drive-folder",
        )
        self.drive_service = Mock(name="drive_service")
        self.gateway._build_user_drive_service = Mock(  # type: ignore[method-assign]
            return_value=self.drive_service
        )
        self.gateway._upload_to_drive = Mock(  # type: ignore[method-assign]
            return_value={
                "id": "drive-file-1",
                "name": "report.pdf",
                "webViewLink": "https://drive.example/report",
                "thumbnailLink": "https://drive.example/report-thumbnail",
            }
        )
        self.gateway._post_message = Mock()  # type: ignore[method-assign]

    def make_file(self, name: str = "report.pdf") -> Path:
        path = self.allowed_root / name
        path.write_bytes(b"test file")
        return path

    def test_send_file_reports_success_only_after_preview_card_is_posted(self) -> None:
        path = self.make_file()

        result = self.gateway.send_file(
            SPACE,
            THREAD,
            path,
            filename="../safe-name.pdf",
            fallback_text="Here is the report.",
            request_id="file-delivery-1",
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.error)
        self.assertEqual(result.file_path, path.resolve())
        self.assertEqual(result.display_name, "safe-name.pdf")
        self.assertEqual(result.drive_file_id, "drive-file-1")
        self.assertEqual(result.web_view_link, "https://drive.example/report")
        self.gateway._upload_to_drive.assert_called_once_with(  # type: ignore[attr-defined]
            self.drive_service,
            path.resolve(),
            "safe-name.pdf",
        )

        posted_space, posted_thread, body = self.gateway._post_message.call_args.args  # type: ignore[attr-defined]
        self.assertEqual((posted_space, posted_thread), (SPACE, THREAD))
        self.assertEqual(
            self.gateway._post_message.call_args.kwargs,  # type: ignore[attr-defined]
            {"request_id": "file-delivery-1"},
        )
        self.assertEqual(body["text"], "Here is the report.")
        widgets = body["cardsV2"][0]["card"]["sections"][0]["widgets"]
        self.assertEqual(
            widgets[-1]["decoratedText"]["button"]["onClick"]["openLink"]["url"],
            "https://drive.example/report",
        )

    def test_send_followup_reports_whether_chat_accepted_the_message(self) -> None:
        self.assertTrue(
            self.gateway.send_followup(
                SPACE,
                THREAD,
                "notice",
                "jinx_system",
                request_id="failure-notice-1",
            )
        )
        self.assertEqual(
            self.gateway._post_message.call_args.kwargs,  # type: ignore[attr-defined]
            {"request_id": "failure-notice-1"},
        )

        self.gateway._post_message.side_effect = ConnectionError("offline")  # type: ignore[attr-defined]
        self.assertFalse(
            self.gateway.send_followup(SPACE, THREAD, "notice", "jinx_system")
        )

    def test_file_preview_card_maps_files_to_ordered_widget_groups(self) -> None:
        action = CardPresenter.build_file_preview_card(
            "",
            [
                {
                    "name": "<one&>.png",
                    "webViewLink": "https://drive.example/one",
                    "thumbnailLink": "https://drive.example/one-thumbnail",
                },
                {
                    "name": "two.pdf",
                    "webViewLink": "https://drive.example/two",
                },
            ],
        )

        message = action["hostAppDataAction"]["chatDataAction"]["createMessageAction"][
            "message"
        ]
        widgets = message["cardsV2"][0]["card"]["sections"][0]["widgets"]
        self.assertEqual(
            widgets,
            [
                {
                    "image": {
                        "imageUrl": "https://drive.example/one-thumbnail",
                        "onClick": {"openLink": {"url": "https://drive.example/one"}},
                    }
                },
                {
                    "decoratedText": {
                        "text": "📎 &lt;one&amp;&gt;.png",
                        "wrapText": True,
                        "button": {
                            "text": "เปิดไฟล์",
                            "onClick": {
                                "openLink": {"url": "https://drive.example/one"}
                            },
                        },
                    }
                },
                {
                    "decoratedText": {
                        "text": "📎 two.pdf",
                        "wrapText": True,
                        "button": {
                            "text": "เปิดไฟล์",
                            "onClick": {
                                "openLink": {"url": "https://drive.example/two"}
                            },
                        },
                    }
                },
            ],
        )

    def test_jinx_error_card_uses_error_icon_in_administrator_title(self) -> None:
        self.gateway.send_followup(
            SPACE,
            THREAD,
            "❌ เกิดข้อผิดพลาด",
            "jinx_system",
        )

        body = self.gateway._post_message.call_args.args[2]  # type: ignore[attr-defined]
        title = body["cardsV2"][0]["card"]["header"]["title"]
        self.assertEqual(title, "❌ ผู้ดูแลระบบ")

    def test_non_error_jinx_card_keeps_administrator_icon(self) -> None:
        self.gateway.send_followup(
            SPACE,
            THREAD,
            "ℹ️ ข้อมูลทั่วไป",
            "jinx_system",
        )

        body = self.gateway._post_message.call_args.args[2]  # type: ignore[attr-defined]
        title = body["cardsV2"][0]["card"]["header"]["title"]
        self.assertEqual(title, "🛠️ ผู้ดูแลระบบ")

    def test_post_message_uses_bot_token_and_google_request_id(self) -> None:
        self.gateway._credential_service.get_bot_token.return_value = "bot-token"
        self.gateway.record_outgoing = Mock()  # type: ignore[method-assign]
        response = Mock()
        response.json.return_value = {"name": f"{SPACE}/messages/created"}
        body = {"text": "hello"}

        with patch("helpers.chat_gateway.requests.post", return_value=response) as post:
            resource = ChatGateway._post_message(
                self.gateway,
                SPACE,
                THREAD,
                body,
                request_id="stable-message-1",
            )

        post.assert_called_once_with(
            f"https://chat.googleapis.com/v1/{SPACE}/messages",
            headers={
                "Authorization": "Bearer bot-token",
                "Content-Type": "application/json",
            },
            params={"requestId": "stable-message-1"},
            json={"text": "hello", "thread": {"name": THREAD}},
            timeout=15,
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(resource, {"name": f"{SPACE}/messages/created"})
        self.assertEqual(body, {"text": "hello"})

    def test_post_message_paces_normal_write_and_supports_custom_message_id(
        self,
    ) -> None:
        self.gateway._credential_service.get_bot_token.return_value = "bot-token"
        self.gateway.record_outgoing = Mock()  # type: ignore[method-assign]
        self.gateway._write_pacer = Mock()
        response = Mock()
        response.json.return_value = {"name": f"{SPACE}/messages/client-stats"}

        with patch("helpers.chat_gateway.requests.post", return_value=response) as post:
            resource = ChatGateway._post_message(
                self.gateway,
                SPACE,
                THREAD,
                {"cardsV2": []},
                message_id="client-jinx-hs-0123456789abcdef0123456789abcdef",
            )

        self.gateway._write_pacer.wait_for_turn.assert_called_once_with(SPACE)
        self.assertEqual(resource["name"], f"{SPACE}/messages/client-stats")
        self.assertEqual(
            post.call_args.kwargs["params"],
            {"messageId": "client-jinx-hs-0123456789abcdef0123456789abcdef"},
        )

    def test_post_message_rejects_missing_or_cross_space_response_name(self) -> None:
        self.gateway._credential_service.get_bot_token.return_value = "bot-token"
        self.gateway.record_outgoing = Mock()  # type: ignore[method-assign]
        for payload in ({}, {"name": "spaces/other/messages/created"}, []):
            with self.subTest(payload=payload):
                response = Mock()
                response.json.return_value = payload
                with (
                    patch("helpers.chat_gateway.requests.post", return_value=response),
                    self.assertRaises(ChatMessageResponseError),
                ):
                    ChatGateway._post_message(
                        self.gateway,
                        SPACE,
                        THREAD,
                        {"text": "hello"},
                    )

    def test_structured_card_returns_resource_without_changing_boolean_methods(
        self,
    ) -> None:
        resource = {"name": f"{SPACE}/messages/stats"}
        self.gateway._post_message.return_value = resource  # type: ignore[attr-defined]
        message = {"cardsV2": []}

        result = self.gateway.send_structured_card(
            SPACE,
            THREAD,
            message,
            message_id="client-jinx-hs-0123456789abcdef0123456789abcdef",
        )

        self.assertEqual(result, resource)
        self.gateway._post_message.assert_called_once_with(  # type: ignore[attr-defined]
            SPACE,
            THREAD,
            message,
            message_id="client-jinx-hs-0123456789abcdef0123456789abcdef",
        )

    def test_send_file_returns_original_upload_error_without_posting(self) -> None:
        path = self.make_file()
        upload_error = TimeoutError("Drive timed out")
        self.gateway._upload_to_drive.side_effect = upload_error  # type: ignore[attr-defined]

        result = self.gateway.send_file(
            SPACE, THREAD, path, request_id="stable-file-message"
        )

        self.assertFalse(result.success)
        self.assertIs(result.error, upload_error)
        self.gateway._post_message.assert_not_called()  # type: ignore[attr-defined]

    def test_send_file_returns_original_card_post_error_without_jinx_notice(
        self,
    ) -> None:
        path = self.make_file()
        post_error = ConnectionError("Google Chat unavailable")
        self.gateway._post_message.side_effect = post_error  # type: ignore[attr-defined]
        self.gateway.send_followup = Mock()  # type: ignore[method-assign]

        result = self.gateway.send_file(
            SPACE, THREAD, path, request_id="stable-file-message"
        )

        self.assertFalse(result.success)
        self.assertIs(result.error, post_error)
        self.assertEqual(result.drive_file_id, "drive-file-1")
        self.assertEqual(result.web_view_link, "https://drive.example/report")
        self.gateway._post_message.assert_called_once()  # type: ignore[attr-defined]
        self.assertEqual(
            self.gateway._post_message.call_args.kwargs,  # type: ignore[attr-defined]
            {"request_id": "stable-file-message"},
        )
        self.gateway.send_followup.assert_not_called()  # type: ignore[attr-defined]

    def test_send_file_reuses_drive_receipt_without_local_file_or_upload(self) -> None:
        missing_path = self.allowed_root / "already-uploaded.pdf"

        result = self.gateway.send_file(
            SPACE,
            THREAD,
            missing_path,
            filename="already-uploaded.pdf",
            request_id="stable-file-message",
            drive_file_id="existing-drive-file",
            web_view_link="  https://drive.example/existing  ",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.drive_file_id, "existing-drive-file")
        self.assertEqual(result.web_view_link, "https://drive.example/existing")
        self.gateway._build_user_drive_service.assert_not_called()  # type: ignore[attr-defined]
        self.gateway._upload_to_drive.assert_not_called()  # type: ignore[attr-defined]
        body = self.gateway._post_message.call_args.args[2]  # type: ignore[attr-defined]
        widgets = body["cardsV2"][0]["card"]["sections"][0]["widgets"]
        self.assertEqual(
            widgets[-1]["decoratedText"]["button"]["onClick"]["openLink"]["url"],
            "https://drive.example/existing",
        )
        self.assertEqual(
            self.gateway._post_message.call_args.kwargs,  # type: ignore[attr-defined]
            {"request_id": "stable-file-message"},
        )

    def test_reused_receipt_survives_another_chat_post_failure(self) -> None:
        missing_path = self.allowed_root / "already-uploaded.pdf"
        post_error = TimeoutError("ambiguous Chat response")
        self.gateway._post_message.side_effect = post_error  # type: ignore[attr-defined]

        result = self.gateway.send_file(
            SPACE,
            THREAD,
            missing_path,
            drive_file_id="existing-drive-file",
            web_view_link="https://drive.example/existing",
        )

        self.assertFalse(result.success)
        self.assertIs(result.error, post_error)
        self.assertEqual(result.drive_file_id, "existing-drive-file")
        self.assertEqual(result.web_view_link, "https://drive.example/existing")
        self.gateway._upload_to_drive.assert_not_called()  # type: ignore[attr-defined]

    def test_send_file_rejects_missing_non_file_and_disallowed_paths(self) -> None:
        missing = self.allowed_root / "missing.txt"
        missing_result = self.gateway.send_file(SPACE, THREAD, missing)
        self.assertIsInstance(missing_result.error, FileNotFoundError)

        directory_result = self.gateway.send_file(SPACE, THREAD, self.allowed_root)
        self.assertIsInstance(directory_result.error, IsADirectoryError)

        other_directory = tempfile.TemporaryDirectory()
        self.addCleanup(other_directory.cleanup)
        outside = Path(other_directory.name) / "outside.txt"
        outside.write_text("outside")
        outside_result = self.gateway.send_file(SPACE, THREAD, outside)
        self.assertIsInstance(outside_result.error, PermissionError)

        self.gateway._build_user_drive_service.assert_not_called()  # type: ignore[attr-defined]
        self.gateway._post_message.assert_not_called()  # type: ignore[attr-defined]

    def test_send_file_treats_missing_drive_link_as_failure(self) -> None:
        path = self.make_file()
        self.gateway._upload_to_drive.return_value = {  # type: ignore[attr-defined]
            "id": "orphaned-drive-file",
            "name": "report.pdf",
        }

        result = self.gateway.send_file(SPACE, THREAD, path)

        self.assertFalse(result.success)
        self.assertIsInstance(result.error, DriveUploadResponseError)
        self.assertEqual(result.drive_file_id, "orphaned-drive-file")
        self.gateway._post_message.assert_not_called()  # type: ignore[attr-defined]

    def test_send_file_derives_space_from_thread(self) -> None:
        path = self.make_file()

        result = self.gateway.send_file("", THREAD, path)

        self.assertTrue(result.success)
        self.assertEqual(
            self.gateway._post_message.call_args.args[:2],  # type: ignore[attr-defined]
            (SPACE, THREAD),
        )


if __name__ == "__main__":
    unittest.main()
