from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import websocket

from helpers.providers.kimiclaw_gateway import (
    GatewaySettings,
    KimiclawGatewayError,
    run_gateway_request,
)


def encoded(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


class FakeWebSocket:
    def __init__(self, frames: list[object]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.timeouts: list[float] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self) -> str | bytes | None:
        if not self.frames:
            raise AssertionError("test WebSocket has no more frames")
        frame = self.frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return frame  # type: ignore[return-value]

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


class KimiclawGatewayTests(unittest.TestCase):
    settings = GatewaySettings(url="ws://gateway.test", token="secret-token")

    def run_with_socket(
        self,
        frames: list[object],
        *,
        mode: str = "agent",
    ) -> tuple[object, FakeWebSocket, object]:
        ws = FakeWebSocket(frames)
        with patch(
            "helpers.providers.kimiclaw_gateway.websocket.create_connection",
            return_value=ws,
        ) as connect:
            result = run_gateway_request(
                "hello",
                "agent:main:gchat:abc123",
                mode=mode,  # type: ignore[arg-type]
                settings=self.settings,
                client_timeout_seconds=10,
            )
        return result, ws, connect

    def test_agent_handshake_stream_filtering_and_final_response(self) -> None:
        frames = [
            encoded({"type": "event", "event": "connect.challenge"}),
            encoded({"type": "res", "id": "connect", "ok": True}),
            encoded(
                {
                    "type": "event",
                    "event": "agent",
                    "payload": {
                        "runId": "run-1",
                        "stream": "assistant",
                        "data": {"delta": "ignored-before-accepted"},
                    },
                }
            ),
            encoded(
                {
                    "type": "res",
                    "id": "agent",
                    "ok": True,
                    "payload": {"status": "accepted", "runId": "run-1"},
                }
            ),
            encoded(
                {
                    "type": "event",
                    "event": "agent",
                    "payload": {
                        "runId": "foreign-run",
                        "stream": "assistant",
                        "data": {"delta": "ignored-foreign"},
                    },
                }
            ),
            encoded(
                {
                    "type": "event",
                    "event": "agent",
                    "payload": {
                        "stream": "assistant",
                        "data": {"delta": "ignored-without-run-id"},
                    },
                }
            ),
            encoded(
                {
                    "type": "event",
                    "event": "agent",
                    "payload": {
                        "runId": "run-1",
                        "stream": "assistant",
                        "data": {"delta": "สวัส"},
                    },
                }
            ),
            encoded(
                {
                    "type": "event",
                    "event": "agent",
                    "payload": {
                        "runId": "run-1",
                        "stream": "assistant",
                        "data": {"delta": "ดี", "text": "สวัสดี"},
                    },
                }
            ),
            encoded(
                {
                    "type": "event",
                    "event": "agent",
                    "payload": {
                        "runId": "run-1",
                        "stream": "assistant",
                        "data": {"delta": "!"},
                    },
                }
            ),
            encoded({"type": "res", "id": "agent", "ok": True, "payload": {}}),
        ]

        result, ws, connect = self.run_with_socket(frames)

        self.assertEqual(result.text, "สวัสดี!")
        self.assertEqual(result.run_id, "run-1")
        self.assertTrue(ws.closed)
        connect.assert_called_once_with(
            "ws://gateway.test", timeout=10, suppress_origin=True
        )

        requests = [json.loads(item) for item in ws.sent]
        self.assertEqual([item["method"] for item in requests], ["connect", "agent"])
        self.assertEqual(
            requests[0]["params"]["auth"], {"token": "secret-token"}
        )
        agent_params = requests[1]["params"]
        self.assertEqual(agent_params["message"], "hello")
        self.assertEqual(agent_params["sessionKey"], "agent:main:gchat:abc123")
        self.assertEqual(agent_params["channel"], "kimi-claw")
        self.assertEqual(agent_params["timeout"], 300)
        UUID(agent_params["idempotencyKey"])

    def test_terminal_response_text_overrides_stream(self) -> None:
        result, ws, _ = self.run_with_socket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                encoded(
                    {
                        "type": "res",
                        "id": "agent",
                        "ok": True,
                        "payload": {"status": "accepted", "runId": "run-1"},
                    }
                ),
                encoded(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {
                            "runId": "run-1",
                            "stream": "assistant",
                            "data": {"delta": "partial"},
                        },
                    }
                ),
                encoded(
                    {
                        "type": "res",
                        "id": "agent",
                        "ok": True,
                        "payload": {"finalAssistantVisibleText": "final"},
                    }
                ),
            ]
        )

        self.assertEqual(result.text, "final")
        self.assertTrue(ws.closed)

    def test_command_uses_chat_send_and_returns_fallback(self) -> None:
        result, ws, _ = self.run_with_socket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                encoded({"type": "res", "id": "command", "ok": True}),
            ],
            mode="command",
        )

        request = json.loads(ws.sent[1])
        self.assertEqual(request["method"], "chat.send")
        self.assertEqual(request["params"]["message"], "hello")
        self.assertNotIn("channel", request["params"])
        self.assertIn("hello", result.text)

    def test_duplicate_connect_response_does_not_send_duplicate_agent_run(self) -> None:
        result, ws, _ = self.run_with_socket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                encoded(
                    {
                        "type": "res",
                        "id": "agent",
                        "ok": True,
                        "payload": {"status": "accepted", "runId": "run-1"},
                    }
                ),
                encoded({"type": "res", "id": "agent", "ok": True}),
            ]
        )

        methods = [json.loads(item)["method"] for item in ws.sent]
        self.assertEqual(methods, ["connect", "agent"])
        self.assertEqual(result.run_id, "run-1")

    def test_timeout_after_agent_send_requests_cleanup(self) -> None:
        ws = FakeWebSocket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                websocket.WebSocketTimeoutException("timed out"),
            ]
        )

        with (
            patch(
                "helpers.providers.kimiclaw_gateway.websocket.create_connection",
                return_value=ws,
            ),
            self.assertRaises(KimiclawGatewayError) as raised,
        ):
            run_gateway_request(
                "hello",
                "agent:main:gchat:abc123",
                settings=self.settings,
                client_timeout_seconds=10,
            )

        self.assertEqual(raised.exception.code, "KIMICLAW_TIMEOUT")
        self.assertTrue(raised.exception.should_abort)
        self.assertTrue(ws.closed)

    def test_socket_close_after_agent_send_requests_cleanup(self) -> None:
        ws = FakeWebSocket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                websocket.WebSocketConnectionClosedException("closed"),
            ]
        )

        with (
            patch(
                "helpers.providers.kimiclaw_gateway.websocket.create_connection",
                return_value=ws,
            ),
            self.assertRaises(KimiclawGatewayError) as raised,
        ):
            run_gateway_request(
                "hello", "agent:main:gchat:abc123", settings=self.settings
            )

        self.assertEqual(raised.exception.code, "WS_CLOSED")
        self.assertTrue(raised.exception.should_abort)
        self.assertTrue(ws.closed)

    def test_chat_error_for_own_run_requests_cleanup(self) -> None:
        ws = FakeWebSocket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded({"type": "res", "id": "connect", "ok": True}),
                encoded(
                    {
                        "type": "res",
                        "id": "agent",
                        "ok": True,
                        "payload": {"status": "accepted", "runId": "run-1"},
                    }
                ),
                encoded(
                    {
                        "type": "event",
                        "event": "chat",
                        "payload": {
                            "runId": "run-1",
                            "state": "error",
                            "errorMessage": "engine overloaded",
                        },
                    }
                ),
            ]
        )

        with (
            patch(
                "helpers.providers.kimiclaw_gateway.websocket.create_connection",
                return_value=ws,
            ),
            self.assertRaises(KimiclawGatewayError) as raised,
        ):
            run_gateway_request(
                "hello", "agent:main:gchat:abc123", settings=self.settings
            )

        self.assertEqual(raised.exception.code, "RUN_ERROR")
        self.assertEqual(raised.exception.run_id, "run-1")
        self.assertTrue(raised.exception.should_abort)

    def test_connect_rejection_does_not_request_cleanup(self) -> None:
        ws = FakeWebSocket(
            [
                encoded({"type": "event", "event": "connect.challenge"}),
                encoded(
                    {
                        "type": "res",
                        "id": "connect",
                        "ok": False,
                        "error": {"code": "UNAUTHORIZED", "message": "bad token"},
                    }
                ),
            ]
        )

        with (
            patch(
                "helpers.providers.kimiclaw_gateway.websocket.create_connection",
                return_value=ws,
            ),
            self.assertRaises(KimiclawGatewayError) as raised,
        ):
            run_gateway_request(
                "hello", "agent:main:gchat:abc123", settings=self.settings
            )

        self.assertEqual(raised.exception.code, "UNAUTHORIZED")
        self.assertFalse(raised.exception.should_abort)
        self.assertTrue(ws.closed)

    def test_gateway_settings_support_relay_environment_and_config_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENCLAW_GATEWAY_URL": " ws://relay.test:18790 ",
                "OPENCLAW_GATEWAY_TOKEN": " relay-token ",
            },
            clear=True,
        ):
            settings = GatewaySettings.from_env()

        self.assertEqual(settings, GatewaySettings("ws://relay.test:18790", "relay-token"))

        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "openclaw.json"
            config_file.write_text(
                json.dumps({"gateway": {"auth": {"token": "config-token"}}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "OPENCLAW_GATEWAY_WS_URL": "ws://legacy.test",
                    "OPENCLAW_CONFIG_FILE": str(config_file),
                },
                clear=True,
            ):
                settings = GatewaySettings.from_env()

        self.assertEqual(settings, GatewaySettings("ws://legacy.test", "config-token"))


if __name__ == "__main__":
    unittest.main()
