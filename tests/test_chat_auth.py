from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers.services.chat_services import (
    CHAT_SERVICE_ACCOUNT,
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

    @patch.object(ChatAuthVerifier, "_fetch_certs", return_value={"kid": "cert"})
    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_accepts_chat_http_audience_oidc_token(self, decode, _fetch_certs) -> None:
        decode.return_value = {
            "iss": "https://accounts.google.com",
            "email": CHAT_SERVICE_ACCOUNT,
        }
        verifier = ChatAuthVerifier(
            self._settings(audiences={"https://example.com/chat"})
        )

        self.assertTrue(verifier.verify(self._request()))

        self.assertEqual(decode.call_args.args[0], "signed-token")
        self.assertEqual(
            decode.call_args.kwargs["audience"], ["https://example.com/chat"]
        )
        self.assertEqual(decode.call_args.kwargs["certs"], {"kid": "cert"})

    @patch.object(ChatAuthVerifier, "_fetch_certs", return_value={"kid": "cert"})
    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_keeps_workspace_addon_service_account_compatibility(
        self, decode, _fetch_certs
    ) -> None:
        addon_email = "service-1234567890@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
        decode.return_value = {
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

    @patch.object(ChatAuthVerifier, "_fetch_certs", return_value={"kid": "cert"})
    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_rejects_untrusted_http_audience_identity(
        self, decode, _fetch_certs
    ) -> None:
        decode.return_value = {
            "iss": "https://accounts.google.com",
            "email": "attacker@example.com",
        }
        verifier = ChatAuthVerifier(
            self._settings(audiences={"https://example.com/chat"})
        )

        self.assertFalse(verifier.verify(self._request()))
        # No project number is configured, so the project-JWT path must not
        # attempt a second decode.
        self.assertEqual(decode.call_count, 1)

    @patch.object(ChatAuthVerifier, "_fetch_certs", return_value={"kid": "cert"})
    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_accepts_project_number_jwt_with_chat_certificate_and_issuer(
        self, decode, _fetch_certs
    ) -> None:
        decode.return_value = {"iss": CHAT_SERVICE_ACCOUNT}
        verifier = ChatAuthVerifier(self._settings(audiences={"1234567890"}))

        self.assertTrue(verifier.verify(self._request()))

        self.assertEqual(decode.call_count, 1)
        self.assertEqual(decode.call_args.args[0], "signed-token")
        self.assertEqual(decode.call_args.kwargs["audience"], ["1234567890"])

    @patch.object(ChatAuthVerifier, "_fetch_certs", return_value={"kid": "cert"})
    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_rejects_project_number_jwt_with_wrong_issuer(
        self, decode, _fetch_certs
    ) -> None:
        decode.return_value = {"iss": "someone@example.com"}
        verifier = ChatAuthVerifier(self._settings(audiences={"1234567890"}))

        self.assertFalse(verifier.verify(self._request()))

    @patch.object(ChatAuthVerifier, "_fetch_certs", return_value={"kid": "cert"})
    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_mixed_configuration_uses_mode_specific_audiences(
        self, decode, _fetch_certs
    ) -> None:
        decode.side_effect = [
            ValueError("not an OIDC token"),
            {"iss": CHAT_SERVICE_ACCOUNT},
        ]
        verifier = ChatAuthVerifier(
            self._settings(audiences={"https://example.com/chat", "1234567890"})
        )

        self.assertTrue(verifier.verify(self._request()))
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(
            decode.call_args_list[0].kwargs["audience"], ["https://example.com/chat"]
        )
        self.assertEqual(
            decode.call_args_list[1].kwargs["audience"], ["1234567890"]
        )

    @patch("helpers.services.chat_services.google_auth_jwt.decode")
    def test_rejects_missing_configuration_or_bearer_header(self, decode) -> None:
        verifier = ChatAuthVerifier(self._settings(audiences=set()))

        self.assertFalse(verifier.verify(self._request()))
        self.assertFalse(
            ChatAuthVerifier(
                self._settings(audiences={"https://example.com/chat"})
            ).verify(SimpleNamespace(headers={}))
        )
        decode.assert_not_called()

    @patch("helpers.services.chat_services.google_id_token.fetch_certs")
    def test_signing_certs_are_cached_per_url(self, fetch_certs) -> None:
        fetch_certs.return_value = {"kid": "cert"}
        verifier = ChatAuthVerifier(self._settings(audiences=set()))

        self.assertEqual(
            verifier._fetch_certs("https://example.com/certs"), {"kid": "cert"}
        )
        self.assertEqual(
            verifier._fetch_certs("https://example.com/certs"), {"kid": "cert"}
        )
        fetch_certs.assert_called_once()


if __name__ == "__main__":
    unittest.main()
