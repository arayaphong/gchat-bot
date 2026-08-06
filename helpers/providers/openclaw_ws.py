"""Fire-and-forget dispatch of agent runs over the OpenClaw gateway WS RPC.

Unlike ``POST /v1/chat/completions`` — where the gateway ties the run's abort
signal to the HTTP connection (a client read-timeout kills the run mid-flight)
— the WS ``agent`` method returns ``accepted`` immediately and the run keeps
going after the client disconnects. Replies reach the user through the
SessionTrajectoryWatcher, so the caller only needs the dispatch to succeed.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import websocket

GATEWAY_PROTOCOL_VERSION = 4
CONNECT_TIMEOUT_SECONDS = 15
RESPONSE_TIMEOUT_SECONDS = 15
AGENT_RUN_TIMEOUT_SECONDS = 300

_CLIENT_INFO = {
    "id": "gateway-client",
    "displayName": "gchat-bot",
    "version": "0.0.1",
    "platform": "linux",
    "mode": "backend",
}


class OpenclawDispatchError(RuntimeError):
    pass


def http_to_ws_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    url = url.removesuffix("/v1")
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    return url


def _request(ws: Any, req_id: str, method: str, params: dict[str, Any]) -> None:
    ws.send(json.dumps({"type": "req", "id": req_id, "method": method, "params": params}))


def _recv_response(ws: Any, req_id: str, step: str) -> dict[str, Any]:
    deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        frame = json.loads(ws.recv())
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame
    raise OpenclawDispatchError(f"{step}: no response within {RESPONSE_TIMEOUT_SECONDS}s")


def _check_ok(frame: dict[str, Any], step: str) -> dict[str, Any]:
    if frame.get("ok") is False:
        error = frame.get("error") or {}
        code = error.get("code", "UNKNOWN")
        message = error.get("message", "")
        raise OpenclawDispatchError(f"{step} rejected: {code} {message}".strip())
    return frame.get("payload") or {}


def dispatch_agent_run(
    *,
    base_url: str,
    token: str,
    session_key: str,
    channel: str,
    message: str,
) -> str:
    """Submit an agent run and return its runId once the gateway accepts it."""
    ws_url = http_to_ws_url(base_url)
    try:
        # suppress_origin: the gateway treats WS connections carrying an
        # Origin header (websocket-client sends one by default) as browser
        # clients and grants them no operator scopes; node-style clients
        # without Origin get operator.read/write.
        ws = websocket.create_connection(
            ws_url, timeout=CONNECT_TIMEOUT_SECONDS, suppress_origin=True
        )
    except Exception as e:
        raise OpenclawDispatchError(f"connect {ws_url} failed: {e}") from e

    try:
        challenge = json.loads(ws.recv())
        if not (
            challenge.get("type") == "event"
            and challenge.get("event") == "connect.challenge"
        ):
            raise OpenclawDispatchError(
                f"unexpected first frame (expected connect.challenge): {challenge}"
            )

        _request(
            ws,
            "c1",
            "connect",
            {
                "minProtocol": GATEWAY_PROTOCOL_VERSION,
                "maxProtocol": GATEWAY_PROTOCOL_VERSION,
                "client": _CLIENT_INFO,
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "auth": {"token": token},
            },
        )
        _check_ok(_recv_response(ws, "c1", "connect"), "connect")

        _request(
            ws,
            "a1",
            "agent",
            {
                "message": message,
                "sessionKey": session_key,
                "channel": channel,
                "idempotencyKey": str(uuid.uuid4()),
                "timeout": AGENT_RUN_TIMEOUT_SECONDS,
            },
        )
        payload = _check_ok(_recv_response(ws, "a1", "agent"), "agent")
        if payload.get("status") != "accepted":
            raise OpenclawDispatchError(f"agent not accepted: {payload}")
        return str(payload.get("runId") or "")
    except OpenclawDispatchError:
        raise
    except websocket.WebSocketException as e:
        raise OpenclawDispatchError(f"websocket error: {e}") from e
    finally:
        ws.close()
