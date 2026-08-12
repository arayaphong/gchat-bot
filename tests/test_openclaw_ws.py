from __future__ import annotations

import json
import unittest
import uuid
from unittest.mock import Mock, call, patch

import websocket

from helpers.providers.openclaw_ws import (
    OpenclawDispatchError,
    OpenclawRunCancelled,
    dispatch_agent_run,
    http_to_ws_url,
)


class FakeWebSocket:
    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.timeouts: list[float] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        return json.dumps(self._frames.pop(0))

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


class DisconnectWhenEmptyWebSocket(FakeWebSocket):
    def recv(self) -> str:
        if not self._frames:
            raise websocket.WebSocketConnectionClosedException("connection lost")
        return super().recv()


CHALLENGE = {"type": "event", "event": "connect.challenge"}
CONNECT_OK = {"type": "res", "id": "c1", "ok": True, "payload": {}}
AGENT_ACCEPTED = {
    "type": "res",
    "id": "a1",
    "ok": True,
    "payload": {"status": "accepted", "runId": "gchat-message-key"},
}
WAIT_OK = {
    "type": "res",
    "id": "w1",
    "ok": True,
    "payload": {"status": "ok", "startedAt": 1, "endedAt": 2},
}


def run_dispatch(
    ws: FakeWebSocket,
    *,
    idempotency_key: str | None = "gchat-message-key",
    resume_run_id: str | None = None,
    on_run_accepted: object = None,
) -> str:
    with patch(
        "helpers.providers.openclaw_ws.websocket.create_connection",
        return_value=ws,
    ) as create:
        options: dict[str, object] = {}
        if resume_run_id is not None:
            options["resume_run_id"] = resume_run_id
        if on_run_accepted is not None:
            options["on_run_accepted"] = on_run_accepted
        result = dispatch_agent_run(
            base_url="http://127.0.0.1:18789/v1",
            token="gateway-token",
            session_key="agent:main:gchat:c0ffee",
            channel="googlechat",
            message="Alice: hello",
            idempotency_key=idempotency_key,
            **options,
        )
    create.assert_called_once_with(
        "ws://127.0.0.1:18789", timeout=15, suppress_origin=True
    )
    return result


def run_dispatch_with_reconnect(sockets: list[FakeWebSocket]) -> tuple[str, Mock]:
    with (
        patch(
            "helpers.providers.openclaw_ws.websocket.create_connection",
            side_effect=sockets,
        ) as create,
        patch("helpers.providers.openclaw_ws.time.sleep") as sleep,
    ):
        result = dispatch_agent_run(
            base_url="http://127.0.0.1:18789/v1",
            token="gateway-token",
            session_key="agent:main:gchat:c0ffee",
            channel="googlechat",
            message="Alice: hello",
            idempotency_key="gchat-message-key",
        )
    self_calls = create.call_args_list
    expected_call = call("ws://127.0.0.1:18789", timeout=15, suppress_origin=True)
    if any(call != expected_call for call in self_calls):
        raise AssertionError(f"unexpected create_connection calls: {self_calls}")
    return result, sleep


class HttpToWsUrlTests(unittest.TestCase):
    def test_strips_v1_and_converts_scheme(self) -> None:
        self.assertEqual(
            http_to_ws_url("http://127.0.0.1:18789/v1"), "ws://127.0.0.1:18789"
        )
        self.assertEqual(http_to_ws_url("https://example.com/v1/"), "wss://example.com")
        self.assertEqual(
            http_to_ws_url("http://127.0.0.1:18789"), "ws://127.0.0.1:18789"
        )


