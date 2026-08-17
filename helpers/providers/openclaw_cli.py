from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

from helpers.session_keys import SESSION_AGENT

OPENCLAW_CLI_TIMEOUT_SECONDS = 15
OPENCLAW_CLI_ENV = "OPENCLAW_CLI"


def _version_key(path: Path) -> tuple[int, ...]:
    # Split off the pre-release suffix (e.g. "-rc.1") before splitting the
    # numeric core by "." - SemVer pre-release identifiers are themselves
    # dot-separated, so splitting the whole name by "." would make a
    # dotted suffix like "-rc.1" produce an extra tuple element and outrank
    # the shorter, plain release under Python's tuple ordering.
    core, is_prerelease, _suffix = (
        path.parent.parent.name.removeprefix("v").partition("-")
    )
    parts: list[int] = []
    for chunk in core.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    # A pre-release suffix must sort below the plain release with the same
    # numeric core, not tie with it.
    parts.append(0 if is_prerelease else 1)
    return tuple(parts)


@functools.lru_cache(maxsize=1)
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
        existing_path = os.environ.get("PATH", "")
        path_value = (
            f"{binary_dir}{os.pathsep}{existing_path}" if existing_path else binary_dir
        )
        env = {**os.environ, "PATH": path_value}
    try:
        return subprocess.run(
            [binary, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=OPENCLAW_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (IsADirectoryError, NotADirectoryError, PermissionError) as exc:
        # Callers translate FileNotFoundError into a friendly "command not
        # found" message; a misconfigured OPENCLAW_CLI/PATH can raise these
        # sibling OSError subtypes instead, which would otherwise surface a
        # raw OS error to chat users.
        raise FileNotFoundError(f"openclaw binary is not runnable: {binary}") from exc


def abort_session(session_key: str) -> subprocess.CompletedProcess[str]:
    # OpenClaw >= 2026.7 removed the clearQueued param — sessions.abort now
    # accepts only the session key and rejects any extra property with
    # INVALID_REQUEST, which silently broke /abort after the gateway upgrade.
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


def reset_session(session_key: str) -> subprocess.CompletedProcess[str]:
    """Reset one exact session while preserving its deterministic key."""

    return _run(
        [
            "gateway",
            "call",
            "sessions.reset",
            "--json",
            "--params",
            json.dumps({"key": session_key, "reason": "new"}),
        ]
    )


def list_models() -> subprocess.CompletedProcess[str]:
    return _run(["models", "list", "--json"])


def get_default_model() -> subprocess.CompletedProcess[str]:
    return _run(["config", "get", "agents.defaults.model.primary", "--json"])


def list_sessions() -> subprocess.CompletedProcess[str]:
    # Deterministic Chat sessions can easily outlive the CLI's bounded default
    # page. Their key prefix is fixed to agent `main`, so model/exists checks
    # must inspect that exact complete store rather than the ambient default.
    return _run(
        [
            "sessions",
            "list",
            "--agent",
            SESSION_AGENT,
            "--json",
            "--limit",
            "all",
        ]
    )
