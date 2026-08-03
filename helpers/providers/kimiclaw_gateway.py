from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import websocket

GatewayMode = Literal["agent", "command"]

DEFAULT_GATEWAY_URL = "ws://127.0.0.1:18789"
AGENT_TIMEOUT_SECONDS = 300
CLIENT_TIMEOUT_SECONDS = 320
CONNECT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class GatewaySettings:
    url: str
    token: str

    @staticmethod
    def from_env() -> GatewaySettings:
        url = (
            os.environ.get("OPENCLAW_GATEWAY_URL")
            or os.environ.get("OPENCLAW_GATEWAY_WS_URL")
            or DEFAULT_GATEWAY_URL
        ).strip()
        token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()

        if not token:
            config_file = Path(
                os.environ.get(
                    "OPENCLAW_CONFIG_FILE",
                    str(Path.home() / ".openclaw" / "openclaw.json"),
                )
            ).expanduser()
            config = json.loads(config_file.read_text(encoding="utf-8"))
            configured_token = config.get("gateway", {}).get("auth", {}).get("token")
            if isinstance(configured_token, str):
                token = configured_token.strip()

        if not url:
            raise ValueError("OpenClaw gateway URL must not be empty")
        if not token:
            raise ValueError("OpenClaw gateway token is missing")
        return GatewaySettings(url=url, token=token)


@dataclass(frozen=True)
class GatewayResult:
    text: str
    run_id: str | None


class KimiclawGatewayError(RuntimeError):
    def __init__(
        self,
        where: str,
        code: str,
        message: str,
        run_id: str | None = None,
        *,
        should_abort: bool = False,
    ) -> None:
        self.where = where
        self.code = code
        self.message = message
        self.run_id = run_id
        self.should_abort = should_abort
        super().__init__(f"{code}: {message}")


