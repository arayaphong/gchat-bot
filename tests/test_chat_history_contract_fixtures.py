from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"fixture must contain an object: {name}")
    return value


class ChatHistoryContractFixtureTests(unittest.TestCase):
    def test_addon_message_fixture_has_stable_authority_fields(self) -> None:
        event = load_fixture("chat_history_addon_message.json")

        chat = event["chat"]
        payload = chat["messagePayload"]
        message = payload["message"]

        self.assertEqual(chat["user"]["type"], "HUMAN")
        self.assertTrue(chat["user"]["name"].startswith("users/fixture-"))
        self.assertEqual(chat["space"]["spaceType"], "DIRECT_MESSAGE")
        self.assertIs(chat["space"]["singleUserBotDm"], True)
        self.assertEqual(payload["space"]["name"], chat["space"]["name"])
        self.assertTrue(
            message["name"].startswith(f'{chat["space"]["name"]}/messages/')
        )
        self.assertEqual(message["sender"]["name"], chat["user"]["name"])
        self.assertIn("eventTime", chat)

    def test_button_fixtures_bind_actor_space_card_and_handle(self) -> None:
        confirmation = load_fixture("chat_history_rest_confirmation_message.json")
        fixture_names = {
            "chat_history_addon_button_web.json": "WEB",
            "chat_history_addon_button_android.json": "ANDROID",
            "chat_history_addon_button_ios.json": "IOS",
        }

        for fixture_name, platform in fixture_names.items():
            with self.subTest(platform=platform):
                event = load_fixture(fixture_name)
                common = event["commonEventObject"]
                chat = event["chat"]
                payload = chat["buttonClickedPayload"]

                self.assertEqual(common["platform"], platform)
                self.assertEqual(
                    set(common["parameters"]), {"historyActionHandle"}
                )
                self.assertTrue(
                    common["parameters"]["historyActionHandle"].startswith(
                        "fixture-"
                    )
                )
                self.assertEqual(payload["space"]["name"], chat["space"]["name"])
                self.assertEqual(
                    payload["message"]["name"], confirmation["name"]
                )
                client_id = payload["message"]["name"].rsplit("/", 1)[-1]
                self.assertLessEqual(len(client_id), 63)
                self.assertRegex(
                    client_id,
                    re.compile(r"^client-jinx-hc-[0-9a-f]{32}-[0-9]{2}$"),
                )
                self.assertEqual(chat["user"]["type"], "HUMAN")
                self.assertEqual(payload["message"]["sender"]["type"], "BOT")

        self.assertEqual(confirmation["space"]["name"], "spaces/fixture-dm-001")
        self.assertEqual(confirmation["sender"]["type"], "BOT")

    def test_all_values_are_synthetic(self) -> None:
        for fixture_path in FIXTURE_DIR.glob("chat_history_*.json"):
            with self.subTest(fixture=fixture_path.name):
                serialized = fixture_path.read_text(encoding="utf-8")
                self.assertIn("fixture", serialized.lower())
                self.assertNotIn("token.json", serialized)
                self.assertNotIn("credentials.json", serialized)

    def test_legacy_fixture_preserves_only_the_observed_field_shape(self) -> None:
        event = load_fixture("chat_history_legacy_message.json")

        self.assertEqual(set(event), {"message", "user"})
        self.assertEqual(
            set(event["message"]), {"attachment", "space", "text", "thread"}
        )
        self.assertEqual(set(event["user"]), {"displayName"})


if __name__ == "__main__":
    unittest.main()