class DispatchAgentRunTests(unittest.TestCase):
    def test_happy_path_returns_run_id_and_closes(self) -> None:
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, WAIT_OK])

        run_id = run_dispatch(ws)

        self.assertEqual(run_id, "gchat-message-key")
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
        self.assertEqual(agent_req["params"]["idempotencyKey"], "gchat-message-key")

        wait_req = ws.sent[2]
        self.assertEqual(wait_req["method"], "agent.wait")
        self.assertEqual(
            wait_req["params"],
            {"runId": "gchat-message-key", "timeoutMs": 10_000},
        )
        self.assertTrue(ws.timeouts)
        self.assertGreater(ws.timeouts[-1], 10)

    def test_skips_unrelated_frames_while_waiting(self) -> None:
        ws = FakeWebSocket(
            [
                CHALLENGE,
                {"type": "event", "event": "agent", "payload": {}},
                CONNECT_OK,
                {"type": "res", "id": "other", "ok": True, "payload": {}},
                AGENT_ACCEPTED,
                {"type": "event", "event": "agent", "payload": {}},
                WAIT_OK,
            ]
        )

        self.assertEqual(run_dispatch(ws), "gchat-message-key")

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

    def test_in_flight_acceptance_is_supported(self) -> None:
        in_flight = {
            "type": "res",
            "id": "a1",
            "ok": True,
            "payload": {"status": "in_flight", "runId": "gchat-message-key"},
        }
        wait_ok = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 10},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, in_flight, wait_ok])

        self.assertEqual(run_dispatch(ws), "gchat-message-key")

    def test_run_id_must_match_the_idempotency_key(self) -> None:
        mismatched = {
            "type": "res",
            "id": "a1",
            "ok": True,
            "payload": {"status": "accepted", "runId": "unexpected-run"},
        }
        terminal = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 42},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, mismatched, terminal])

        with self.assertRaisesRegex(OpenclawDispatchError, "does not match"):
            run_dispatch(ws)

        self.assertEqual(ws.sent[-1]["method"], "agent.wait")
        self.assertEqual(ws.sent[-1]["params"]["runId"], "unexpected-run")

    def test_resume_run_skips_agent_and_waits_for_terminal_result(self) -> None:
        resumed_wait = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 10},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, resumed_wait])
        accepted = Mock()

        self.assertEqual(
            run_dispatch(
                ws,
                resume_run_id="run-recovered",
                on_run_accepted=accepted,
            ),
            "run-recovered",
        )

        self.assertEqual(
            [request["method"] for request in ws.sent],
            ["connect", "agent.wait"],
        )
        self.assertEqual(ws.sent[1]["params"]["runId"], "run-recovered")
        accepted.assert_called_once_with("run-recovered")

    def test_resume_initial_connect_failure_is_bounded_by_safety_deadline(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = FakeClock()
        with (
            patch(
                "helpers.providers.openclaw_ws._open_authenticated_connection",
                side_effect=OpenclawDispatchError("gateway unavailable"),
            ) as connect,
            patch("helpers.providers.openclaw_ws.AGENT_RUN_TIMEOUT_SECONDS", 0.1),
            patch("helpers.providers.openclaw_ws.RUN_TERMINATION_GRACE_SECONDS", 0),
            patch(
                "helpers.providers.openclaw_ws.time.monotonic",
                side_effect=clock.monotonic,
            ),
            patch(
                "helpers.providers.openclaw_ws.time.sleep",
                side_effect=clock.sleep,
            ),
            self.assertRaisesRegex(
                OpenclawDispatchError,
                "could not confirm terminal state for run run-recovered",
            ),
        ):
            dispatch_agent_run(
                base_url="http://127.0.0.1:18789/v1",
                token="gateway-token",
                session_key="agent:main:gchat:c0ffee",
                channel="googlechat",
                message="Alice: hello",
                resume_run_id="run-recovered",
            )

        connect.assert_called_once()
        self.assertEqual(clock.now, 0.1)

    def test_acceptance_without_run_id_is_recovered_with_same_key(self) -> None:
        accepted_without_run = {
            "type": "res",
            "id": "a1",
            "ok": True,
            "payload": {"status": "accepted", "runId": "  "},
        }
        first = FakeWebSocket([CHALLENGE, CONNECT_OK, accepted_without_run])
        recovered = FakeWebSocket(
            [
                CHALLENGE,
                CONNECT_OK,
                {
                    "type": "res",
                    "id": "a1",
                    "ok": True,
                    "payload": {"status": "in_flight", "runId": "gchat-message-key"},
                },
                WAIT_OK,
            ]
        )

        result, _sleep = run_dispatch_with_reconnect([first, recovered])

        self.assertEqual(result, "gchat-message-key")
        self.assertEqual(
            first.sent[1]["params"]["idempotencyKey"],
            recovered.sent[1]["params"]["idempotencyKey"],
        )

    def test_wait_pending_and_nonterminal_timeout_are_polled_again(self) -> None:
        wait_pending = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "pending"},
        }
        wait_poll_timeout = {
            "type": "res",
            "id": "w2",
            "ok": True,
            "payload": {"status": "timeout"},
        }
        wait_ok = {
            "type": "res",
            "id": "w3",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 42},
        }
        ws = FakeWebSocket(
            [
                CHALLENGE,
                CONNECT_OK,
                AGENT_ACCEPTED,
                wait_pending,
                wait_poll_timeout,
                wait_ok,
            ]
        )

        self.assertEqual(run_dispatch(ws), "gchat-message-key")
        wait_requests = [
            request for request in ws.sent if request["method"] == "agent.wait"
        ]
        self.assertEqual(
            [request["id"] for request in wait_requests], ["w1", "w2", "w3"]
        )
        self.assertTrue(
            all(
                request["params"] == {"runId": "gchat-message-key", "timeoutMs": 10_000}
                for request in wait_requests
            )
        )

    def test_terminal_wait_error_raises_useful_reason(self) -> None:
        wait_error = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {
                "status": "error",
                "endedAt": 42,
                "error": {"code": "MODEL_FAILED", "message": "provider unavailable"},
            },
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, wait_error])

        with self.assertRaisesRegex(
            OpenclawDispatchError, "MODEL_FAILED provider unavailable"
        ):
            run_dispatch(ws)
        self.assertTrue(ws.closed)

    def test_terminal_abort_reasons_raise_typed_cancellation(self) -> None:
        for stop_reason in ("aborted", "rpc", "stop"):
            with self.subTest(stop_reason=stop_reason):
                wait_aborted = {
                    "type": "res",
                    "id": "w1",
                    "ok": True,
                    "payload": {
                        "status": "error",
                        "stopReason": stop_reason,
                        "endedAt": 42,
                        "error": {"message": "aborted by sessions.abort"},
                    },
                }
                ws = FakeWebSocket(
                    [CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, wait_aborted]
                )

                with self.assertRaises(OpenclawRunCancelled) as raised:
                    run_dispatch(ws)

                self.assertEqual(raised.exception.run_id, "gchat-message-key")
                self.assertEqual(raised.exception.reason, stop_reason)
                self.assertTrue(ws.closed)

    def test_successful_stop_reason_is_not_misclassified_as_abort(self) -> None:
        wait_ok_with_stop = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "ok", "stopReason": "stop", "endedAt": 42},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, wait_ok_with_stop])

        self.assertEqual(run_dispatch(ws), "gchat-message-key")

    def test_terminal_wait_timeout_raises(self) -> None:
        wait_timeout = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "timeout", "endedAt": 42},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, wait_timeout])

        with self.assertRaisesRegex(OpenclawDispatchError, "timed out at 42"):
            run_dispatch(ws)

    def test_wait_rpc_rejection_is_not_treated_as_terminal(self) -> None:
        wait_rejected = {
            "type": "res",
            "id": "w1",
            "ok": False,
            "error": {"code": "NOT_FOUND", "message": "unknown run"},
        }
        first = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, wait_rejected])
        recovered_wait = {
            "type": "res",
            "id": "w2",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 42},
        }
        recovered = FakeWebSocket([CHALLENGE, CONNECT_OK, recovered_wait])

        result, _sleep = run_dispatch_with_reconnect([first, recovered])

        self.assertEqual(result, "gchat-message-key")
        self.assertNotIn("agent", [request["method"] for request in recovered.sent])

    def test_not_found_requires_confirmation_on_a_fresh_connection(self) -> None:
        first_not_found = {
            "type": "res",
            "id": "w1",
            "ok": False,
            "error": {"code": "NOT_FOUND", "message": "unknown run"},
        }
        second_not_found = {
            "type": "res",
            "id": "w2",
            "ok": False,
            "error": {"code": "NOT_FOUND", "message": "unknown run"},
        }
        first = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, first_not_found])
        confirmation = FakeWebSocket([CHALLENGE, CONNECT_OK, second_not_found])

        with (
            patch(
                "helpers.providers.openclaw_ws.websocket.create_connection",
                side_effect=[first, confirmation],
            ) as create,
            patch("helpers.providers.openclaw_ws.time.sleep") as sleep,
            self.assertRaisesRegex(OpenclawDispatchError, "NOT_FOUND confirmed on 2"),
        ):
            dispatch_agent_run(
                base_url="http://127.0.0.1:18789/v1",
                token="gateway-token",
                session_key="agent:main:gchat:c0ffee",
                channel="googlechat",
                message="Alice: hello",
                idempotency_key="gchat-message-key",
            )

        self.assertEqual(create.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertTrue(first.closed)
        self.assertTrue(confirmation.closed)

    def test_other_wait_rejection_holds_until_run_safety_deadline(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = FakeClock()
        wait_rejected = {
            "type": "res",
            "id": "w1",
            "ok": False,
            "error": {"code": "INVALID_REQUEST", "message": "bad wait params"},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED, wait_rejected])

        with (
            patch(
                "helpers.providers.openclaw_ws.websocket.create_connection",
                return_value=ws,
            ) as create,
            patch("helpers.providers.openclaw_ws.AGENT_RUN_TIMEOUT_SECONDS", 0.1),
            patch("helpers.providers.openclaw_ws.RUN_TERMINATION_GRACE_SECONDS", 0),
            patch(
                "helpers.providers.openclaw_ws.time.monotonic",
                side_effect=clock.monotonic,
            ),
            patch(
                "helpers.providers.openclaw_ws.time.sleep",
                side_effect=clock.sleep,
            ),
            self.assertRaisesRegex(
                OpenclawDispatchError,
                "could not confirm terminal state.*0.1s timeout plus 0s grace",
            ),
        ):
            dispatch_agent_run(
                base_url="http://127.0.0.1:18789/v1",
                token="gateway-token",
                session_key="agent:main:gchat:c0ffee",
                channel="googlechat",
                message="Alice: hello",
                idempotency_key="gchat-message-key",
            )

        create.assert_called_once()
        self.assertEqual(clock.now, 0.1)
        self.assertTrue(ws.closed)

    def test_disconnect_after_agent_send_redispatches_same_logical_run(self) -> None:
        first = DisconnectWhenEmptyWebSocket([CHALLENGE, CONNECT_OK])
        recovered_agent = {
            "type": "res",
            "id": "a1",
            "ok": True,
            "payload": {"status": "in_flight", "runId": "gchat-message-key"},
        }
        recovered = FakeWebSocket([CHALLENGE, CONNECT_OK, recovered_agent, WAIT_OK])

        result, sleep = run_dispatch_with_reconnect([first, recovered])

        self.assertEqual(result, "gchat-message-key")
        agent_requests = [first.sent[1], recovered.sent[1]]
        self.assertEqual(
            {request["params"]["idempotencyKey"] for request in agent_requests},
            {"gchat-message-key"},
        )
        self.assertEqual(recovered.sent[2]["method"], "agent.wait")
        sleep.assert_called_once_with(0.25)
        self.assertTrue(first.closed)
        self.assertTrue(recovered.closed)

    def test_rejection_after_ambiguous_send_does_not_release_early(self) -> None:
        agent_rejected = {
            "type": "res",
            "id": "a1",
            "ok": False,
            "error": {"code": "INVALID_REQUEST", "message": "cannot recover ack"},
        }
        first = DisconnectWhenEmptyWebSocket([CHALLENGE, CONNECT_OK])
        second = FakeWebSocket([CHALLENGE, CONNECT_OK, agent_rejected])
        third = FakeWebSocket([CHALLENGE, CONNECT_OK, agent_rejected])
        final_connection = FakeWebSocket([CHALLENGE, CONNECT_OK])

        with (
            patch(
                "helpers.providers.openclaw_ws.websocket.create_connection",
                side_effect=[first, second, third, final_connection],
            ),
            patch("helpers.providers.openclaw_ws.time.sleep"),
            patch(
                "helpers.providers.openclaw_ws._hold_until_deadline"
            ) as hold_until_safe,
            self.assertRaisesRegex(OpenclawDispatchError, "ambiguously accepted run"),
        ):
            dispatch_agent_run(
                base_url="http://127.0.0.1:18789/v1",
                token="gateway-token",
                session_key="agent:main:gchat:c0ffee",
                channel="googlechat",
                message="Alice: hello",
                idempotency_key="gchat-message-key",
            )

        hold_until_safe.assert_called_once()
        agent_requests = [ws.sent[1] for ws in (first, second, third)]
        self.assertEqual(
            {request["params"]["idempotencyKey"] for request in agent_requests},
            {"gchat-message-key"},
        )

    def test_disconnect_during_wait_resumes_wait_without_redispatch(self) -> None:
        first = DisconnectWhenEmptyWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED])
        recovered_wait = {
            "type": "res",
            "id": "w2",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 42},
        }
        recovered = FakeWebSocket([CHALLENGE, CONNECT_OK, recovered_wait])

        result, sleep = run_dispatch_with_reconnect([first, recovered])

        self.assertEqual(result, "gchat-message-key")
        self.assertEqual(
            sum(
                request["method"] == "agent"
                for ws in (first, recovered)
                for request in ws.sent
            ),
            1,
        )
        self.assertEqual(recovered.sent[1]["method"], "agent.wait")
        self.assertEqual(recovered.sent[1]["params"]["runId"], "gchat-message-key")
        sleep.assert_called_once_with(0.25)

    def test_socket_timeout_update_failure_during_wait_reconnects(self) -> None:
        class TimeoutUpdateFailureWebSocket(FakeWebSocket):
            def settimeout(self, timeout: float) -> None:
                super().settimeout(timeout)
                if len(self.timeouts) == 3:
                    raise OSError("socket already closed")

        first = TimeoutUpdateFailureWebSocket([CHALLENGE, CONNECT_OK, AGENT_ACCEPTED])
        recovered_wait = {
            "type": "res",
            "id": "w2",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 42},
        }
        recovered = FakeWebSocket([CHALLENGE, CONNECT_OK, recovered_wait])

        result, _sleep = run_dispatch_with_reconnect([first, recovered])

        self.assertEqual(result, "gchat-message-key")
        self.assertEqual(recovered.sent[1]["method"], "agent.wait")
        self.assertNotIn("agent", [request["method"] for request in recovered.sent])

    def test_legacy_missing_idempotency_key_gets_random_fallback(self) -> None:
        generated_key = "11111111-1111-4111-8111-111111111111"
        accepted = {
            "type": "res",
            "id": "a1",
            "ok": True,
            "payload": {"status": "accepted", "runId": generated_key},
        }
        wait_ok = {
            "type": "res",
            "id": "w1",
            "ok": True,
            "payload": {"status": "ok", "endedAt": 42},
        }
        ws = FakeWebSocket([CHALLENGE, CONNECT_OK, accepted, wait_ok])

        with patch(
            "helpers.providers.openclaw_ws.uuid.uuid4",
            return_value=uuid.UUID(generated_key),
        ):
            self.assertEqual(run_dispatch(ws, idempotency_key=None), generated_key)

        generated = ws.sent[1]["params"]["idempotencyKey"]
        self.assertEqual(generated, generated_key)

    def test_blank_idempotency_key_is_rejected_before_connect(self) -> None:
        ws = FakeWebSocket([])
        with (
            patch(
                "helpers.providers.openclaw_ws.websocket.create_connection",
                return_value=ws,
            ) as create,
            self.assertRaisesRegex(ValueError, "nonblank"),
        ):
            dispatch_agent_run(
                base_url="http://127.0.0.1:18789/v1",
                token="gateway-token",
                session_key="agent:main:gchat:c0ffee",
                channel="googlechat",
                message="Alice: hello",
                idempotency_key="   ",
            )
        create.assert_not_called()

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

        with self.assertRaisesRegex(OpenclawDispatchError, "no response within 15s"):
            run_dispatch(ws)
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