def _text(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


def _terminal_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    message_text = message.get("text") if isinstance(message, dict) else ""
    return (
        _text(payload.get("finalAssistantVisibleText"))
        or _text(payload.get("text"))
        or _text(message_text)
    )


def _frame_error(
    frame: dict[str, Any], default_code: str, default_message: str
) -> tuple[str, str]:
    error = frame.get("error")
    if not isinstance(error, dict):
        return default_code, default_message
    return (
        _text(error.get("code")) or default_code,
        _text(error.get("message")) or default_message,
    )


def run_gateway_request(
    message: str,
    session_key: str,
    mode: GatewayMode = "agent",
    *,
    settings: GatewaySettings | None = None,
    client_timeout_seconds: float = CLIENT_TIMEOUT_SECONDS,
) -> GatewayResult:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError("session_key must be a non-empty string")
    if mode not in {"agent", "command"}:
        raise ValueError(f"unsupported Kimiclaw mode: {mode!r}")
    if client_timeout_seconds <= 0:
        raise ValueError("client_timeout_seconds must be positive")

    gateway_settings = settings or GatewaySettings.from_env()
    deadline = time.monotonic() + client_timeout_seconds
    ws: websocket.WebSocket | None = None
    run_id: str | None = None
    stream_text = ""
    connect_requested = False
    operation_sent = False
    agent_request_sent = False

    def fail(where: str, code: str, failure_message: str) -> KimiclawGatewayError:
        return KimiclawGatewayError(
            where,
            code,
            failure_message,
            run_id,
            should_abort=agent_request_sent,
        )

    try:
        ws = websocket.create_connection(
            gateway_settings.url,
            timeout=min(CONNECT_TIMEOUT_SECONDS, client_timeout_seconds),
            suppress_origin=True,
        )

        def request(request_id: str, method: str, params: dict[str, Any]) -> None:
            assert ws is not None
            ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    },
                    ensure_ascii=False,
                )
            )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise fail(
                    "timeout",
                    "KIMICLAW_TIMEOUT",
                    f"no final response in {client_timeout_seconds:g}s",
                )
            ws.settimeout(remaining)

            try:
                raw_frame = ws.recv()
            except websocket.WebSocketConnectionClosedException as e:
                raise fail(
                    "ws",
                    "WS_CLOSED",
                    "WebSocket closed before completion",
                ) from e
            except (TimeoutError, websocket.WebSocketTimeoutException) as e:
                raise fail(
                    "timeout",
                    "KIMICLAW_TIMEOUT",
                    f"no final response in {client_timeout_seconds:g}s",
                ) from e

            if raw_frame is None or raw_frame == "":
                raise fail(
                    "ws",
                    "WS_CLOSED",
                    "WebSocket closed before completion",
                )
            if isinstance(raw_frame, bytes):
                raw_frame = raw_frame.decode("utf-8", errors="replace")

            try:
                frame = json.loads(raw_frame)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue

            if frame.get("type") == "event":
                if frame.get("event") == "connect.challenge":
                    if not connect_requested:
                        connect_requested = True
                        request(
                            "connect",
                            "connect",
                            {
                                "minProtocol": 4,
                                "maxProtocol": 4,
                                "client": {
                                    "id": "gateway-client",
                                    "displayName": "gchat-kimiclaw-provider",
                                    "version": "1.0.0",
                                    "platform": "linux",
                                    "mode": "backend",
                                },
                                "role": "operator",
                                "scopes": ["operator.read", "operator.write"],
                                "auth": {"token": gateway_settings.token},
                            },
                        )
                    continue

                payload = frame.get("payload")
                if (
                    not run_id
                    or not isinstance(payload, dict)
                    or payload.get("runId") != run_id
                ):
                    continue

                if frame.get("event") == "agent":
                    data = payload.get("data")
                    if not isinstance(data, dict):
                        data = {}
                    if payload.get("stream") == "assistant":
                        accumulated = _text(data.get("text"))
                        delta = _text(data.get("delta"))
                        if accumulated:
                            stream_text = accumulated
                        elif delta:
                            stream_text += delta
                    elif (
                        payload.get("stream") == "lifecycle"
                        and data.get("phase") == "end"
                        and data.get("aborted")
                    ):
                        raise fail(
                            "agent-event",
                            "RUN_ABORTED",
                            "Kimiclaw run was aborted",
                        )
                    continue

                if frame.get("event") == "chat":
                    final_text = _terminal_text(payload)
                    if final_text:
                        stream_text = final_text
                    if payload.get("state") == "error":
                        raise fail(
                            "chat-event",
                            "RUN_ERROR",
                            _text(payload.get("errorMessage"))
                            or "unknown run error",
                        )
                continue

            if frame.get("type") != "res":
                continue

            response_id = frame.get("id")
            if response_id == "connect":
                if not connect_requested or operation_sent:
                    continue
                if frame.get("ok") is False:
                    code, error_message = _frame_error(
                        frame, "CONNECT_ERROR", "gateway connection was rejected"
                    )
                    raise fail("connect", code, error_message)

                if mode == "command":
                    operation_sent = True
                    request(
                        "command",
                        "chat.send",
                        {
                            "message": message,
                            "sessionKey": session_key.strip(),
                            "idempotencyKey": str(uuid4()),
                        },
                    )
                else:
                    operation_sent = True
                    agent_request_sent = True
                    request(
                        "agent",
                        "agent",
                        {
                            "message": message,
                            "sessionKey": session_key.strip(),
                            "channel": "kimi-claw",
                            "idempotencyKey": str(uuid4()),
                            "timeout": AGENT_TIMEOUT_SECONDS,
                        },
                    )
                continue

            if response_id == "command":
                if not operation_sent or mode != "command":
                    continue
                if frame.get("ok") is False:
                    code, error_message = _frame_error(
                        frame, "COMMAND_ERROR", "chat command was rejected"
                    )
                    raise fail("command-res", code, error_message)
                return GatewayResult(
                    text=_terminal_text(frame.get("payload"))
                    or f"✅ ส่งคำสั่ง {message} แล้ว",
                    run_id=None,
                )

            if response_id != "agent":
                continue
            if not operation_sent or mode != "agent":
                continue

            if frame.get("ok") is False:
                code, error_message = _frame_error(
                    frame, "AGENT_ERROR", "agent request failed"
                )
                raise fail("agent-res", code, error_message)

            payload = frame.get("payload")
            if isinstance(payload, dict) and payload.get("status") == "accepted":
                accepted_run_id = payload.get("runId")
                if not isinstance(accepted_run_id, str) or not accepted_run_id:
                    raise fail(
                        "agent-res",
                        "MISSING_RUN_ID",
                        "accepted response has no runId",
                    )
                if run_id and accepted_run_id != run_id:
                    raise fail(
                        "agent-res",
                        "RUN_ID_CHANGED",
                        "accepted response changed runId",
                    )
                run_id = accepted_run_id
                continue

            final_text = _terminal_text(payload)
            if final_text:
                stream_text = final_text
            return GatewayResult(text=stream_text, run_id=run_id)
    except KimiclawGatewayError:
        raise
    except (OSError, websocket.WebSocketException) as e:
        raise fail("ws", "WS_ERROR", str(e) or "WebSocket error") from e
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001, S110
                pass
