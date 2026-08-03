from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from helpers.jsonl_log import append_jsonl
from helpers.providers.kimiclaw_gateway import (
    GatewayResult,
    KimiclawGatewayError,
    run_gateway_request,
)
from helpers.providers.openclaw_cli import abort_session
from helpers.providers.openclaw_provider import (
    build_openclaw_prompt,
    parse_openclaw_response,
)

KIMICLAW_CHANNEL = "kimi-claw"
MODEL_COMMAND_RE = re.compile(r"^/model(?:\s|$)", re.IGNORECASE)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KIMICLAW_OUT_LOG_FILE = _PROJECT_ROOT / "kimiclaw-out.jsonl"
KIMICLAW_IN_LOG_FILE = _PROJECT_ROOT / "kimiclaw-in.jsonl"


def _parse_gateway_result(result: GatewayResult) -> dict[str, Any]:
    return parse_openclaw_response(
        {"choices": [{"message": {"content": result.text}}]}
    )


def _abort_failed_run(session_key: str) -> None:
    try:
        result = abort_session(session_key)
        print(
            f"🛑 [kimiclaw] cleanup abort returncode={result.returncode} "
            f"session={session_key!r}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"❌ [kimiclaw] cleanup abort failed: {e} session={session_key!r}")


def ask_kimiclaw(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    session_key: str,
    quoted_message: dict[str, str] | None = None,
) -> dict[str, Any]:
    is_model_command = MODEL_COMMAND_RE.match(text.strip()) is not None
    prompt = (
        text.strip()
        if is_model_command
        else build_openclaw_prompt(text, user, files_with_meta, quoted_message)
    )
    mode = "command" if is_model_command else "agent"
    append_jsonl(
        KIMICLAW_OUT_LOG_FILE,
        {
            "channel": KIMICLAW_CHANNEL,
            "mode": mode,
            "sessionKey": session_key,
            "message": prompt,
        },
    )

    try:
        result = run_gateway_request(prompt, session_key, mode=mode)
    except KimiclawGatewayError as e:
        append_jsonl(
            KIMICLAW_IN_LOG_FILE,
            {
                "ok": False,
                "where": e.where,
                "code": e.code,
                "message": e.message,
                "runId": e.run_id,
            },
        )
        if e.should_abort:
            _abort_failed_run(session_key)
        raise RuntimeError(str(e)) from e
    except Exception as e:
        append_jsonl(
            KIMICLAW_IN_LOG_FILE,
            {
                "ok": False,
                "where": "startup",
                "code": "STARTUP_ERROR",
                "message": str(e),
            },
        )
        raise RuntimeError(f"Kimiclaw gateway startup failed: {e}") from e

    append_jsonl(
        KIMICLAW_IN_LOG_FILE,
        {"ok": True, "runId": result.run_id, "text": result.text},
    )
    return _parse_gateway_result(result)
