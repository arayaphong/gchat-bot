from __future__ import annotations

from typing import Any

BUSY_TEXT = "⏳ ระบบกำลังคิดตอบสำหรับข้อความก่อนหน้าอยู่ กรุณาส่งใหม่อีกครั้งภายหลัง"
ATTACHMENT_BUSY_TEMPLATE = (
    "⏳ Jinx กำลังประมวลผลข้อความก่อนหน้า "
    "จึงยังไม่ได้ดาวน์โหลดไฟล์แนบ {count} ไฟล์ "
    "กรุณาส่งข้อความและไฟล์ใหม่อีกครั้งภายหลัง"
)
ATTACHMENT_COMMAND_IGNORED_TEMPLATE = (
    "ℹ️ คำสั่ง {command} ไม่รองรับไฟล์แนบ Jinx จึงไม่ได้ประมวลผลไฟล์ {count} ไฟล์{names}"
)
ATTACHMENT_LIMIT_TEMPLATE = (
    "⚠️ ได้รับไฟล์แนบ {total} ไฟล์ "
    "แต่ Jinx รองรับได้สูงสุด {limit} ไฟล์ต่อข้อความ "
    "จึงข้ามไฟล์ท้ายสุด {ignored} ไฟล์{names}"
)
ATTACHMENT_DOWNLOAD_FAILURE_TEXT = (
    "❌ Jinx เริ่มดาวน์โหลดไฟล์แนบไม่สำเร็จ กรุณาลองส่งไฟล์ใหม่อีกครั้ง"
)
ATTACHMENT_REMOTE_UNAVAILABLE_TEXT = (
    "❌ Jinx ดาวน์โหลดไฟล์แล้ว "
    "แต่ช่องทางโมเดลปัจจุบันเข้าถึงไฟล์ในเครื่องบอตไม่ได้ "
    "จึงไม่ได้ส่งไฟล์ให้โมเดล กรุณาแจ้งผู้ดูแลระบบ{names}"
)
ATTACHMENT_CLEANUP_FAILURE_TEMPLATE = (
    "⚠️ Jinx ไม่สามารถลบไฟล์ชั่วคราวได้ {failed} ไฟล์ (ลบสำเร็จ {cleaned} ไฟล์) กรุณาแจ้งผู้ดูแลระบบ"
)
OUTBOUND_ATTACHMENT_FAILURE_TEMPLATE = (
    "❌ Jinx ส่งไฟล์ {name} ไม่สำเร็จหลังลองแล้ว {attempts} ครั้ง กรุณาลองสร้างไฟล์ใหม่อีกครั้ง"
)
OUTBOUND_ATTACHMENT_REJECTED_TEMPLATE = "❌ Jinx ส่งไฟล์ {name} ไม่สำเร็จ: {reason}"
ABORT_SUCCESS_TEXT = "✅ หยุดการทำงานสำเร็จ"
ABORT_FAILURE_TEMPLATE = "❌ หยุดการทำงานไม่สำเร็จ: {reason}"
NEW_SESSION_SUCCESS_TEMPLATE = "🔄 เริ่มเซสชั่นใหม่โดยคงโมเดล {model} แล้ว"
NEW_SESSION_FAILURE_TEMPLATE = (
    "❌ ไม่สามารถเริ่มเซสชั่นใหม่โดยคงโมเดลเดิมได้: {reason} เซสชั่นเดิมยังคงใช้งานอยู่"
)
MODELS_FAILURE_TEMPLATE = "❌ ไม่สามารถแสดงรายการโมเดลได้: {reason}"
MODEL_COMMAND_USAGE_TEXT = "ℹ️ วิธีใช้: /model <model-key> (ดูรายการด้วย /models)"
MODEL_NOT_FOUND_TEMPLATE = "❌ ไม่พบโมเดล: {model} (ดูรายการด้วย /models)"
MODEL_UNAVAILABLE_TEMPLATE = "❌ โมเดลไม่พร้อมใช้งาน: {model}"
MODEL_VALIDATION_FAILURE_TEMPLATE = "❌ ไม่สามารถตรวจสอบโมเดลได้: {reason}"
MODEL_SESSION_SUCCESS_TEMPLATE = (
    "🔄 เริ่มเซสชั่นใหม่ด้วยโมเดล {model} แล้ว บริบทการสนทนาเดิมจะไม่ถูกนำมาใช้"
)
MODEL_SESSION_FAILURE_TEMPLATE = (
    "❌ ไม่สามารถเริ่มเซสชั่นใหม่ด้วยโมเดล {model}: {reason} เซสชั่นเดิมยังคงใช้งานอยู่"
)


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


