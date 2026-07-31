from __future__ import annotations

import base64
import mimetypes
import os
from itertools import chain
from pathlib import Path
from typing import Any

from openai import OpenAI


def _to_kimi_content_blocks(
    item: dict[str, Any],
    max_image_embed_bytes: int,
) -> list[dict[str, Any]]:
    fp = item["fp"]
    m = item["meta"]
    if not fp or not Path(fp).exists():
        return [
            {
                "type": "text",
                "text": f"[ไฟล์ {m.get('contentName')} โหลดไม่สำเร็จ: {m.get('error')}]",
            }
        ]

    meta_text = "\n".join(
        [
            "[FILE_META]",
            f"name: {m.get('contentName')}",
            f"mimeType: {m.get('contentType')}",
            f"driveFileId: {m.get('driveFileId')}",
            f"size: {m.get('savedSize')} bytes",
            "[/FILE_META]",
        ]
    )
    meta_block = {"type": "text", "text": meta_text}
    is_image = m.get("contentType", "").startswith("image/") or fp.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )
    if not is_image:
        return [meta_block]

    if Path(fp).stat().st_size > max_image_embed_bytes:
        return [
            meta_block,
            {
                "type": "text",
                "text": f"[ไฟล์ {m.get('contentName')} ใหญ่เกิน {max_image_embed_bytes} bytes จึงไม่แนบรูปภาพ]",
            },
        ]

    with open(fp, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    mime = mimetypes.guess_type(fp)[0] or m.get("contentType") or "image/png"
    data_url = f"data:{mime};base64,{b64}"
    return [meta_block, {"type": "image_url", "image_url": {"url": data_url}}]


def ask_kimi_direct(
    text: str,
    user: str,
    files_with_meta: list[dict[str, Any]],
    *,
    auth_debug: bool = False,
    base_url: str = "https://api.moonshot.ai/v1",
) -> str:
    client = OpenAI(
        api_key=os.environ.get("MOONSHOT_API_KEY"),
        base_url=base_url,
    )
    max_image_embed_bytes = int(
        os.environ.get("MAX_IMAGE_EMBED_BYTES", str(8 * 1024 * 1024))
    )
    content_blocks = list(
        chain.from_iterable(
            _to_kimi_content_blocks(item, max_image_embed_bytes)
            for item in files_with_meta
            )
    )
    content_blocks = [*content_blocks, {"type": "text", "text": f"{user}: {text}"}]
    raw = client.chat.completions.with_raw_response.create(
        model="kimi-k3",
        messages=[
            {
                "role": "system",
                "content": "You are Kimi K3. เมื่อได้รับ FILE_META ให้ใช้ชื่อไฟล์และ mimeType ประกอบการตอบด้วย",
            },
            {"role": "user", "content": content_blocks},
        ],
    )

    completion = raw.parse()
    usage = completion.usage
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(
        getattr(usage, "total_tokens", prompt_tokens + completion_tokens)
        or (prompt_tokens + completion_tokens)
    )

    if auth_debug:
        import logging

        logging.getLogger(__name__).debug(
            "Moonshot usage: prompt=%s completion=%s total=%s",
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )

    return completion.choices[0].message.content
