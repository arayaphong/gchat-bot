from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.token_tools import get_token, get_token_manual
from helpers.token_tools.oauth_config import USER_OAUTH_SCOPES, write_oauth_token


class OAuthTokenToolTests(unittest.TestCase):
    def test_shared_scopes_are_the_required_drive_and_chat_scopes(self) -> None:
        self.assertEqual(
            USER_OAUTH_SCOPES,
            (
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/chat.spaces.create",
                "https://www.googleapis.com/auth/chat.memberships.app",
            ),
        )

    def test_token_file_is_replaced_atomically_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "private" / "token.json"

            write_oauth_token(token_file, '{"refresh_token":"secret"}')
            write_oauth_token(token_file, '{"refresh_token":"replacement"}')

            self.assertEqual(
                token_file.read_text(encoding="utf-8"),
                '{"refresh_token":"replacement"}',
            )
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                list(token_file.parent.glob(".token.json.*.tmp")),
                [],
            )

    def test_local_flow_forces_offline_consent_and_uses_shared_scopes(self) -> None:
        flow = Mock()
        credentials = Mock()
        credentials.to_json.return_value = '{"token":"access"}'
        flow.run_local_server.return_value = credentials

        with (
            patch.object(
                get_token.InstalledAppFlow,
                "from_client_secrets_file",
                return_value=flow,
            ) as create_flow,
            patch.object(get_token, "write_oauth_token") as write_token,
            patch("builtins.print"),
        ):
            get_token.main()

        create_flow.assert_called_once_with("client_secret.json", USER_OAUTH_SCOPES)
        flow.run_local_server.assert_called_once_with(
            port=0,
            access_type="offline",
            prompt="consent",
        )
        write_token.assert_called_once_with(
            Path("token.json"),
            '{"token":"access"}',
        )

    def test_manual_flow_uses_the_full_redirect_url_and_private_writer(self) -> None:
        flow = Mock()
        flow.authorization_url.return_value = ("https://consent.example", "state")
        flow.credentials.to_json.return_value = '{"token":"manual"}'

        with (
            patch.object(
                get_token_manual.InstalledAppFlow,
                "from_client_secrets_file",
                return_value=flow,
            ),
            patch("builtins.input", return_value="http://localhost/?code=abc"),
            patch.object(get_token_manual, "write_oauth_token") as write_token,
            patch("builtins.print"),
        ):
            get_token_manual.main()

        self.assertEqual(flow.redirect_uri, "http://localhost")
        flow.authorization_url.assert_called_once_with(
            access_type="offline",
            prompt="consent",
        )
        flow.fetch_token.assert_called_once_with(
            authorization_response="http://localhost/?code=abc"
        )
        write_token.assert_called_once_with(
            Path("token.json"),
            '{"token":"manual"}',
        )


if __name__ == "__main__":
    unittest.main()
