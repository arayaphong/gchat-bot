from __future__ import annotations

from typing import Any

BUSY_TEXT = "⏳ ระบบกำลังคิดตอบสำหรับข้อความก่อนหน้าอยู่ กรุณาส่งใหม่อีกครั้งภายหลัง"
ABORT_SUCCESS_TEXT = "✅ หยุดการทำงานสำเร็จ"
ABORT_FAILURE_TEMPLATE = "❌ หยุดการทำงานไม่สำเร็จ: {reason}"
NEW_SESSION_TEXT = "🔄 เริ่มเซสชั่นใหม่แล้ว"
MODELS_FAILURE_TEMPLATE = "❌ ไม่สามารถแสดงรายการโมเดลได้: {reason}"
MODEL_COMMAND_USAGE_TEXT = "ℹ️ วิธีใช้: /model <model-key> (ดูรายการด้วย /models)"
MODEL_NOT_FOUND_TEMPLATE = "❌ ไม่พบโมเดล: {model} (ดูรายการด้วย /models)"
MODEL_UNAVAILABLE_TEMPLATE = "❌ โมเดลไม่พร้อมใช้งาน: {model}"
MODEL_VALIDATION_FAILURE_TEMPLATE = "❌ ไม่สามารถตรวจสอบโมเดลได้: {reason}"


def _markdown_text(value: Any, fallback: str = "—") -> str:
    if value is None:
        return fallback

    text = " ".join(str(value).split())
    if not text:
        return fallback

    for character in "\\`*{}_[]<>#":
        text = text.replace(character, f"\\{character}")
    return text


def format_model_not_found(model_key: str) -> str:
    return MODEL_NOT_FOUND_TEMPLATE.format(model=_markdown_text(model_key))


def format_model_unavailable(model_key: str) -> str:
    return MODEL_UNAVAILABLE_TEMPLATE.format(model=_markdown_text(model_key))


def format_model_validation_failure(reason: Any) -> str:
    return MODEL_VALIDATION_FAILURE_TEMPLATE.format(reason=_markdown_text(reason))


def format_models_summary(
    models: list[Any],
    default_model: str = "—",
    current_session_model: str = "—",
) -> str:
    available = sum(
        1
        for model in models
        if isinstance(model, dict)
        and model.get("available") is True
        and model.get("missing") is not True
    )
    unavailable = sum(
        1
        for model in models
        if isinstance(model, dict)
        and (model.get("available") is False or model.get("missing") is True)
    )
    lines = [
        (
            f"📚 โมเดลทั้งหมด {len(models)} รายการ "
            f"(พร้อมใช้ {available} · ใช้งานไม่ได้ {unavailable})  "
        ),
        f"Default: {_markdown_text(default_model)}  ",
        f"Current session: {_markdown_text(current_session_model)}",
    ]

    if not models:
        lines.extend(["", "ไม่พบโมเดลที่ตั้งค่าไว้"])
        return "\n".join(lines)

    indexed_models = list(enumerate(models, start=1))
    groups = [
        (
            "Configured",
            [
                item
                for item in indexed_models
                if isinstance(item[1], dict) and bool(item[1].get("tags"))
            ],
        ),
        (
            "No tags",
            [
                item
                for item in indexed_models
                if not isinstance(item[1], dict) or not item[1].get("tags")
            ],
        ),
    ]

    for group_name, group_models in groups:
        if not group_models:
            continue

        lines.extend(["", f"{group_name}:"])
        for index, model in group_models:
            if not isinstance(model, dict):
                lines.append(f"- รายการที่ {index} — รูปแบบข้อมูลไม่ถูกต้อง")
                continue

            name = _markdown_text(model.get("name"), f"โมเดล {index}")
            key = _markdown_text(model.get("key"))
            model_input = _markdown_text(model.get("input"))
            lines.append(f"- {name} — {key} · input: {model_input}")

    return "\n".join(lines)
