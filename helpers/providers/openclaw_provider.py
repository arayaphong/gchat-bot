from __future__ import annotations

import json
import subprocess
from typing import Any


def build_openclaw_prompt(
    text: str, user: str, files_with_meta: list[dict[str, Any]]
) -> str:
    blocks: list[str] = []
    for item in files_with_meta:
        meta = item.get("meta", {})
        if item.get("fp"):
            blocks.append(
                "\n".join(
                    [
                        "[FILE_META]",
                        f"name: {meta.get('contentName')}",
                        f"mimeType: {meta.get('contentType')}",
                        f"driveFileId: {meta.get('driveFileId')}",
                        f"size: {meta.get('savedSize')} bytes",
                        "[/FILE_META]",
                    ]
                )
            )
        else:
            blocks.append(
                f"[ไฟล์ {meta.get('contentName')} โหลดไม่สำเร็จ: {meta.get('error')}]"
            )
    blocks.append(f"{user}: {text}")
    return "\n\n".join(blocks)


def parse_openclaw_text(stdout: str) -> str:
    payload = json.loads(stdout.strip())
    if payload.get("status") != "ok":
        raise RuntimeError(f"openclaw status not ok: {payload.get('status')}")

    result = payload.get("result", {})
    payloads = result.get("payloads", []) if isinstance(result, dict) else []
    if payloads and isinstance(payloads[0], dict) and payloads[0].get("text"):
        return str(payloads[0]["text"])

    text = result.get("finalAssistantVisibleText") if isinstance(result, dict) else None
    if text:
        return str(text)

    raise RuntimeError("openclaw output has no assistant text")


def ask_openclaw_direct(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    agent: str,
    session_key: str,
    timeout_seconds: int,
) -> str:
    prompt = build_openclaw_prompt(text, user, files_with_meta)
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        agent,
        "--session-key",
        session_key,
        "--json",
        "--message",
        prompt,
    ]
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(timeout_seconds, 1),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"openclaw timeout: {e}") from e

    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()[:500]
        raise RuntimeError(f"openclaw failed (code={cp.returncode}): {err}")

    try:
        return parse_openclaw_text(cp.stdout)
    except Exception as e:
        snippet = (cp.stdout or "").strip()[:500]
        raise RuntimeError(f"openclaw parse failed: {e}; output={snippet}") from e
