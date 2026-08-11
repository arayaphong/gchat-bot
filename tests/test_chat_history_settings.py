from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers.chat_history_settings import (
    CARD_ACTION_URL_ENV,
    HISTORY_ALLOWED_SPACE_ENV,
    HISTORY_ALLOWED_USER_ENV,
    HISTORY_DELETE_ENABLED_ENV,
    HISTORY_ENABLED_ENV,
    HISTORY_STATE_DIR_ENV,
    OUTBOUND_SPACE_ENV,
    ChatHistorySettings,
    ChatHistorySettingsError,
    chat_history_state_dir_from_env,
    default_chat_history_state_dir,
)


class ChatHistorySettingsTests(unittest.TestCase):
    @staticmethod
    def enabled_env(**overrides: str) -> dict[str, str]:
        return {
            HISTORY_ENABLED_ENV: "true",
            HISTORY_DELETE_ENABLED_ENV: "false",
            HISTORY_ALLOWED_USER_ENV: "users/fixture-human-001",
            HISTORY_ALLOWED_SPACE_ENV: "spaces/fixture-dm-001",
        } | overrides

    def test_defaults_keep_both_feature_flags_disabled(self) -> None:
        settings = ChatHistorySettings.from_env({})

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.delete_enabled)
        self.assertIsNone(settings.allowed_user)
        self.assertIsNone(settings.allowed_space)
        self.assertIsNone(settings.card_action_url)
        self.assertEqual(
            settings.state_dir,
            default_chat_history_state_dir().resolve(strict=False),
        )

    def test_flags_accept_only_exact_lowercase_true_or_false(self) -> None:
        for name in (HISTORY_ENABLED_ENV, HISTORY_DELETE_ENABLED_ENV):
            for invalid in ("", "1", "0", "yes", "TRUE", "False", " true "):
                with self.subTest(name=name, invalid=invalid):
                    values = {name: invalid}
                    with self.assertRaises(ChatHistorySettingsError):
                        ChatHistorySettings.from_env(values)

    def test_delete_cannot_be_enabled_while_history_is_disabled(self) -> None:
        with self.assertRaisesRegex(
            ChatHistorySettingsError,
            HISTORY_ENABLED_ENV,
        ):
            ChatHistorySettings.from_env({HISTORY_DELETE_ENABLED_ENV: "true"})

    def test_enabled_history_requires_exact_user_and_space_resources(self) -> None:
        for missing in (HISTORY_ALLOWED_USER_ENV, HISTORY_ALLOWED_SPACE_ENV):
            values = self.enabled_env()
            del values[missing]
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(ChatHistorySettingsError, missing),
            ):
                ChatHistorySettings.from_env(values)

    def test_exact_resource_names_are_preserved(self) -> None:
        settings = ChatHistorySettings.from_env(self.enabled_env())

        self.assertEqual(settings.allowed_user, "users/fixture-human-001")
        self.assertEqual(settings.allowed_space, "spaces/fixture-dm-001")

    def test_noncanonical_user_resources_are_rejected(self) -> None:
        invalid_users = (
            "users/me",
            "users/all",
            "users/app",
            "users/",
            "people/123",
            "users/one/extra",
            "users/one,users/two",
            "users/*",
            " users/123",
            "users/123\n",
            f"users/{'x' * 256}",
        )
        for value in invalid_users:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ChatHistorySettingsError,
                    HISTORY_ALLOWED_USER_ENV,
                ),
            ):
                ChatHistorySettings.from_env(
                    self.enabled_env(**{HISTORY_ALLOWED_USER_ENV: value})
                )

    def test_noncanonical_space_resources_are_rejected(self) -> None:
        invalid_spaces = (
            "spaces/",
            "space/one",
            "spaces/one/messages/two",
            "spaces/one,spaces/two",
            "spaces/*",
            " spaces/one",
            "spaces/one\n",
            f"spaces/{'x' * 256}",
        )
        for value in invalid_spaces:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ChatHistorySettingsError,
                    HISTORY_ALLOWED_SPACE_ENV,
                ),
            ):
                ChatHistorySettings.from_env(
                    self.enabled_env(**{HISTORY_ALLOWED_SPACE_ENV: value})
                )

    def test_explicit_outbound_space_env_is_the_only_space_fallback(self) -> None:
        values = self.enabled_env()
        del values[HISTORY_ALLOWED_SPACE_ENV]

        with (
            patch.dict(
                os.environ,
                {OUTBOUND_SPACE_ENV: "spaces/process-global-must-not-leak"},
                clear=True,
            ),
            self.assertRaisesRegex(
                ChatHistorySettingsError,
                HISTORY_ALLOWED_SPACE_ENV,
            ),
        ):
            ChatHistorySettings.from_env(values)

        values[OUTBOUND_SPACE_ENV] = "spaces/explicit-outbound-001"
        settings = ChatHistorySettings.from_env(values)
        self.assertEqual(settings.allowed_space, "spaces/explicit-outbound-001")

    def test_explicit_history_and_outbound_spaces_must_match(self) -> None:
        matching = ChatHistorySettings.from_env(
            self.enabled_env(**{OUTBOUND_SPACE_ENV: "spaces/fixture-dm-001"})
        )
        self.assertEqual(matching.allowed_space, "spaces/fixture-dm-001")

        with self.assertRaisesRegex(ChatHistorySettingsError, "must match"):
            ChatHistorySettings.from_env(
                self.enabled_env(**{OUTBOUND_SPACE_ENV: "spaces/other"})
            )

    def test_action_url_is_not_required_while_delete_is_disabled(self) -> None:
        settings = ChatHistorySettings.from_env(self.enabled_env())
        self.assertIsNone(settings.card_action_url)

    def test_delete_requires_and_preserves_an_absolute_https_action_url(self) -> None:
        action_url = "https://chat.example.test/callback?source=card"
        settings = ChatHistorySettings.from_env(
            self.enabled_env(
                **{
                    HISTORY_DELETE_ENABLED_ENV: "true",
                    CARD_ACTION_URL_ENV: action_url,
                }
            )
        )

        self.assertTrue(settings.delete_enabled)
        self.assertEqual(settings.card_action_url, action_url)

    def test_delete_rejects_missing_or_unsafe_action_urls(self) -> None:
        invalid_urls = (
            "",
            "http://chat.example.test/callback",
            "/callback",
            "https:///callback?host=chat.example.test",
            "https://user@chat.example.test/callback",
            "https://chat.example.test/callback#fragment",
            "https://chat.example.test\\@evil.example/callback",
            "https://chat.example.test:99999/callback",
            " https://chat.example.test/callback",
        )
        for value in invalid_urls:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ChatHistorySettingsError,
                    CARD_ACTION_URL_ENV,
                ),
            ):
                ChatHistorySettings.from_env(
                    self.enabled_env(
                        **{
                            HISTORY_DELETE_ENABLED_ENV: "true",
                            CARD_ACTION_URL_ENV: value,
                        }
                    )
                )

    def test_invalid_optional_action_url_is_not_silently_retained(self) -> None:
        with self.assertRaisesRegex(ChatHistorySettingsError, CARD_ACTION_URL_ENV):
            ChatHistorySettings.from_env(
                self.enabled_env(**{CARD_ACTION_URL_ENV: "http://unsafe.example"})
            )

    def test_errors_name_the_setting_without_echoing_its_value(self) -> None:
        secret_value = "https://user:secret@example.test/callback"
        with self.assertRaises(ChatHistorySettingsError) as raised:
            ChatHistorySettings.from_env(
                self.enabled_env(
                    **{
                        HISTORY_DELETE_ENABLED_ENV: "true",
                        CARD_ACTION_URL_ENV: secret_value,
                    }
                )
            )

        self.assertIn(CARD_ACTION_URL_ENV, str(raised.exception))
        self.assertNotIn(secret_value, str(raised.exception))

    def test_configured_state_directory_is_expanded_and_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "nested" / ".." / "history"
            resolved = chat_history_state_dir_from_env(
                {HISTORY_STATE_DIR_ENV: str(configured)}
            )

        self.assertEqual(resolved, configured.resolve(strict=False))


if __name__ == "__main__":
    unittest.main()