def format_model_session_success(model_key: str) -> str:
    return MODEL_SESSION_SUCCESS_TEMPLATE.format(model=_markdown_text(model_key))


def format_model_session_failure(model_key: str, reason: Any) -> str:
    return MODEL_SESSION_FAILURE_TEMPLATE.format(
        model=_markdown_text(model_key),
        reason=_markdown_text(reason),
    )


def format_new_session_success(model_key: str) -> str:
    return NEW_SESSION_SUCCESS_TEMPLATE.format(model=_markdown_text(model_key))


def format_new_session_failure(reason: Any) -> str:
    return NEW_SESSION_FAILURE_TEMPLATE.format(reason=_markdown_text(reason))


def format_attachment_busy(count: int) -> str:
    return ATTACHMENT_BUSY_TEMPLATE.format(count=count)


def _format_attachment_names(names: list[Any], heading: str) -> str:
    if not names:
        return ""
    return (
        "\n" + heading + "\n" + "\n".join(f"- {_markdown_text(name)}" for name in names)
    )


def format_attachment_command_ignored(command: str, names: list[Any]) -> str:
    command_name = command.strip().split(maxsplit=1)[0] if command.strip() else "คำสั่ง"
    return ATTACHMENT_COMMAND_IGNORED_TEMPLATE.format(
        command=_markdown_text(command_name),
        count=len(names),
        names=_format_attachment_names(names, "ไฟล์ที่ข้าม:"),
    )


def format_attachment_limit(
    total: int,
    limit: int,
    ignored_names: list[Any],
) -> str:
    return ATTACHMENT_LIMIT_TEMPLATE.format(
        total=total,
        limit=limit,
        ignored=max(0, total - limit),
        names=_format_attachment_names(ignored_names, "ไฟล์ที่ข้าม:"),
    )


def format_attachment_download_failure(names: list[Any]) -> str:
    return ATTACHMENT_DOWNLOAD_FAILURE_TEXT + _format_attachment_names(
        names, "ไฟล์ที่ดาวน์โหลดไม่สำเร็จ:"
    )


def format_attachment_download_result(
    total: int,
    failures: list[tuple[Any, Any]],
) -> str:
    lines = [
        f"⚠️ Jinx ดาวน์โหลดไฟล์แนบไม่สำเร็จ {len(failures)}/{total} ไฟล์",
        "ไฟล์ที่ไม่สำเร็จ:",
    ]
    lines.extend(
        f"- {_markdown_text(name)}: {_markdown_text(str(reason)[:300])}"
        for name, reason in failures
    )
    return "\n".join(lines)


def format_attachment_remote_unavailable(names: list[Any]) -> str:
    return ATTACHMENT_REMOTE_UNAVAILABLE_TEXT.format(
        names=_format_attachment_names(names, "ไฟล์ที่ไม่ได้ส่งให้โมเดล:"),
    )


def format_attachment_cleanup(
    cleaned: int,
    failures: list[tuple[Any, Any]],
) -> str:
    message = ATTACHMENT_CLEANUP_FAILURE_TEMPLATE.format(
        cleaned=cleaned,
        failed=len(failures),
    )
    details = "\n".join(
        f"- {_markdown_text(name)}: {_markdown_text(str(reason)[:300])}"
        for name, reason in failures
    )
    return f"{message}\n{details}"


def format_outbound_attachment_failure(
    name: Any,
    attempts: int,
    error_category: str = "delivery_failed",
) -> str:
    validation_reasons = {
        "empty_file": "ไฟล์ว่างเปล่า",
        "file_too_large": "ขนาดไฟล์เกินขีดจำกัดของระบบ",
        "staging_unavailable": "ไม่พบสำเนาไฟล์ที่เตรียมไว้สำหรับส่ง",
        "file_unstable": "ไฟล์ยังเขียนไม่เสร็จภายในเวลาที่กำหนด",
        "source_unavailable": "ไฟล์ต้นทางหายไปก่อนที่ Jinx จะเตรียมส่ง",
        "staging_failed": "Jinx ไม่สามารถอ่านหรือเตรียมสำเนาไฟล์ได้",
    }
    reason = validation_reasons.get(error_category)
    if reason:
        return OUTBOUND_ATTACHMENT_REJECTED_TEMPLATE.format(
            name=_markdown_text(name, "ไฟล์ไม่ทราบชื่อ"),
            reason=reason,
        )
    return OUTBOUND_ATTACHMENT_FAILURE_TEMPLATE.format(
        name=_markdown_text(name, "ไฟล์ไม่ทราบชื่อ"),
        attempts=max(1, attempts),
    )


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
