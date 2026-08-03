from __future__ import annotations

from typing import Any

BUSY_TEXT = "⏳ ระบบกำลังคิดตอบสำหรับข้อความก่อนหน้าอยู่ กรุณาส่งใหม่อีกครั้งภายหลัง"
ABORT_SUCCESS_TEXT = "✅ หยุดการทำงานสำเร็จ"
ABORT_FAILURE_TEMPLATE = "❌ หยุดการทำงานไม่สำเร็จ: {reason}"
NEW_SESSION_TEXT = "🔄 เริ่มเซสชั่นใหม่แล้ว"
MODELS_FAILURE_TEMPLATE = "❌ ไม่สามารถแสดงรายการโมเดลได้: {reason}"


def _markdown_text(value: Any, fallback: str = "—") -> str:
    if value is None:
        return fallback

    text = " ".join(str(value).split())
    if not text:
        return fallback

    for character in "\\`*{}_[]<>#":
        text = text.replace(character, f"\\{character}")
    return text


def _context_window(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _markdown_text(value)


def _model_status(model: dict[str, Any]) -> str:
    if model.get("missing") is True:
        return "❌ ขาดการตั้งค่า"
    if model.get("available") is True:
        return "✅ พร้อมใช้"
    if model.get("available") is False:
        return "⚠️ ใช้งานไม่ได้"
    return "❔ ไม่ทราบสถานะ"


def _model_location(value: Any) -> str:
    if value is True:
        return "local"
    if value is False:
        return "remote"
    return "—"


def _model_tags(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "—"
    return ", ".join(_markdown_text(tag) for tag in value)


def format_models_summary(models: list[Any]) -> str:
    if not models:
        return "📚 ไม่พบโมเดลที่ตั้งค่าไว้"

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
            f"📚 **โมเดลทั้งหมด {len(models)} รายการ** "
            f"(พร้อมใช้ {available} · ใช้งานไม่ได้ {unavailable})"
        )
    ]

    for index, model in enumerate(models, start=1):
        if not isinstance(model, dict):
            lines.append(f"- **รายการที่ {index}** — รูปแบบข้อมูลไม่ถูกต้อง")
            continue

        name = _markdown_text(model.get("name"), f"โมเดล {index}")
        key = _markdown_text(model.get("key"))
        model_input = _markdown_text(model.get("input"))
        context = _context_window(model.get("contextWindow"))
        location = _model_location(model.get("local"))
        status = _model_status(model)
        tags = _model_tags(model.get("tags"))
        lines.append(
            f"- **{name}** — {key} · input: {model_input} · context: {context} · "
            f"{location} · {status} · tags: {tags}"
        )

    return "\n".join(lines)
