from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

with patch("pathlib.Path.mkdir"):
    import app as app_module

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"fixture must contain an object: {name}")
    return value


def addon_message(text: str) -> dict[str, Any]:
    payload = load_fixture("chat_history_addon_message.json")
    message = payload["chat"]["messagePayload"]["message"]
    message["argumentText"] = text
    message["text"] = text
    return payload


class ChatRouteEventTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_authentication_happens_before_accepted_event_logging(self) -> None:
        events: list[str] = []

        def verify(_request: object) -> bool:
            events.append("auth")
            return True

        def record(_body: dict[str, Any]) -> None:
            events.append("log")

        with (
            patch.object(app_module.auth_verifier, "verify", side_effect=verify),
            patch.object(app_module.gateway, "record_incoming", side_effect=record),
            patch.object(app_module.gateway, "ack", return_value={}),
        ):
            response = self.client.post("/chat", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["auth", "log"])

    def test_unauthorized_body_is_never_logged_or_routed(self) -> None:
        with (
            patch.object(app_module.auth_verifier, "verify", return_value=False),
            patch.object(app_module.gateway, "record_incoming") as record,
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(
                app_module, "_start_outbound_attachment_service"
            ) as start_watcher,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = self.client.post(
                "/chat",
                json={
                    "commonEventObject": {
                        "parameters": {"historyActionHandle": "sentinel-secret"}
                    }
                },
            )

        self.assertEqual(response.status_code, 401)
        record.assert_not_called()
        remember.assert_not_called()
        start_watcher.assert_not_called()
        dispatch.assert_not_called()

    def test_malformed_addon_event_fails_closed_with_safe_diagnostic(self) -> None:
        payload = addon_message("sentinel-private-message")
        del payload["chat"]["user"]["name"]

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming") as record,
            patch.object(app_module.gateway, "ack", return_value={}) as ack,
            patch.object(app_module.gateway, "send_followup") as send,
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(
                app_module, "_start_outbound_attachment_service"
            ) as start_watcher,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 400)
        record.assert_called_once_with(
            {
                "eventType": "REJECTED",
                "errorCategory": "missing_field",
                "fieldPath": "chat.user.name",
            }
        )
        self.assertNotIn("sentinel-private-message", repr(record.call_args))
        ack.assert_not_called()
        send.assert_not_called()
        remember.assert_not_called()
        start_watcher.assert_not_called()
        dispatch.assert_not_called()

    def test_button_callback_does_not_enter_normal_message_pipeline(self) -> None:
        payload = load_fixture("chat_history_addon_button_web.json")

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming") as record,
            patch.object(app_module.gateway, "ack", return_value={}) as ack,
            patch.object(app_module.gateway, "send_followup") as send,
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(
                app_module, "_start_outbound_attachment_service"
            ) as start_watcher,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        record.assert_called_once_with(payload)
        ack.assert_called_once_with()
        send.assert_not_called()
        remember.assert_not_called()
        start_watcher.assert_not_called()
        dispatch.assert_not_called()

    def test_reserved_history_commands_do_not_enter_normal_pipeline(self) -> None:
        for command in ("/chat", "/chat clear 1w", "/Chat", "/chat!"):
            with self.subTest(command=command):
                payload = addon_message(command)
                payload["chat"]["messagePayload"]["message"]["attachment"] = [
                    {"contentName": "must-not-download.txt"}
                ]

                with (
                    patch.object(app_module.auth_verifier, "verify", return_value=True),
                    patch.object(app_module.gateway, "record_incoming"),
                    patch.object(app_module.gateway, "ack", return_value={}),
                    patch.object(
                        app_module.gateway, "send_followup", return_value=True
                    ) as send,
                    patch.object(app_module.target_store, "remember") as remember,
                    patch.object(
                        app_module, "_start_outbound_attachment_service"
                    ) as start_watcher,
                    patch.object(app_module.orchestrator, "dispatch") as dispatch,
                ):
                    response = self.client.post("/chat", json=payload)

                self.assertEqual(response.status_code, 200)
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0], "spaces/fixture-dm-001")
                remember.assert_not_called()
                start_watcher.assert_not_called()
                dispatch.assert_not_called()

    def test_chatty_remains_on_normal_dispatch_path(self) -> None:
        payload = addon_message("  /chatty  ")
        message = payload["chat"]["messagePayload"]["message"]
        message["attachment"] = [{"contentName": "fixture.txt"}]
        message["attachedGifs"] = [{"contentName": "fixture.gif"}]
        message["quotedMessageMetadata"] = {
            "quotedMessageSnapshot": {
                "sender": "Fixture Quoted User",
                "text": "fixture quote",
            }
        }
        expected_payload = copy.deepcopy(payload)

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming") as record,
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(app_module.gateway, "send_followup") as send,
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(
                app_module,
                "_start_outbound_attachment_service",
                return_value=True,
            ) as start_watcher,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        record.assert_called_once_with(expected_payload)
        remember.assert_called_once_with(
            "spaces/fixture-dm-001",
            "spaces/fixture-dm-001/threads/fixture-thread-001",
        )
        start_watcher.assert_called_once_with()
        dispatch.assert_called_once_with(
            "spaces/fixture-dm-001",
            "spaces/fixture-dm-001/threads/fixture-thread-001",
            "Fixture User",
            "/chatty",
            [
                {"contentName": "fixture.txt"},
                {"contentName": "fixture.gif", "isSticker": True},
            ],
            {"sender": "Fixture Quoted User", "text": "fixture quote"},
        )
        send.assert_not_called()
        self.assertEqual(
            payload, expected_payload, "normalization must not mutate input"
        )


if __name__ == "__main__":
    unittest.main()
