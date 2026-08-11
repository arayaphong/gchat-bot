from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests

from helpers.chat_gateway import (
    CHAT_API_BASE_URL,
    ChatApiResponseError,
    ChatAppMembershipError,
    ChatGateway,
    ChatSpaceCreationResult,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.services import CardPresenter, CredentialService


class ChatSpaceCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.credentials = Mock()
        self.credentials.get_user_token.return_value = "user-token"
        self.credentials.get_bot_token.return_value = "bot-token"
        self.gateway = self._make_gateway()

    def _make_gateway(self) -> ChatGateway:
        return ChatGateway(
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

    def test_credential_service_keeps_bot_and_combined_user_scopes_separate(
        self,
    ) -> None:
        user_scopes = [
            "drive.file",
            "chat.spaces.create",
            "chat.memberships.app",
        ]
        service = CredentialService(
            bot_cred=self.root / "credentials.json",
            token_file=self.root / "token.json",
            scopes_bot=["chat.bot"],
            scopes_user=user_scopes,
        )
        bot_creds = Mock(token="bot-token")
        user_creds = Mock(
            token="user-token",
            expired=False,
            refresh_token="refresh-token",
        )

        with (
            patch(
                "helpers.services.chat_services.service_account.Credentials."
                "from_service_account_file",
                return_value=bot_creds,
            ) as load_bot,
            patch(
                "helpers.services.chat_services.UserCreds."
                "from_authorized_user_file",
                return_value=user_creds,
            ) as load_user,
        ):
            self.assertEqual(service.get_bot_token(), "bot-token")
            self.assertEqual(service.get_user_token(), "user-token")

        load_bot.assert_called_once_with(
            str(self.root / "credentials.json"),
            scopes=["chat.bot"],
        )
        load_user.assert_called_once_with(
            str(self.root / "token.json"),
            user_scopes,
        )
        bot_creds.refresh.assert_called_once()
        user_creds.refresh.assert_not_called()

    def test_create_space_uses_user_auth_then_adds_calling_app(self) -> None:
        space_response = self._response(
            {
                "name": "spaces/new-space",
                "displayName": "Jinx session 42",
                "spaceUri": "https://chat.google.com/room/new-space",
            }
        )
        membership_response = self._response(
            {"name": "spaces/new-space/members/app"}
        )

        with patch(
            "helpers.chat_gateway.requests.post",
            side_effect=[space_response, membership_response],
        ) as post:
            result = self.gateway.create_space_for_user(
                "  Jinx session 42  ",
                request_id="create-space-request-42",
            )

        self.assertEqual(
            result,
            ChatSpaceCreationResult(
                name="spaces/new-space",
                display_name="Jinx session 42",
                space_uri="https://chat.google.com/room/new-space",
            ),
        )
        self.credentials.get_user_token.assert_called_once_with()
        self.credentials.get_bot_token.assert_not_called()
        self.assertEqual(
            post.call_args_list,
            [
                call(
                    f"{CHAT_API_BASE_URL}/spaces",
                    headers={
                        "Authorization": "Bearer user-token",
                        "Content-Type": "application/json",
                    },
                    params={"requestId": "create-space-request-42"},
                    json={
                        "spaceType": "SPACE",
                        "displayName": "Jinx session 42",
                    },
                    timeout=15,
                ),
                call(
                    f"{CHAT_API_BASE_URL}/spaces/new-space/members",
                    headers={
                        "Authorization": "Bearer user-token",
                        "Content-Type": "application/json",
                    },
                    json={
                        "member": {
                            "name": "users/app",
                            "type": "BOT",
                        }
                    },
                    timeout=15,
                ),
            ],
        )
        space_response.raise_for_status.assert_called_once_with()
        membership_response.raise_for_status.assert_called_once_with()

    def test_create_space_omits_customer_and_uses_uri_fallback(self) -> None:
        space_response = self._response(
            {
                "name": "spaces/opaque-id",
                "displayName": "Returned display name",
            }
        )
        membership_response = self._response({})

        with patch(
            "helpers.chat_gateway.requests.post",
            side_effect=[space_response, membership_response],
        ) as post:
            result = self.gateway.create_space_for_user("Requested")

        self.assertEqual(result.display_name, "Returned display name")
        self.assertEqual(
            result.space_uri,
            "https://mail.google.com/chat/u/0/#chat/space/opaque-id",
        )
        self.assertNotIn("customer", post.call_args_list[0].kwargs["json"])
        self.assertIsNone(post.call_args_list[0].kwargs["params"])

    def test_create_space_validates_inputs_before_fetching_token(self) -> None:
        invalid_calls = [
            ("", ValueError),
            ("x" * 129, ValueError),
        ]

        with patch("helpers.chat_gateway.requests.post") as post:
            for display_name, error_type in invalid_calls:
                with (
                    self.subTest(display_name=display_name),
                    self.assertRaises(error_type),
                ):
                    self.gateway.create_space_for_user(display_name)

        post.assert_not_called()
        self.credentials.get_user_token.assert_not_called()
        self.credentials.get_bot_token.assert_not_called()

    def test_create_space_rejects_success_response_without_valid_name(self) -> None:
        response = self._response({"displayName": "Missing resource name"})

        with (
            patch("helpers.chat_gateway.requests.post", return_value=response) as post,
            self.assertRaises(ChatApiResponseError),
        ):
            self.gateway.create_space_for_user("New space")

        post.assert_called_once()

    def test_membership_failure_retains_created_space_in_typed_error(self) -> None:
        space_response = self._response(
            {
                "name": "spaces/orphaned",
                "displayName": "Potential orphan",
                "spaceUri": "https://chat.google.com/room/orphaned",
            }
        )
        membership_response = self._response({})
        http_error = requests.HTTPError("membership denied")
        membership_response.raise_for_status.side_effect = http_error

        with patch(
            "helpers.chat_gateway.requests.post",
            side_effect=[space_response, membership_response],
        ), self.assertRaises(ChatAppMembershipError) as caught:
            self.gateway.create_space_for_user("Potential orphan")

        self.assertEqual(caught.exception.space.name, "spaces/orphaned")
        self.assertIs(caught.exception.__cause__, http_error)

    def test_existing_membership_allows_idempotent_space_retry(self) -> None:
        space_response = self._response(
            {
                "name": "spaces/recovered",
                "displayName": "Recovered",
            }
        )
        membership_response = self._response({})
        membership_response.status_code = 409
        membership_response.raise_for_status.side_effect = requests.HTTPError(
            "already exists"
        )

        with patch(
            "helpers.chat_gateway.requests.post",
            side_effect=[space_response, membership_response],
        ):
            result = self.gateway.create_space_for_user(
                "Recovered",
                request_id="stable-retry",
            )

        self.assertEqual(result.name, "spaces/recovered")
        membership_response.raise_for_status.assert_not_called()

    def test_seed_space_root_posts_without_thread_and_returns_full_thread_name(
        self,
    ) -> None:
        response = self._response(
            {
                "name": "spaces/new-space/messages/root",
                "thread": {"name": "spaces/new-space/threads/root-thread"},
            }
        )
        self.gateway.record_outgoing = Mock()  # type: ignore[method-assign]

        with patch(
            "helpers.chat_gateway.requests.post",
            return_value=response,
        ) as post:
            thread = self.gateway.seed_space_root(
                "spaces/new-space",
                "เริ่มเซสชั่นใหม่แล้ว",
                request_id="seed-message-42",
            )

        self.assertEqual(thread, "spaces/new-space/threads/root-thread")
        posted_body = post.call_args.kwargs["json"]
        self.assertNotIn("thread", posted_body)
        self.assertIn("cardsV2", posted_body)
        post.assert_called_once_with(
            f"{CHAT_API_BASE_URL}/spaces/new-space/messages",
            headers={
                "Authorization": "Bearer bot-token",
                "Content-Type": "application/json",
            },
            params={"requestId": "seed-message-42"},
            json=posted_body,
            timeout=15,
        )
        self.gateway.record_outgoing.assert_called_once_with(posted_body)  # type: ignore[attr-defined]

    def test_seed_space_root_supports_plain_text_without_request_id(self) -> None:
        response = self._response(
            {"thread": {"name": "spaces/new-space/threads/root-thread"}}
        )

        with patch(
            "helpers.chat_gateway.requests.post",
            return_value=response,
        ) as post:
            thread = self.gateway.seed_space_root(
                "spaces/new-space",
                "plain seed",
                provider="openclaw",
            )

        self.assertEqual(thread, "spaces/new-space/threads/root-thread")
        self.assertEqual(post.call_args.kwargs["json"], {"text": "plain seed"})
        self.assertIsNone(post.call_args.kwargs["params"])

    def test_seed_space_root_rejects_missing_or_cross_space_thread(self) -> None:
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
                    self.gateway.seed_space_root("spaces/new-space", "seed")


if __name__ == "__main__":
    unittest.main()
