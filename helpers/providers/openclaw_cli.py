from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

OPENCLAW_CLI_TIMEOUT_SECONDS = 15
OPENCLAW_CLI_ENV = "OPENCLAW_CLI"


def _version_key(path: Path) -> tuple[int, ...]:
    parts: list[int] = []
    is_release = True
    for chunk in path.parent.parent.name.lstrip("v").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                is_release = False
                break
        parts.append(int(digits) if digits else 0)
    # Pre-release suffixes (e.g. "0-rc1") must sort below the plain release
    # with the same numeric prefix, not tie with it at the coerced-to-0 value.
    parts.append(1 if is_release else 0)
    return tuple(parts)


def _resolve_binary() -> str:
    override = os.environ.get(OPENCLAW_CLI_ENV, "").strip()
    if override:
        return os.path.expanduser(os.path.expandvars(override))
    found = shutil.which("openclaw")
    if found:
        return found
    # nvm-managed installs are not on PATH for services; use the newest one.
    try:
        home = Path.home()
    except RuntimeError:
        # HOME unset and no passwd entry for this UID (common under minimal
        # service/container users) - fall through to the bare-name fallback.
        home = None
    if home is not None:
        candidates = sorted(
            home.glob(".nvm/versions/node/*/bin/openclaw"),
            key=_version_key,
        )
        if candidates:
            return str(candidates[-1])
    # Fall back to the bare name so callers still get FileNotFoundError.
    return "openclaw"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    binary = _resolve_binary()
    env = None
    binary_dir = os.path.dirname(binary)
    if binary_dir:
        # The openclaw shim uses `#!/usr/bin/env node`; make sure the node
        # from the same install prefix wins over any older system node.
        env = {
            **os.environ,
            "PATH": binary_dir + os.pathsep + os.environ.get("PATH", ""),
        }
    return subprocess.run(
        [binary, *args],
        env=env,
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


def create_session(
    session_key: str,
    agent: str,
    model: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "gateway",
            "call",
            "sessions.create",
            "--json",
            "--params",
            json.dumps(
                {
                    "key": session_key,
                    "agentId": agent,
                    "model": model,
                }
            ),
        ]
    )


def list_models() -> subprocess.CompletedProcess[str]:
    return _run(["models", "list", "--json"])


def get_default_model() -> subprocess.CompletedProcess[str]:
    return _run(["config", "get", "agents.defaults.model.primary", "--json"])


def list_sessions() -> subprocess.CompletedProcess[str]:
    return _run(["sessions", "list", "--json"])
