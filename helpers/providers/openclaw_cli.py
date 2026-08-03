from __future__ import annotations

import json
import subprocess

OPENCLAW_CLI_TIMEOUT_SECONDS = 15


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openclaw", *args],
        capture_output=True,
        text=True,
        timeout=OPENCLAW_CLI_TIMEOUT_SECONDS,
        check=False,
    )


def abort_session(session_key: str) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "gateway",
            "call",
            "sessions.abort",
            "--json",
            "--params",
            json.dumps({"key": session_key}),
        ]
    )


def list_models() -> subprocess.CompletedProcess[str]:
    return _run(["models", "list", "--json"])
