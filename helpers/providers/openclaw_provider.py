from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from helpers.jsonl_log import append_jsonl
from helpers.providers.openclaw_prompts import (
    ATTACHMENT_INSTRUCTION_CLOSE,
    ATTACHMENT_INSTRUCTION_OPEN,
    CONVERTED_FILE_NOTE_TEMPLATE,
    FAILED_ATTACHMENT_TEMPLATE,
    FILE_META_CLOSE,
    FILE_META_OPEN,
    GOOGLE_WORKSPACE_TYPE_FALLBACK_LABEL,
    GOOGLE_WORKSPACE_TYPE_LABELS,
    IMAGE_INSTRUCTION,
    OTHER_FILE_INSTRUCTION,
    QUOTED_MESSAGE_CLOSE,
    QUOTED_MESSAGE_INSTRUCTION,
    QUOTED_MESSAGE_OPEN,
    STICKER_INSTRUCTION,
    STICKER_KIND_LABEL,
)
from helpers.providers.openclaw_ws import (
    OpenclawDispatchError,
    dispatch_agent_run,
)

DEFAULT_OPENCLAW_CONFIG_FILE = Path("~/.openclaw/openclaw.json").expanduser()
ENGLISH_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9 _-]*$")
OPENCLAW_MESSAGE_CHANNEL = "googlechat"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OPENCLAW_OUT_LOG_FILE = _PROJECT_ROOT / "openclaw-out.jsonl"
OPENCLAW_IN_LOG_FILE = _PROJECT_ROOT / "openclaw-in.jsonl"
NO_ASSISTANT_TEXT_INFO = "ℹ️ การทำงานเสร็จสิ้นโดยไม่มีข้อความตอบกลับ"


def _load_gateway_token() -> str:
    environment_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if environment_token:
        return environment_token

    config_file = Path(
        os.environ.get("OPENCLAW_CONFIG_FILE", str(DEFAULT_OPENCLAW_CONFIG_FILE))
    ).expanduser()
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(f"openclaw config not found: {config_file}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"openclaw config is not valid JSON: {config_file}") from e

    token = (
        data.get("gateway", {}).get("auth", {}).get("token", "")
        if isinstance(data, dict)
        else ""
    )
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(
            f"openclaw gateway token missing at {config_file} -> gateway.auth.token"
        )
    return token.strip()


def _is_image(meta: dict[str, Any], local_path: str) -> bool:
    return meta.get("contentType", "").startswith(
        "image/"
    ) or local_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def build_openclaw_prompt(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    quoted_message: dict[str, str] | None = None,
) -> str:
    if (
        ENGLISH_SLASH_COMMAND_RE.fullmatch(text.strip())
        and not files_with_meta
        and not quoted_message
    ):
        return text.strip()

    def classify(meta: dict[str, Any], local_path: str) -> str:
        if meta.get("isSticker"):
            return "sticker"
        if _is_image(meta, local_path):
            return "image"
        return "other"

    def to_block_and_path(item: dict[str, Any]) -> tuple[str, str | None, str]:
        meta = item.get("meta", {})
        local_path = item.get("fp") or meta.get("localPath")
        if not local_path:
            return (
                FAILED_ATTACHMENT_TEMPLATE.format(
                    name=meta.get("contentName"), error=meta.get("error")
                ),
                None,
                "other",
            )
        kind = classify(meta, str(local_path))
        block_lines = [
            FILE_META_OPEN,
            f"localPath: {local_path}",
            f"name: {meta.get('contentName')}",
            f"mimeType: {meta.get('contentType')}",
            f"driveFileId: {meta.get('driveFileId')}",
            f"size: {meta.get('savedSize')} bytes",
        ]
        if kind == "sticker":
            block_lines.append(STICKER_KIND_LABEL)
        original_ctype = meta.get("originalContentType")
        if original_ctype:
            label = GOOGLE_WORKSPACE_TYPE_LABELS.get(
                original_ctype, GOOGLE_WORKSPACE_TYPE_FALLBACK_LABEL
            )
            block_lines.append(
                CONVERTED_FILE_NOTE_TEMPLATE.format(original_label=label)
            )
        block_lines.append(FILE_META_CLOSE)
        return ("\n".join(block_lines), str(local_path), kind)

    def tool_section(label: str, paths: list[str]) -> list[str]:
        return [label, *(f"- {p}" for p in paths)] if paths else []

    block_and_path_triples = [to_block_and_path(item) for item in files_with_meta]
    blocks = [block for block, _, _ in block_and_path_triples]
    sticker_paths = [
        p for _, p, kind in block_and_path_triples if p and kind == "sticker"
    ]
    image_paths = [p for _, p, kind in block_and_path_triples if p and kind == "image"]
    other_paths = [p for _, p, kind in block_and_path_triples if p and kind == "other"]

    instruction_body = [
        *tool_section(STICKER_INSTRUCTION, sticker_paths),
        *tool_section(IMAGE_INSTRUCTION, image_paths),
        *tool_section(OTHER_FILE_INSTRUCTION, other_paths),
    ]
    attachment_instruction = (
        [
            "\n".join(
                [
                    ATTACHMENT_INSTRUCTION_OPEN,
                    *instruction_body,
                    ATTACHMENT_INSTRUCTION_CLOSE,
                ]
            )
        ]
        if instruction_body
        else []
    )
    quoted_block = (
        [
            "\n".join(
                [
                    QUOTED_MESSAGE_INSTRUCTION,
                    QUOTED_MESSAGE_OPEN,
                    f"sender: {quoted_message.get('sender', '')}",
                    f"text: {quoted_message.get('text', '')}",
                    QUOTED_MESSAGE_CLOSE,
                ]
            )
        ]
        if quoted_message and quoted_message.get("text")
        else []
    )
    return "\n\n".join(
        [
            *quoted_block,
            *blocks,
            *attachment_instruction,
            f"{user}: {text}",
        ]
    )


def parse_openclaw_response(payload: dict[str, Any]) -> dict[str, str]:
    """Return the assistant's text response."""
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("openclaw output has no choices")

    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        raise TypeError("openclaw output message is not a dict")

    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(part for part in text_parts if part)

    text = text.strip()
    if not text:
        text = NO_ASSISTANT_TEXT_INFO

    return {"text": text}


def ask_openclaw_direct(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    session_key: str,
    base_url: str,
    model: str,
    quoted_message: dict[str, str] | None = None,
) -> dict[str, str]:
    gateway_token = _load_gateway_token()
    prompt = build_openclaw_prompt(text, user, files_with_meta, quoted_message)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }

    append_jsonl(OPENCLAW_OUT_LOG_FILE, payload)

    try:
        run_id = dispatch_agent_run(
            base_url=base_url,
            token=gateway_token,
            session_key=session_key,
            channel=OPENCLAW_MESSAGE_CHANNEL,
            message=prompt,
        )
    except OpenclawDispatchError as e:
        raise RuntimeError(f"openclaw dispatch failed: {e}") from e

    append_jsonl(OPENCLAW_IN_LOG_FILE, {"status": "accepted", "run_id": run_id})
    return {"text": "", "run_id": run_id}
