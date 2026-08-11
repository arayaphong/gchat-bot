from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.chat_gateway import (
    CHAT_API_BASE_URL,
    ChatApiResponseError,
    ChatGateway,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.services import CardPresenter


class ChatThreadCreationTests(unittest.TestCase):
    """Covers bot-auth root-message creation in an existing Space."""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.credentials = Mock()
        self.credentials.get_bot_token.return_value = "bot-token"
        self.gateway = ChatGateway(
            credential_service=self.credentials,
            card_presenter=CardPresenter(),
            chat_in_log=self.root / "chat-in.jsonl",
            chat_out_log=self.root / "chat-out.jsonl",
            file_policy=SendableFilePolicy([self.root]),
            drive_folder_id="drive-folder",
        )

    @staticmethod
    def _response(payload: object | None = None) -> Mock:
        response = Mock()
        response.json.return_value = payload
        return response

    def test_create_root_thread_posts_without_thread_and_returns_full_name(
        self,
    ) -> None:
        response = self._response(
            {
                "name": "spaces/current/messages/root",
                "thread": {"name": "spaces/current/threads/root-thread"},
            }
        )
        self.gateway.record_outgoing = Mock()  # type: ignore[method-assign]

        with patch(
            "helpers.chat_gateway.requests.post",
            return_value=response,
        ) as post:
            thread = self.gateway.create_root_thread(
                "spaces/current",
                "เริ่มเซสชั่นใหม่แล้ว",
                request_id="root-message-42",
            )

        self.assertEqual(thread, "spaces/current/threads/root-thread")
        posted_body = post.call_args.kwargs["json"]
        self.assertNotIn("thread", posted_body)
        self.assertIn("cardsV2", posted_body)
        post.assert_called_once_with(
            f"{CHAT_API_BASE_URL}/spaces/current/messages",
            headers={
                "Authorization": "Bearer bot-token",
                "Content-Type": "application/json",
            },
            params={"requestId": "root-message-42"},
            json=posted_body,
            timeout=15,
        )
        self.gateway.record_outgoing.assert_called_once_with(posted_body)  # type: ignore[attr-defined]

    def test_create_root_thread_supports_plain_text_without_request_id(self) -> None:
        response = self._response(
            {"thread": {"name": "spaces/current/threads/root-thread"}}
        )

        with patch(
            "helpers.chat_gateway.requests.post",
            return_value=response,
        ) as post:
            thread = self.gateway.create_root_thread(
                "spaces/current",
                "plain root",
                provider="openclaw",
            )

        self.assertEqual(thread, "spaces/current/threads/root-thread")
        self.assertEqual(post.call_args.kwargs["json"], {"text": "plain root"})
        self.assertIsNone(post.call_args.kwargs["params"])

    def test_create_root_thread_rejects_missing_or_cross_space_thread(self) -> None:
        invalid_responses = [
            {},
            {"thread": {"name": "threads/short"}},
            {"thread": {"name": "spaces/other/threads/root"}},
        ]

        for payload in invalid_responses:
            with self.subTest(payload=payload):
                response = self._response(payload)
                with (
                    patch(
                        "helpers.chat_gateway.requests.post",
                        return_value=response,
                    ),
                    self.assertRaises(ChatApiResponseError),
                ):
                    self.gateway.create_root_thread("spaces/current", "root")


if __name__ == "__main__":
    unittest.main()
