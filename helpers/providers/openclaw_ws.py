"""Dispatch agent runs over the OpenClaw gateway WS RPC.

Unlike ``POST /v1/chat/completions`` — where the gateway ties the run's abort
signal to the HTTP connection (a client read-timeout kills the run mid-flight)
— the WS ``agent`` method returns ``accepted`` immediately.  The connection is
kept open and ``agent.wait`` is polled until the run reaches a terminal state,
so callers do not release their per-session processing lease while the agent is
still running. Replies still reach the user through SessionTrajectoryWatcher.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket

GATEWAY_PROTOCOL_VERSION = 4
CONNECT_TIMEOUT_SECONDS = 15
RESPONSE_TIMEOUT_SECONDS = 15
AGENT_RUN_TIMEOUT_SECONDS = 300
AGENT_WAIT_POLL_MILLISECONDS = 10_000
AGENT_WAIT_RESPONSE_TIMEOUT_SECONDS = (
    RESPONSE_TIMEOUT_SECONDS + AGENT_WAIT_POLL_MILLISECONDS / 1000
)
RECONNECT_INITIAL_DELAY_SECONDS = 0.25
RECONNECT_MAX_DELAY_SECONDS = 5.0
RUN_TERMINATION_GRACE_SECONDS = 30.0
MAX_AMBIGUOUS_AGENT_ATTEMPTS = 3
DEFINITIVE_MISSING_CONFIRMATIONS = 2
_DEFINITIVE_MISSING_CODES = frozenset({"NOT_FOUND", "RUN_NOT_FOUND"})
_CANCELLATION_STOP_REASONS = frozenset(
    {"aborted", "cancelled", "canceled", "rpc", "stop"}
)

_CLIENT_INFO = {
    "id": "gateway-client",
    "displayName": "gchat-bot",
    "version": "0.0.1",
    "platform": "linux",
    "mode": "backend",
}


class OpenclawDispatchError(RuntimeError):
    pass


class OpenclawRunCancelled(OpenclawDispatchError):
    """The run reached a terminal cancellation requested outside this worker."""

    def __init__(self, run_id: str, reason: str = "aborted") -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"agent run {run_id} was cancelled: {reason}")


class _RetryableTransportError(OpenclawDispatchError):
    """The peer may have processed the last request before transport failed."""


class _TerminalRunError(OpenclawDispatchError):
    """OpenClaw confirmed that an accepted run ended unsuccessfully."""


class _RpcRejectedError(OpenclawDispatchError):
    """The Gateway definitively rejected an RPC request."""

    def __init__(self, step: str, code: str, message: str) -> None:
        self.step = step
        self.code = code.strip().upper() or "UNKNOWN"
        self.message = message.strip()
        super().__init__(f"{step} rejected: {self.code} {self.message}".strip())


class _ActiveRunDeadlineExceeded(OpenclawDispatchError):
    """The configured maximum run lifetime and confirmation grace elapsed."""


def http_to_ws_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    url = url.removesuffix("/v1")
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    return url


def _request(ws: Any, req_id: str, method: str, params: dict[str, Any]) -> None:
    try:
        ws.send(
            json.dumps(
                {"type": "req", "id": req_id, "method": method, "params": params}
            )
        )
    except (OSError, websocket.WebSocketException) as e:
        raise _RetryableTransportError(f"{method}: websocket send failed: {e}") from e


def _recv_json(ws: Any, step: str) -> dict[str, Any]:
    try:
        raw_frame = ws.recv()
    except (OSError, websocket.WebSocketException) as e:
        raise _RetryableTransportError(f"{step}: websocket receive failed: {e}") from e
    if not raw_frame:
        raise _RetryableTransportError(f"{step}: websocket closed without a frame")
    try:
        frame = json.loads(raw_frame)
    except (TypeError, json.JSONDecodeError) as e:
        raise OpenclawDispatchError(f"{step}: invalid JSON frame") from e
    if not isinstance(frame, dict):
        raise OpenclawDispatchError(f"{step}: expected an object frame")
    return frame


def _recv_response(
    ws: Any,
    req_id: str,
    step: str,
    *,
    timeout_seconds: float = RESPONSE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        # The connection-level timeout used during the handshake must not make
        # a long-polling agent.wait call fail before its server-side timeout.
        settimeout = getattr(ws, "settimeout", None)
        if callable(settimeout):
            try:
                settimeout(remaining)
            except (OSError, websocket.WebSocketException) as e:
                raise _RetryableTransportError(
                    f"{step}: cannot update websocket timeout: {e}"
                ) from e
        try:
            frame = _recv_json(ws, step)
        except websocket.WebSocketTimeoutException as e:
            raise _RetryableTransportError(
                f"{step}: no response within {timeout_seconds:g}s"
            ) from e
        except _RetryableTransportError as e:
            if isinstance(e.__cause__, websocket.WebSocketTimeoutException):
                raise _RetryableTransportError(
                    f"{step}: no response within {timeout_seconds:g}s"
                ) from e
            raise
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame
    raise _RetryableTransportError(f"{step}: no response within {timeout_seconds:g}s")


def _check_ok(frame: dict[str, Any], step: str) -> dict[str, Any]:
    if frame.get("ok") is False:
        error = frame.get("error") or {}
        if isinstance(error, dict):
            code = error.get("code", "UNKNOWN")
            message = error.get("message", "")
        else:
            code = "UNKNOWN"
            message = str(error)
        raise _RpcRejectedError(step, str(code), str(message))
    payload = frame.get("payload") or {}
    if not isinstance(payload, dict):
        raise OpenclawDispatchError(f"{step}: expected an object payload")
    return payload


def _close_quietly(ws: Any | None) -> None:
    if ws is None:
        return
    try:
        ws.close()
    except (OSError, websocket.WebSocketException):
        return


def _open_authenticated_connection(ws_url: str, token: str) -> Any:
    try:
        # suppress_origin: the gateway treats WS connections carrying an
        # Origin header (websocket-client sends one by default) as browser
        # clients and grants them no operator scopes; node-style clients
        # without Origin get operator.read/write.
        ws = websocket.create_connection(
            ws_url, timeout=CONNECT_TIMEOUT_SECONDS, suppress_origin=True
        )
    except Exception as e:
        raise _RetryableTransportError(f"connect {ws_url} failed: {e}") from e

    try:
        challenge = _recv_json(ws, "connect.challenge")
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
        return ws
    except Exception:
        _close_quietly(ws)
        raise


def _active_run_error(run_id: str) -> OpenclawDispatchError:
    identity = f"run {run_id}" if run_id else "an ambiguously accepted run"
    return OpenclawDispatchError(
        f"could not confirm terminal state for {identity} before its configured "
        f"{AGENT_RUN_TIMEOUT_SECONDS:g}s timeout plus "
        f"{RUN_TERMINATION_GRACE_SECONDS:g}s grace"
    )


def _hold_until_deadline(deadline: float) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(RECONNECT_MAX_DELAY_SECONDS, remaining))


def _reconnect_until_authenticated(
    ws_url: str,
    token: str,
    *,
    deadline: float,
) -> Any:
    """Reconnect while the gateway-enforced run lifetime can still be active."""
    delay = RECONNECT_INITIAL_DELAY_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ActiveRunDeadlineExceeded
        time.sleep(min(delay, remaining))
        if time.monotonic() >= deadline:
            raise _ActiveRunDeadlineExceeded
        try:
            ws = _open_authenticated_connection(ws_url, token)
        except OpenclawDispatchError:
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
            continue
        if time.monotonic() >= deadline:
            _close_quietly(ws)
            raise _ActiveRunDeadlineExceeded
        return ws


def dispatch_agent_run(
    *,
    base_url: str,
    token: str,
    session_key: str,
    channel: str,
    message: str,
    idempotency_key: str | None = None,
    resume_run_id: str | None = None,
    on_run_accepted: Callable[[str], None] | None = None,
) -> str:
    """Submit or resume an agent run and return after terminal success."""
    if idempotency_key is None:
        # Legacy callers still get a unique key. Inbound webhook callers should
        # supply a stable key so a retried event maps to the same OpenClaw run.
        agent_idempotency_key = str(uuid.uuid4())
    elif not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must be a nonblank string")
    else:
        agent_idempotency_key = idempotency_key.strip()

    if resume_run_id is None:
        normalized_resume_run_id = ""
    elif not isinstance(resume_run_id, str) or not resume_run_id.strip():
        raise ValueError("resume_run_id must be a nonblank string")
    elif resume_run_id != resume_run_id.strip():
        raise ValueError("resume_run_id must not contain surrounding whitespace")
    else:
        normalized_resume_run_id = resume_run_id
    if on_run_accepted is not None and not callable(on_run_accepted):
        raise TypeError("on_run_accepted must be callable")

    ws_url = http_to_ws_url(base_url)
    run_may_be_active = bool(normalized_resume_run_id)
    run_id = normalized_resume_run_id
    wait_number = 0
    agent_attempts = 0
    active_run_deadline = (
        time.monotonic() + AGENT_RUN_TIMEOUT_SECONDS + RUN_TERMINATION_GRACE_SECONDS
        if run_may_be_active
        else 0.0
    )
    missing_confirmations = 0
    acceptance_callback_invoked = False
    acceptance_callback_error: Exception | None = None
    run_identity_mismatch = False

    try:
        ws = _open_authenticated_connection(ws_url, token)
    except OpenclawDispatchError:
        # A fresh dispatch can fail fast because no request was attempted.
        # A persisted run, however, may still be active and keeps the same
        # bounded recovery policy as a disconnect during agent.wait.
        if not run_may_be_active:
            raise
        try:
            ws = _reconnect_until_authenticated(
                ws_url,
                token,
                deadline=active_run_deadline,
            )
        except _ActiveRunDeadlineExceeded as deadline_error:
            raise _active_run_error(run_id) from deadline_error

    try:
        while True:
            if run_may_be_active and time.monotonic() >= active_run_deadline:
                raise _active_run_error(run_id)
            if not run_id and agent_attempts >= MAX_AMBIGUOUS_AGENT_ATTEMPTS:
                _hold_until_deadline(active_run_deadline)
                raise _active_run_error(run_id)
            try:
                if not run_id:
                    # Mark the run as possibly active before send: a send error
                    # can happen after some or all request bytes reach OpenClaw.
                    agent_attempts += 1
                    active_run_deadline = max(
                        active_run_deadline,
                        time.monotonic()
                        + AGENT_RUN_TIMEOUT_SECONDS
                        + RUN_TERMINATION_GRACE_SECONDS,
                    )
                    run_may_be_active = True
                    try:
                        _request(
                            ws,
                            "a1",
                            "agent",
                            {
                                "message": message,
                                "sessionKey": session_key,
                                "channel": channel,
                                "idempotencyKey": agent_idempotency_key,
                                "timeout": AGENT_RUN_TIMEOUT_SECONDS,
                            },
                        )
                        payload = _check_ok(_recv_response(ws, "a1", "agent"), "agent")
                    except _RpcRejectedError:
                        # A correlated negative response confirms that this
                        # request did not enter the agent run lifecycle only
                        # when no earlier attempt had an ambiguous outcome.
                        if agent_attempts == 1:
                            run_may_be_active = False
                        raise

                    status = str(payload.get("status") or "").strip().lower()
                    if status not in {"accepted", "in_flight"}:
                        # An affirmative but unfamiliar response is ambiguous;
                        # reconnect and replay the same idempotency key.
                        raise _RetryableTransportError(
                            f"agent returned an unexpected status: {payload}"
                        )
                    run_id = str(payload.get("runId") or "").strip()
                    if not run_id:
                        raise OpenclawDispatchError(
                            f"agent accepted without a nonblank runId: {payload}"
                        )
                    if run_id != agent_idempotency_key:
                        # The Gateway may already be executing this run. Keep
                        # the processing lease through its terminal state,
                        # then fail closed instead of trusting the mismatched
                        # provider identity for durable recovery.
                        run_identity_mismatch = True

                if not acceptance_callback_invoked:
                    acceptance_callback_invoked = True
                    if on_run_accepted is not None:
                        try:
                            on_run_accepted(run_id)
                        except Exception as callback_error:  # noqa: BLE001
                            # The run is already active. Preserve the processing
                            # lease until it terminates, then report persistence
                            # failure instead of releasing while it may run.
                            acceptance_callback_error = callback_error

                wait_number += 1
                wait_id = f"w{wait_number}"
                _request(
                    ws,
                    wait_id,
                    "agent.wait",
                    {
                        "runId": run_id,
                        "timeoutMs": AGENT_WAIT_POLL_MILLISECONDS,
                    },
                )
                wait_payload = _check_ok(
                    _recv_response(
                        ws,
                        wait_id,
                        "agent.wait",
                        timeout_seconds=AGENT_WAIT_RESPONSE_TIMEOUT_SECONDS,
                    ),
                    "agent.wait",
                )
                missing_confirmations = 0
                wait_status = str(wait_payload.get("status") or "").strip().lower()

                stop_reason = str(
                    wait_payload.get("stopReason")
                    or wait_payload.get("stop_reason")
                    or ""
                ).strip()
                if (
                    wait_status == "error"
                    and stop_reason.lower() in _CANCELLATION_STOP_REASONS
                ):
                    raise OpenclawRunCancelled(run_id, stop_reason)

                if wait_status == "ok":
                    if run_identity_mismatch:
                        raise _TerminalRunError(
                            "agent reached terminal success, but its runId does "
                            "not match the supplied idempotencyKey"
                        )
                    if acceptance_callback_error is not None:
                        run_may_be_active = False
                        raise OpenclawDispatchError(
                            "agent run reached terminal success, but its accepted "
                            "runId could not be persisted"
                        ) from acceptance_callback_error
                    return run_id
                if wait_status == "error":
                    error = wait_payload.get("error")
                    if isinstance(error, dict):
                        detail = " ".join(
                            str(error.get(field) or "").strip()
                            for field in ("code", "message")
                        ).strip()
                    else:
                        detail = str(error or "").strip()
                    detail = detail or str(wait_payload)
                    raise _TerminalRunError(f"agent run {run_id} failed: {detail}")
                if wait_status == "timeout":
                    ended_at = wait_payload.get("endedAt")
                    if ended_at not in (None, ""):
                        detail = str(wait_payload.get("error") or "").strip()
                        suffix = f": {detail}" if detail else ""
                        raise _TerminalRunError(
                            f"agent run {run_id} timed out at {ended_at}{suffix}"
                        )
                    continue
                if wait_status == "pending":
                    continue
                raise OpenclawDispatchError(
                    "agent.wait returned unexpected status "
                    f"for {run_id}: {wait_payload}"
                )
            except _TerminalRunError:
                raise
            except OpenclawRunCancelled:
                raise
            except OpenclawDispatchError as error:
                if not run_may_be_active:
                    raise

                if (
                    run_id
                    and isinstance(error, _RpcRejectedError)
                    and error.step == "agent.wait"
                    and error.code in _DEFINITIVE_MISSING_CODES
                ):
                    missing_confirmations += 1
                    if missing_confirmations >= DEFINITIVE_MISSING_CONFIRMATIONS:
                        raise _TerminalRunError(
                            f"agent run {run_id} is no longer active: "
                            f"{error.code} confirmed on "
                            f"{missing_confirmations} authenticated connections"
                        ) from error
                else:
                    missing_confirmations = 0

                _close_quietly(ws)
                try:
                    ws = _reconnect_until_authenticated(
                        ws_url,
                        token,
                        deadline=active_run_deadline,
                    )
                except _ActiveRunDeadlineExceeded as deadline_error:
                    raise _active_run_error(run_id) from deadline_error
    finally:
        _close_quietly(ws)
