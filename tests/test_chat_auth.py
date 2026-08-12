from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers.services.chat_services import (
    CHAT_SERVICE_ACCOUNT,
    CHAT_SERVICE_ACCOUNT_CERTS_URL,
    ChatAuthSettings,
    ChatAuthVerifier,
)


class ChatAuthVerifierTests(unittest.TestCase):
    @staticmethod
    def _settings(
        *,
        audiences: set[str],
        trusted_emails: set[str] | None = None,
        service_email_re: re.Pattern[str] | None = None,
    ) -> ChatAuthSettings:
        return ChatAuthSettings(
            audiences=audiences,
            issuers={"https://accounts.google.com", "accounts.google.com"},
            trusted_emails=trusted_emails or set(),
            service_email_re=service_email_re,
            auth_debug=False,
        )

    @staticmethod
    def _request(token: str = "signed-token") -> SimpleNamespace:
        return SimpleNamespace(headers={"Authorization": f"Bearer {token}"})

    def test_from_env_collects_and_strips_http_and_project_audiences(self) -> None:
        env = {
            "GCHAT_AUDIENCE": " https://example.com/chat , https://alt.example/chat ",
            "GCHAT_PROJECT_NUMBER": " 1234567890 ",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = ChatAuthSettings.from_env()

        self.assertEqual(
            settings.audiences,
            {
                "https://example.com/chat",
                "https://alt.example/chat",
                "1234567890",
            },
        )
        self.assertIn(
            "service-1234567890@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
            settings.trusted_emails,
        )

    @patch("helpers.services.chat_services.google_id_token.verify_oauth2_token")
    def test_accepts_chat_http_audience_oidc_token(self, verify_oidc) -> None:
        verify_oidc.return_value = {
            "iss": "https://accounts.google.com",
            "email": CHAT_SERVICE_ACCOUNT,
        }
        verifier = ChatAuthVerifier(
            self._settings(audiences={"https://example.com/chat"})
        )

        self.assertTrue(verifier.verify(self._request()))

        _, request = verify_oidc.call_args.args
        self.assertIsNotNone(request)
        self.assertEqual(
            verify_oidc.call_args.kwargs["audience"], ["https://example.com/chat"]
        )

    @patch("helpers.services.chat_services.google_id_token.verify_oauth2_token")
    def test_keeps_workspace_addon_service_account_compatibility(
        self, verify_oidc
    ) -> None:
        addon_email = "service-1234567890@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
        verify_oidc.return_value = {
            "iss": "accounts.google.com",
            "email": addon_email,
        }
        verifier = ChatAuthVerifier(
            self._settings(
                audiences={"https://example.com/chat"},
                trusted_emails={addon_email},
            )
        )

        self.assertTrue(verifier.verify(self._request()))

    @patch("helpers.services.chat_services.google_id_token.verify_token")
    @patch("helpers.services.chat_services.google_id_token.verify_oauth2_token")
    def test_rejects_untrusted_http_audience_identity(
        self, verify_oidc, verify_project_jwt
    ) -> None:
        verify_oidc.return_value = {
            "iss": "https://accounts.google.com",
            "email": "attacker@example.com",
        }
        verifier = ChatAuthVerifier(
            self._settings(audiences={"https://example.com/chat"})
        )

        self.assertFalse(verifier.verify(self._request()))
        verify_project_jwt.assert_not_called()

    @patch("helpers.services.chat_services.google_id_token.verify_token")
    @patch("helpers.services.chat_services.google_id_token.verify_oauth2_token")
    def test_accepts_project_number_jwt_with_chat_certificate_and_issuer(
        self, verify_oidc, verify_project_jwt
    ) -> None:
        verify_project_jwt.return_value = {"iss": CHAT_SERVICE_ACCOUNT}
        verifier = ChatAuthVerifier(self._settings(audiences={"1234567890"}))

        self.assertTrue(verifier.verify(self._request()))

        verify_oidc.assert_not_called()
        self.assertEqual(verify_project_jwt.call_args.args[0], "signed-token")
        self.assertIsNotNone(verify_project_jwt.call_args.args[1])
        self.assertEqual(
            verify_project_jwt.call_args.kwargs,
            {
                "audience": ["1234567890"],
                "certs_url": CHAT_SERVICE_ACCOUNT_CERTS_URL,
            },
        )

    @patch("helpers.services.chat_services.google_id_token.verify_token")
    def test_rejects_project_number_jwt_with_wrong_issuer(
        self, verify_project_jwt
    ) -> None:
        verify_project_jwt.return_value = {"iss": "someone@example.com"}
        verifier = ChatAuthVerifier(self._settings(audiences={"1234567890"}))

        self.assertFalse(verifier.verify(self._request()))

    @patch("helpers.services.chat_services.google_id_token.verify_token")
    @patch("helpers.services.chat_services.google_id_token.verify_oauth2_token")
    def test_mixed_configuration_uses_mode_specific_audiences(
        self, verify_oidc, verify_project_jwt
    ) -> None:
        verify_oidc.side_effect = ValueError("not an OIDC token")
        verify_project_jwt.return_value = {"iss": CHAT_SERVICE_ACCOUNT}
        verifier = ChatAuthVerifier(
            self._settings(audiences={"https://example.com/chat", "1234567890"})
        )

        self.assertTrue(verifier.verify(self._request()))
        self.assertEqual(
            verify_oidc.call_args.kwargs["audience"], ["https://example.com/chat"]
        )
        self.assertEqual(
            verify_project_jwt.call_args.kwargs["audience"], ["1234567890"]
        )

    @patch("helpers.services.chat_services.google_id_token.verify_token")
    @patch("helpers.services.chat_services.google_id_token.verify_oauth2_token")
    def test_rejects_missing_configuration_or_bearer_header(
        self, verify_oidc, verify_project_jwt
    ) -> None:
        verifier = ChatAuthVerifier(self._settings(audiences=set()))

        self.assertFalse(verifier.verify(self._request()))
        self.assertFalse(
            ChatAuthVerifier(
                self._settings(audiences={"https://example.com/chat"})
            ).verify(SimpleNamespace(headers={}))
        )
        verify_oidc.assert_not_called()
        verify_project_jwt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
