from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from helpers.chat_events import (
    ChatEventKind,
    ChatEventValidationCode,
    ChatEventValidationError,
    normalize_chat_event,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"fixture must contain an object: {name}")
    return value


class ChatEventNormalizationTests(unittest.TestCase):
    def test_normalizes_addon_message_authority_and_preserves_raw_text(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        raw_text = " \t/chat clear 1w\n"
        payload["chat"]["messagePayload"]["message"]["text"] = raw_text
        del payload["chat"]["messagePayload"]["message"]["argumentText"]

        event = normalize_chat_event(payload)

        self.assertIs(event.kind, ChatEventKind.MESSAGE)
        self.assertEqual(event.actor_name, "users/fixture-human-001")
        self.assertEqual(event.display_name, "Fixture User")
        self.assertEqual(event.space_name, "spaces/fixture-dm-001")
        self.assertEqual(
            event.message_name,
            "spaces/fixture-dm-001/messages/fixture-source-001",
        )
        self.assertEqual(
            event.thread_name,
            "spaces/fixture-dm-001/threads/fixture-thread-001",
        )
        self.assertEqual(event.event_time, "2026-08-11T02:30:00.123456Z")
        self.assertEqual(event.text, raw_text)
        self.assertEqual(event.attachments, ())
        self.assertEqual(event.platform, "WEB")
        self.assertFalse(event.is_legacy)

    def test_argument_text_takes_precedence_without_being_stripped(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        message = payload["chat"]["messagePayload"]["message"]
        message["argumentText"] = "\t/chat\t"
        message["text"] = "different text"

        event = normalize_chat_event(payload)

        self.assertEqual(event.text, "\t/chat\t")

    def test_normalizes_addon_attachments_without_aliasing_source_dict(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        attachment = {"name": "spaces/fixture-dm-001/attachments/fixture-001"}
        payload["chat"]["messagePayload"]["message"]["attachment"] = [attachment]

        event = normalize_chat_event(payload)
        attachment["name"] = "mutated"

        self.assertEqual(
            event.attachments[0]["name"],
            "spaces/fixture-dm-001/attachments/fixture-001",
        )

    def test_normalizes_button_fixtures_without_using_platform_as_authority(
        self,
    ) -> None:
        for fixture_name, platform in (
            ("chat_history_addon_button_web.json", "WEB"),
            ("chat_history_addon_button_android.json", "ANDROID"),
            ("chat_history_addon_button_ios.json", "IOS"),
        ):
            with self.subTest(platform=platform):
                event = normalize_chat_event(load_fixture(fixture_name))

                self.assertIs(event.kind, ChatEventKind.BUTTON_CLICK)
                self.assertEqual(event.actor_name, "users/fixture-human-001")
                self.assertEqual(event.display_name, "Fixture User")
                self.assertEqual(event.space_name, "spaces/fixture-dm-001")
                self.assertEqual(
                    event.message_name,
                    "spaces/fixture-dm-001/messages/"
                    "client-jinx-hc-0123456789abcdef0123456789abcdef-01",
                )
                self.assertEqual(event.platform, platform)
                self.assertEqual(
                    event.action_parameters["historyActionHandle"],
                    f"fixture-confirm-handle-{platform.lower()}",
                )
                self.assertIsNone(event.text)
                self.assertFalse(event.is_legacy)

    def test_normalizes_legacy_message_without_display_name_authority(self) -> None:
        event = normalize_chat_event(load_fixture("chat_history_legacy_message.json"))

        self.assertIs(event.kind, ChatEventKind.MESSAGE)
        self.assertTrue(event.is_legacy)
        self.assertIsNone(event.actor_name)
        self.assertEqual(event.display_name, "Fixture Legacy User")
        self.assertEqual(event.space_name, "spaces/fixture-legacy-dm-001")
        self.assertEqual(
            event.thread_name,
            "spaces/fixture-legacy-dm-001/threads/fixture-thread-001",
        )
        self.assertEqual(event.text, "/chat")
        self.assertIsNone(event.message_name)

    def test_legacy_stable_actor_is_used_when_present(self) -> None:
        payload = load_fixture("chat_history_legacy_message.json")
        payload["user"]["name"] = "users/fixture-legacy-human-001"

        event = normalize_chat_event(payload)

        self.assertEqual(event.actor_name, "users/fixture-legacy-human-001")
        self.assertEqual(event.display_name, "Fixture Legacy User")

    def test_legacy_sender_is_secondary_identity_and_display_source(self) -> None:
        payload = load_fixture("chat_history_legacy_message.json")
        del payload["user"]
        payload["message"]["sender"] = {
            "name": "users/fixture-legacy-human-001",
            "displayName": "Fixture Sender",
        }

        event = normalize_chat_event(payload)

        self.assertEqual(event.actor_name, "users/fixture-legacy-human-001")
        self.assertEqual(event.display_name, "Fixture Sender")

    def test_legacy_top_level_space_is_supported_and_cross_checked(self) -> None:
        payload = load_fixture("chat_history_legacy_message.json")
        payload["space"] = payload["message"].pop("space")

        event = normalize_chat_event(payload)

        self.assertEqual(event.space_name, "spaces/fixture-legacy-dm-001")

        payload["message"]["space"] = {"name": "spaces/fixture-other-dm"}
        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)
        self.assertIs(caught.exception.code, ChatEventValidationCode.SPACE_MISMATCH)

    def test_normalizes_attached_gif_as_sticker_and_quoted_snapshot(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        message = payload["chat"]["messagePayload"]["message"]
        gif = {"name": "spaces/fixture-dm-001/attachments/fixture-gif"}
        message["attachedGifs"] = [gif]
        message["quotedMessageMetadata"] = {
            "quotedMessageSnapshot": {
                "sender": "Fixture Quoted User",
                "text": "quoted fixture text",
            }
        }

        event = normalize_chat_event(payload)
        gif["name"] = "mutated"

        self.assertEqual(len(event.attachments), 1)
        self.assertEqual(
            event.attachments[0]["name"],
            "spaces/fixture-dm-001/attachments/fixture-gif",
        )
        self.assertIs(event.attachments[0]["isSticker"], True)
        self.assertEqual(
            event.quoted_message,
            {
                "sender": "Fixture Quoted User",
                "text": "quoted fixture text",
            },
        )

    def test_rejects_non_string_quoted_text_before_truthiness(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        payload["chat"]["messagePayload"]["message"]["quotedMessageMetadata"] = {
            "quotedMessageSnapshot": {"text": []}
        }

        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)

        self.assertIs(caught.exception.code, ChatEventValidationCode.INVALID_FIELD)
        self.assertEqual(
            caught.exception.field_path,
            "chat.messagePayload.message.quotedMessageMetadata."
            "quotedMessageSnapshot.text",
        )

    def test_addon_payload_takes_precedence_over_legacy_fallback(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        payload["message"] = {
            "text": "legacy text",
            "space": {"name": "spaces/fixture-other-dm"},
        }

        event = normalize_chat_event(payload)

        self.assertFalse(event.is_legacy)
        self.assertEqual(event.text, "/chat clear 1w")
        self.assertEqual(event.space_name, "spaces/fixture-dm-001")

    def test_unknown_payload_returns_typed_unknown_event(self) -> None:
        event = normalize_chat_event(
            {"chat": {"user": {"displayName": "Not authority"}}}
        )

        self.assertIs(event.kind, ChatEventKind.UNKNOWN)
        self.assertIsNone(event.actor_name)
        self.assertIsNone(event.display_name)

    def test_missing_stable_addon_actor_fails_without_display_name_fallback(
        self,
    ) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        del payload["chat"]["user"]["name"]

        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)

        self.assertIs(caught.exception.code, ChatEventValidationCode.MISSING_FIELD)
        self.assertEqual(caught.exception.field_path, "chat.user.name")
        self.assertNotIn("Fixture User", str(caught.exception))

    def test_rejects_top_level_and_payload_space_mismatch(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        payload["chat"]["space"]["name"] = "spaces/fixture-other-dm"

        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)

        self.assertIs(caught.exception.code, ChatEventValidationCode.SPACE_MISMATCH)
        self.assertEqual(caught.exception.field_path, "chat.space.name")

    def test_rejects_message_name_outside_bound_space(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        payload["chat"]["messagePayload"]["message"]["name"] = (
            "spaces/fixture-other-dm/messages/fixture-source-001"
        )

        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)

        self.assertIs(caught.exception.code, ChatEventValidationCode.SPACE_MISMATCH)
        self.assertEqual(
            caught.exception.field_path,
            "chat.messagePayload.message.name",
        )

    def test_rejects_deprecated_top_level_action_parameter_fallback(self) -> None:
        payload = load_fixture("chat_history_addon_button_web.json")
        payload["parameters"] = copy.deepcopy(
            payload["commonEventObject"]["parameters"]
        )
        del payload["commonEventObject"]["parameters"]

        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)

        self.assertIs(caught.exception.code, ChatEventValidationCode.MISSING_FIELD)
        self.assertEqual(
            caught.exception.field_path,
            "commonEventObject.parameters",
        )

    def test_rejects_ambiguous_addon_payload_family(self) -> None:
        payload = load_fixture("chat_history_addon_message.json")
        payload["chat"]["buttonClickedPayload"] = {}

        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event(payload)

        self.assertIs(caught.exception.code, ChatEventValidationCode.AMBIGUOUS_PAYLOAD)

    def test_rejects_non_object_root_with_safe_error(self) -> None:
        with self.assertRaises(ChatEventValidationError) as caught:
            normalize_chat_event([])  # type: ignore[arg-type]

        self.assertIs(caught.exception.code, ChatEventValidationCode.MALFORMED_EVENT)
        self.assertEqual(str(caught.exception), "malformed_event: $event")


if __name__ == "__main__":
    unittest.main()
