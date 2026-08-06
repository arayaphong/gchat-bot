from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import websocket

from helpers.providers.openclaw_ws import (
    OpenclawDispatchError,
    dispatch_agent_run,
    http_to_ws_url,
)


class FakeWebSocket:
    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        return json.dumps(self._frames.pop(0))

    def close(self) -> None:
        self.closed = True


CHALLENGE = {"type": "event", "event": "connect.challenge"}
CONNECT_OK = {"type": "res", "id": "c1", "ok": True, "payload": {}}
AGENT_ACCEPTED = {
    "type": "res",
    "id": "a1",
    "ok": True,
    "payload": {"status": "accepted", "runId": "run-xyz"},
}


def run_dispatch(ws: FakeWebSocket) -> str:
    with patch(
        "helpers.providers.openclaw_ws.websocket.create_connection",
        return_value=ws,
    ):
        return dispatch_agent_run(
            base_url="http://127.0.0.1:18789/v1",
            token="gateway-token",
            session_key="agent:main:gchat:c0ffee",
            channel="googlechat",
            message="Alice: hello",
        )


class HttpToWsUrlTests(unittest.TestCase):
    def test_strips_v1_and_converts_scheme(self) -> None:
        self.assertEqual(
            http_to_ws_url("http://127.0.0.1:18789/v1"), "ws://127.0.0.1:18789"
        )
        self.assertEqual(
            http_to_ws_url("https://example.com/v1/"), "wss://example.com"
        )
        self.assertEqual(http_to_ws_url("http://127.0.0.1:18789"), "ws://127.0.0.1:18789")


class DispatchAgentRunTests(unittest.TestCase):
    def test_happy_path_returns_run_id_and_closes(self) -> None:
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED])

        run_id = run_dispatch(ws)

        self.assertEqual(run_id, "run-xyz")
        self.assertTrue(ws.closed)

        connect_req = ws.sent[0]
        self.assertEqual(connect_req["method"], "connect")
        self.assertEqual(connect_req["params"]["auth"], {"token": "gateway-token"})
        self.assertEqual(connect_req["params"]["minProtocol"], 4)
        self.assertEqual(connect_req["params"]["maxProtocol"], 4)

        agent_req = ws.sent[1]
        self.assertEqual(agent_req["method"], "agent")
        self.assertEqual(agent_req["params"]["message"], "Alice: hello")
        self.assertEqual(agent_req["params"]["sessionKey"], "agent:main:gchat:c0ffee")
        self.assertEqual(agent_req["params"]["channel"], "googlechat")
        self.assertTrue(agent_req["params"]["idempotencyKey"])

    def test_skips_unrelated_frames_while_waiting(self) -> None:
        ws = FakeWebSocket(
            [
                CHALLENGE,
                {"type": "event", "event": "agent", "payload": {}},
                CONNECT_OK,
                {"type": "res", "id": "other", "ok": True, "payload": {}},
                AGENT_ACCEPTED,
            ]
        )

        self.assertEqual(run_dispatch(ws), "run-xyz")

    def test_connect_rejected_raises(self) -> None:
        ws = FakeWebSocket(
            [
                CHALLENGE,
                {
                    "type": "res",
                    "id": "c1",
                    "ok": False,
                    "error": {"code": "AUTH", "message": "bad token"},
                },
            ]
        )

        with self.assertRaisesRegex(OpenclawDispatchError, "AUTH bad token"):
            run_dispatch(ws)
        self.assertTrue(ws.closed)

    def test_agent_rejected_raises(self) -> None:
        ws = FakeWebSocket(
            [
                CHALLENGE,
                CONNECT_OK,
                {
                    "type": "res",
                    "id": "a1",
                    "ok": False,
                    "error": {"code": "INVALID_REQUEST", "message": "missing scope"},
                },
            ]
        )

        with self.assertRaisesRegex(OpenclawDispatchError, "INVALID_REQUEST"):
            run_dispatch(ws)

    def test_unexpected_first_frame_raises(self) -> None:
        ws = FakeWebSocket([{"type": "res", "id": "x", "ok": True, "payload": {}}])

        with self.assertRaisesRegex(OpenclawDispatchError, "connect.challenge"):
            run_dispatch(ws)

    def test_socket_timeout_while_waiting_is_wrapped_and_closes(self) -> None:
        class TimingOutWebSocket(FakeWebSocket):
            def recv(self) -> str:
                if not self._frames:
                    raise websocket.WebSocketTimeoutException("timed out")
                return super().recv()

        ws = TimingOutWebSocket([CHALLENGE])

        with self.assertRaisesRegex(OpenclawDispatchError, "websocket error"):
            run_dispatch(ws)
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
