from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from helpers.jsonl_log import append_jsonl
from helpers.providers.openclaw_prompts import (
    ATTACHMENT_INSTRUCTION_CLOSE,
    ATTACHMENT_INSTRUCTION_OPEN,
    CONVERTED_FILE_NOTE_TEMPLATE,
    FAILED_ATTACHMENT_TEMPLATE,
    FILE_META_CLOSE,
    FILE_META_OPEN,
    FILE_SEND_CAPABILITY,
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

OPENCLAW_CONFIG_FILE = Path("~/.openclaw/openclaw.json").expanduser()
ENGLISH_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9 _-]*$")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OPENCLAW_OUT_LOG_FILE = _PROJECT_ROOT / "openclaw-out.jsonl"
OPENCLAW_IN_LOG_FILE = _PROJECT_ROOT / "openclaw-in.jsonl"

# Regex fallback: [[ATTACH:/path/to/file]] or [[FILE:/path]] — the double
# brackets on both ends are required so this never fires on ordinary prose
# that happens to contain the word "file:" or "attach:".
FILE_TAG_RE = re.compile(r"\[\[(?:ATTACH|FILE):\s*([^\]\n]+?)\]\]", re.IGNORECASE)


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
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    quoted_message: dict[str, str] | None = None,
) -> str:
    if ENGLISH_SLASH_COMMAND_RE.fullmatch(text.strip()):
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
            block_lines.append(CONVERTED_FILE_NOTE_TEMPLATE.format(original_label=label))
        block_lines.append(FILE_META_CLOSE)
        return ("\n".join(block_lines), str(local_path), kind)

    def tool_section(label: str, paths: list[str]) -> list[str]:
        return [label, *(f"- {p}" for p in paths)] if paths else []

    block_and_path_triples = list(map(to_block_and_path, files_with_meta))
    blocks = [block for block, _, _ in block_and_path_triples]
    sticker_paths = [p for _, p, kind in block_and_path_triples if p and kind == "sticker"]
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
            FILE_SEND_CAPABILITY.strip(),
            *quoted_block,
            *blocks,
            *attachment_instruction,
            f"{user}: {text}",
        ]
    )


def parse_openclaw_response(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Returns dict: {text: str, files: List[Dict{filePath, filename, caption}]}
    Supports:
    1. OpenAI tool_calls: upload-file / send_file
    2. Fallback tags in content: [[ATTACH:/path]] or [[FILE:/path]]
    """
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
        text = "\n".join(filter(None, text_parts))

    files: list[dict[str, str]] = []

    # 1) tool_calls
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        try:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "")
            if name not in ("upload-file", "send_file", "send_file_attachment", "attach_file"):
                continue
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                args = json.loads(args_raw) if args_raw.strip() else {}
            else:
                args = args_raw if isinstance(args_raw, dict) else {}
            fp = args.get("filePath") or args.get("path") or args.get("media") or args.get("file_path")
            if fp:
                files.append({
                    "filePath": str(fp).strip(),
                    "filename": str(args.get("filename") or Path(str(fp)).name),
                    "caption": str(args.get("message") or args.get("caption") or ""),
                })
        except Exception as e:  # noqa: BLE001
            print(f"[parse_openclaw_response] skip malformed tool_call: {e}")
            continue

    # 2) fallback tags in text
    if not files:
        for m in FILE_TAG_RE.finditer(text):
            fp = m.group(1).strip().strip("'\"")
            if fp:
                files.append({
                    "filePath": fp,
                    "filename": Path(fp).name,
                    "caption": "",
                })
        # remove tags from text to avoid showing raw paths
        text = FILE_TAG_RE.sub("", text).strip()

    if not text and not files:
        raise RuntimeError("openclaw output has no assistant text")

    return {"text": text.strip() or "", "files": files}


def ask_openclaw_direct(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    agent: str,
    session_key: str,
    base_url: str,
    model: str,
    quoted_message: dict[str, str] | None = None,
) -> dict[str, Any]:
    gateway_token = _load_gateway_token()
    prompt = build_openclaw_prompt(text, user, files_with_meta, quoted_message)
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
        # allow tool calling
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "upload-file",
                    "description": "ส่งไฟล์แนบกลับไปให้ผู้ใช้ใน Google Chat เมื่อต้องส่งรายงาน PDF Excel รูป ฯลฯ",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filePath": {"type": "string", "description": "พาธเต็มของไฟล์ที่มีอยู่จริงบนดิสก์ เช่น /tmp/openclaw/report.pdf"},
                            "filename": {"type": "string", "description": "ชื่อไฟล์ที่จะแสดง"},
                            "message": {"type": "string", "description": "ข้อความอธิบายไฟล์"}
                        },
                        "required": ["filePath"]
                    }
                }
            }
        ],
        "tool_choice": "auto",
    }

    append_jsonl(OPENCLAW_OUT_LOG_FILE, payload)

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=120,
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
        return parse_openclaw_response(response_body)
    except Exception as e:
        snippet = (resp.text or "").strip()[:500]
        raise RuntimeError(f"openclaw parse failed: {e}; output={snippet}") from e
