from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from helpers.jsonl_log import append_jsonl

OPENCLAW_CONFIG_FILE = Path("~/.openclaw/openclaw.json").expanduser()
ENGLISH_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9 _-]*$")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OPENCLAW_OUT_LOG_FILE = _PROJECT_ROOT / "openclaw-out.jsonl"
OPENCLAW_IN_LOG_FILE = _PROJECT_ROOT / "openclaw-in.jsonl"


def _load_gateway_token() -> str:
    try:
        data = json.loads(OPENCLAW_CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(
            f"openclaw config not found: {OPENCLAW_CONFIG_FILE}"
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"openclaw config is not valid JSON: {OPENCLAW_CONFIG_FILE}"
        ) from e

    token = (
        data.get("gateway", {}).get("auth", {}).get("token", "")
        if isinstance(data, dict)
        else ""
    )
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(
            "openclaw gateway token missing at ~/.openclaw/openclaw.json -> gateway.auth.token"
        )
    return token.strip()


def _is_image(meta: dict[str, Any], local_path: str) -> bool:
    return meta.get("contentType", "").startswith("image/") or local_path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )


def build_openclaw_prompt(
    text: str, user: str, files_with_meta: list[dict[str, Any]]
) -> str:
    if ENGLISH_SLASH_COMMAND_RE.fullmatch(text.strip()):
        return text.strip()

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


def parse_openclaw_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("openclaw output has no choices")

    message = choices[0].get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content

    if isinstance(content, list):
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        merged = "\n".join(filter(None, text_parts)).strip()
        if merged:
            return merged

    raise RuntimeError("openclaw output has no assistant text")


def ask_openclaw_direct(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    agent: str,
    session_key: str,
    base_url: str,
    model: str,
) -> str:
    gateway_token = _load_gateway_token()
    prompt = build_openclaw_prompt(text, user, files_with_meta)
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if gateway_token:
        headers["Authorization"] = f"Bearer {gateway_token}"
    if session_key:
        headers["x-openclaw-session-key"] = session_key
    if agent:
        headers["X-Agent-Name"] = agent
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }

    append_jsonl(OPENCLAW_OUT_LOG_FILE, payload)

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
        )
    except requests.Timeout as e:
        raise RuntimeError(f"openclaw timeout: {e}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"openclaw request failed: {e}") from e

    try:
        response_body = resp.json()
    except ValueError:
        response_body = {"status_code": resp.status_code, "text": (resp.text or "")[:2000]}
    append_jsonl(OPENCLAW_IN_LOG_FILE, response_body)

    if not resp.ok:
        err = (resp.text or "").strip()[:500]
        raise RuntimeError(f"openclaw failed (status={resp.status_code}): {err}")

    try:
        return parse_openclaw_text(response_body)
    except Exception as e:
        snippet = (resp.text or "").strip()[:500]
        raise RuntimeError(f"openclaw parse failed: {e}; output={snippet}") from e
