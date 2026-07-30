from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)


def _is_image(meta: dict[str, Any], local_path: str) -> bool:
    return meta.get("contentType", "").startswith("image/") or local_path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )


def build_openclaw_prompt(
    text: str, user: str, files_with_meta: list[dict[str, Any]]
) -> str:
    def to_block_and_path(item: dict[str, Any]) -> tuple[str, str | None, bool]:
        meta = item.get("meta", {})
        local_path = item.get("fp") or meta.get("localPath")
        if not local_path:
            return (
                f"[Attachment {meta.get('contentName')} failed to download: {meta.get('error')}]",
                None,
                False,
            )
        block = "\n".join(
            [
                "[FILE_META]",
                f"localPath: {local_path}",
                f"name: {meta.get('contentName')}",
                f"mimeType: {meta.get('contentType')}",
                f"driveFileId: {meta.get('driveFileId')}",
                f"size: {meta.get('savedSize')} bytes",
                "[/FILE_META]",
            ]
        )
        return (block, str(local_path), _is_image(meta, str(local_path)))

    def tool_section(label: str, paths: list[str]) -> list[str]:
        return [label, *(f"- {p}" for p in paths)] if paths else []

    block_and_path_pairs = list(map(to_block_and_path, files_with_meta))
    blocks = [block for block, _, _ in block_and_path_pairs]
    image_paths = [path for _, path, is_img in block_and_path_pairs if path and is_img]
    other_paths = [path for _, path, is_img in block_and_path_pairs if path and not is_img]

    instruction_body = [
        *tool_section(
            "The images below are already attached above — answer directly from "
            "what you see, no tool call needed:",
            image_paths,
        ),
        *tool_section(
            "Call the read tool on each path below before answering questions about it:",
            other_paths,
        ),
    ]
    attachment_instruction = (
        [
            "\n".join(
                ["[ATTACHMENT_INSTRUCTION]", *instruction_body, "[/ATTACHMENT_INSTRUCTION]"]
            )
        ]
        if instruction_body
        else []
    )
    return "\n\n".join([*blocks, *attachment_instruction, f"{user}: {text}"])


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
    log.info("openclaw prompt (agent=%s, session_key=%s): %s", agent, session_key, prompt)
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

    log.info(
        "openclaw returncode=%s stdout=%r stderr=%r", cp.returncode, cp.stdout, cp.stderr
    )
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()[:500]
        raise RuntimeError(f"openclaw failed (code={cp.returncode}): {err}")

    try:
        return parse_openclaw_text(cp.stdout)
    except Exception as e:
        snippet = (cp.stdout or "").strip()[:500]
        raise RuntimeError(f"openclaw parse failed: {e}; output={snippet}") from e
