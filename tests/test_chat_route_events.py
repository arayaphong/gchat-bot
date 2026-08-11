from __future__ import annotations

import copy
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from helpers.chat_clear_store import ActionKind, ActionResult, JobStatus
from helpers.chat_history_service import ChatHistoryService

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

    def test_enabled_stats_ack_does_not_wait_for_blocked_chat_api(self) -> None:
        payload = addon_message("/chat")
        actor = payload["chat"]["user"]["name"]
        space = payload["chat"]["messagePayload"]["space"]["name"]
        source = payload["chat"]["messagePayload"]["message"]["name"]
        entered_api = threading.Event()
        release_api = threading.Event()
        delivered = threading.Event()
        route_done = threading.Event()
        result: list[Any] = []

        history_client = Mock()
        history_client.allowed_space = space

        def blocked_list(**_kwargs: Any) -> list[dict[str, Any]]:
            entered_api.set()
            release_api.wait(5)
            return [{"name": source}]

        history_client.iter_messages.side_effect = blocked_list
        history_gateway = Mock()

        def mark_delivered(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            delivered.set()
            return {"name": f"{space}/messages/stats"}

        history_gateway.send_structured_card.side_effect = mark_delivered
        service = ChatHistoryService(history_client, history_gateway)

        def post_request() -> None:
            client = app_module.app.test_client()
            result.append(client.post("/chat", json=payload))
            route_done.set()

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}) as ack,
            patch.object(
                app_module,
                "history_settings",
                SimpleNamespace(enabled=True),
            ),
            patch.object(app_module, "history_service", service),
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            request_thread = threading.Thread(target=post_request)
            request_thread.start()
            self.assertTrue(entered_api.wait(2), "background API was not entered")
            self.assertTrue(route_done.wait(1), "webhook waited for the blocked API")
            self.assertEqual(result[0].status_code, 200)
            ack.assert_called_once_with()
            remember.assert_not_called()
            dispatch.assert_not_called()
            release_api.set()
            request_thread.join(5)
            self.assertTrue(delivered.wait(2))

        history_client.validate_authority.assert_called_once_with(actor, space)

    def test_enabled_clear_remains_reserved_and_creates_no_history_operation(
        self,
    ) -> None:
        payload = addon_message("/chat clear 1w")
        service = Mock()
        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "ack", return_value={}),
            patch.object(
                app_module.gateway, "send_followup", return_value=True
            ) as send,
            patch.object(
                app_module,
                "history_settings",
                SimpleNamespace(enabled=True, delete_enabled=False),
            ),
            patch.object(app_module, "history_service", service),
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        service.submit_stats.assert_not_called()
        send.assert_called_once()
        self.assertIn("ยังไม่เปิดใช้งาน", send.call_args.args[2])
        remember.assert_not_called()
        dispatch.assert_not_called()

    def test_enabled_clear_persists_before_ack_and_wakes_worker(self) -> None:
        payload = addon_message("/chat clear 1w")
        events: list[str] = []
        clear_client = Mock()
        coordinator = Mock()
        worker = Mock()
        create_result = SimpleNamespace(created=True)

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(
                app_module.gateway,
                "ack",
                side_effect=lambda: events.append("ack") or {},
            ),
            patch.object(
                app_module.chat_clear_store,
                "create_job",
                side_effect=lambda _request: events.append("persist")
                or create_result,
            ) as create_job,
            patch.object(
                app_module,
                "history_settings",
                SimpleNamespace(enabled=True, delete_enabled=True),
            ),
            patch.object(app_module, "history_client", clear_client),
            patch.object(app_module, "history_clear_coordinator", coordinator),
            patch.object(app_module, "history_worker", worker),
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            worker.wake.side_effect = lambda: events.append("wake")
            response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["persist", "ack", "wake"])
        clear_client.validate_authority.assert_called_once_with(
            "users/fixture-human-001", "spaces/fixture-dm-001"
        )
        durable = create_job.call_args.args[0]
        self.assertEqual(
            durable.source_message_name,
            "spaces/fixture-dm-001/messages/fixture-source-001",
        )
        self.assertEqual(durable.normalized_argument, "1w")
        remember.assert_not_called()
        dispatch.assert_not_called()

    def test_valid_button_updates_card_records_pacer_then_wakes(self) -> None:
        payload = load_fixture("chat_history_addon_button_web.json")
        events: list[str] = []
        worker = Mock()
        result = ActionResult(
            "op-0123456789abcdef0123456789abcdef",
            JobStatus.DELETE_QUEUED,
            True,
            True,
            ActionKind.CONFIRM,
        )

        with (
            patch.object(app_module.auth_verifier, "verify", return_value=True),
            patch.object(app_module.gateway, "record_incoming"),
            patch.object(app_module.gateway, "record_outgoing"),
            patch.object(
                app_module.chat_clear_store,
                "apply_handle",
                return_value=result,
            ) as apply_handle,
            patch.object(
                app_module.chat_write_pacer,
                "record_external_write",
                side_effect=lambda _space: events.append("pace"),
            ),
            patch.object(app_module, "history_clear_coordinator", Mock()),
            patch.object(app_module, "history_worker", worker),
            patch.object(app_module.target_store, "remember") as remember,
            patch.object(app_module.orchestrator, "dispatch") as dispatch,
        ):
            worker.wake.side_effect = lambda: events.append("wake")
            response = self.client.post("/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["pace", "wake"])
        body = response.get_json()
        self.assertIn(
            "updateMessageAction",
            body["hostAppDataAction"]["chatDataAction"],
        )
        self.assertNotIn("buttonList", repr(body))
        kwargs = apply_handle.call_args.kwargs
        self.assertEqual(kwargs["requester_name"], "users/fixture-human-001")
        self.assertEqual(kwargs["space_name"], "spaces/fixture-dm-001")
        self.assertEqual(
            kwargs["confirmation_message_name"],
            "spaces/fixture-dm-001/messages/client-jinx-hc-0123456789abcdef0123456789abcdef-01",
        )
        remember.assert_not_called()
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
