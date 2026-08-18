"""คำสั่ง /schedule — ตั้งเตือนผ่าน OpenClaw cron ให้ผลกลับเข้า thread เดิม

งานที่สร้างจากคำสั่งนี้ pin เข้า session ของ thread ที่ผู้ใช้สั่งด้วย
--session session:<key> (key derive จาก event โดย ChatSessionContext เสมอ
ห้ามรับจากข้อความผู้ใช้) เมื่อครบกำหนด OpenClaw จะ inject message เข้า
session เดิม และ SessionTrajectoryWatcher ส่งผลลัพธ์กลับ Google Chat
thread นั้นอัตโนมัติ
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from helpers.providers.openclaw_cli import cron_add, cron_list, cron_remove
from helpers.session_keys import SESSION_AGENT

SCHEDULE_TIMEZONE = "Asia/Bangkok"
_SCHEDULE_ZONE = ZoneInfo(SCHEDULE_TIMEZONE)
# กันการพิมพ์ผิดแล้วตั้งเตือนล่วงหน้านานผิดความหมาย
MAX_SCHEDULE_LEAD_DAYS = 366
_LEAD_TIME_EXCEEDED_TEXT = f"ตั้งเตือนล่วงหน้าได้ไม่เกิน {MAX_SCHEDULE_LEAD_DAYS} วัน"
JOB_NAME_PREFIX = "gchat"

_DURATION_SEGMENT_RE = re.compile(r"(\d+)([smhd])")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"(?:\d+[smhd])+")
_CLOCK_RE = re.compile(r"[01]?\d:[0-5]\d|2[0-3]:[0-5]\d")
_DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# ข้อความที่ inject เข้า session ตอนครบกำหนด — ห่อด้วยบริบทเสมอ
# เพื่อไม่ให้ agent เข้าใจผิดว่าเป็นคำสั่งตั้งเตือนใหม่จากผู้ใช้
CRON_MESSAGE_TEMPLATE = (
    "การแจ้งเตือนที่ตั้งเวลาไว้ของ Jinx ครบกำหนดแล้ว: “{message}” — "
    "กรุณาส่งข้อความแจ้งเตือนนี้ถึงผู้ใช้ตอนนี้ ตอบสั้นกระชับ "
    "ตรงประเด็น และไม่ต้องถามยืนยันเพิ่มเติม"
)

USAGE_TEXT = (
    "⏰ วิธีใช้คำสั่ง /schedule\n"
    "- /schedule +10m <ข้อความ> — เตือนในอีก 10 นาที (หน่วย: s m h d)\n"
    "- /schedule at 18:30 <ข้อความ> — เตือนเวลา 18:30 วันนี้\n"
    "- /schedule at 2026-08-14 09:00 <ข้อความ> — เตือนวันเวลาที่ระบุ\n"
    "- /schedule list — ดูงานตั้งเวลาของ thread นี้\n"
    "- /schedule cancel <id> — ยกเลิกงานของ thread นี้"
)


class ScheduleCommandError(ValueError):
    """ข้อผิดพลาดจากการแปลงคำสั่ง /schedule ที่ต้องตอบกลับผู้ใช้"""


@dataclass(frozen=True)
class ScheduleSpec:
    """งานตั้งเตือนครั้งเดียวที่แปลงจากคำสั่งผู้ใช้แล้ว"""

    when: str  # ค่าที่ส่งให้ --at ("+10m" หรือ ISO datetime)
    message: str


@dataclass(frozen=True)
class ParsedScheduleCommand:
    kind: str  # "usage" | "list" | "cancel" | "add"
    spec: ScheduleSpec | None = None
    job_ref: str = ""


def parse_duration_seconds(duration: str) -> int:
    """แปลง '10m', '1h30m' เป็นวินาที — raise ScheduleCommandError ถ้าไม่ถูกต้อง"""

    text = duration.strip().lower()
    if not _DURATION_RE.fullmatch(text):
        raise ScheduleCommandError(f"รูปแบบระยะเวลาไม่ถูกต้อง: {duration}")
    total_seconds = sum(
        int(match.group(1)) * _DURATION_UNIT_SECONDS[match.group(2)]
        for match in _DURATION_SEGMENT_RE.finditer(text)
    )
    if total_seconds <= 0:
        raise ScheduleCommandError(f"รูปแบบระยะเวลาไม่ถูกต้อง: {duration}")
    if total_seconds > MAX_SCHEDULE_LEAD_DAYS * 86400:
        raise ScheduleCommandError(_LEAD_TIME_EXCEEDED_TEXT)
    return total_seconds


def resolve_at_value(raw_when: str, now: datetime) -> str:
    """คืนค่าที่ส่งให้ --at หลังตรวจรูปแบบแล้ว ('+dur' หรือ ISO datetime)"""

    when = raw_when.strip()
    if when.startswith("+"):
        seconds = parse_duration_seconds(when[1:])
        return f"+{seconds}s"

    if _CLOCK_RE.fullmatch(when):
        hour_text, minute_text = when.split(":", 1)
        candidate = now.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.isoformat()

    try:
        parsed = datetime.fromisoformat(when.replace(" ", "T"))
    except ValueError as error:
        raise ScheduleCommandError(f"รูปแบบเวลาไม่ถูกต้อง: {raw_when}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if parsed <= now:
        raise ScheduleCommandError("เวลาที่ระบุผ่านมาแล้ว กรุณาระบุเวลาในอนาคต")
    if parsed > now + timedelta(days=MAX_SCHEDULE_LEAD_DAYS):
        raise ScheduleCommandError(_LEAD_TIME_EXCEEDED_TEXT)
    return parsed.isoformat()


def parse_schedule_args(
    args: str,
    now: datetime | None = None,
) -> ParsedScheduleCommand:
    """แปลง argument หลัง /schedule เป็นคำสั่งที่ชัดเจน"""

    text = args.strip()
    if not text:
        return ParsedScheduleCommand(kind="usage")
    lowered = text.lower()
    if lowered == "list":
        return ParsedScheduleCommand(kind="list")
    if lowered == "cancel" or lowered.startswith("cancel "):
        job_ref = text[len("cancel") :].strip()
        if not job_ref:
            raise ScheduleCommandError("กรุณาระบุ id ของงานที่ต้องการยกเลิก")
        return ParsedScheduleCommand(kind="cancel", job_ref=job_ref)

    if lowered.startswith("in "):
        remainder = text[3:].strip()
        when_token, _, message = remainder.partition(" ")
        when_token = f"+{when_token}"
    elif lowered.startswith("at "):
        remainder = text[3:].strip()
        first_token, _, rest = remainder.partition(" ")
        if _DATE_ONLY_RE.fullmatch(first_token):
            # รูปแบบ "2026-08-14 09:00" มีช่องว่างคั่นกลาง — ต้องอ่าน 2 token
            # strip() ก่อน partition กันช่องว่างซ้ำระหว่างวันที่กับเวลา
            # ทำให้ token เวลาหลุดหายไปเป็นช่องว่างว่างเปล่า
            second_token, _, message = rest.strip().partition(" ")
            when_token = f"{first_token} {second_token}"
        else:
            when_token, message = first_token, rest
    elif text.startswith("+") or _CLOCK_RE.fullmatch(text.split(" ", 1)[0] or ""):
        when_token, _, message = text.partition(" ")
    else:
        return ParsedScheduleCommand(kind="usage")

    message = message.strip()
    if not message:
        return ParsedScheduleCommand(kind="usage")
    at_value = resolve_at_value(when_token, now or datetime.now(_SCHEDULE_ZONE))
    return ParsedScheduleCommand(
        kind="add",
        spec=ScheduleSpec(when=at_value, message=message),
    )


def build_cron_add_argv(spec: ScheduleSpec, session_key: str) -> list[str]:
    """สร้าง argv ของ openclaw cron add — session_key มาจาก event เท่านั้น"""

    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError("session_key must be a non-empty string")
    if not spec.message.strip():
        raise ValueError("schedule message must be a non-empty string")
    name = f"{JOB_NAME_PREFIX}-{uuid.uuid4().hex[:8]}"
    return [
        name,
        "--agent",
        SESSION_AGENT,
        "--at",
        spec.when,
        "--tz",
        SCHEDULE_TIMEZONE,
        "--delete-after-run",
        "--session",
        f"session:{session_key}",
        # ใช้รูปแบบ --flag=value ตัวเดียว ไม่ใช่ 2 argv element แยกกัน
        # เพราะข้อความอาจขึ้นต้นด้วย "-" ซึ่ง CLI parser ของ openclaw
        # อาจตีความเป็น flag ใหม่แทนที่จะเป็นค่าของ --display-name
        f"--display-name={spec.message[:80]}",
        "--message",
        CRON_MESSAGE_TEMPLATE.format(message=spec.message),
        "--json",
    ]


def _load_json_payload(result: Any, command_label: str) -> dict[str, Any]:
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:300]
        raise RuntimeError(f"openclaw cron {command_label} ล้มเหลว: {detail}")
    raw_output = (result.stdout or "").lstrip("\ufeff").strip()
    try:
        payload = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(
            f"openclaw cron {command_label} ส่ง JSON กลับมาไม่ถูกต้อง"
        ) from error
    if not isinstance(payload, dict):
        raise TypeError(f"รูปแบบข้อมูลจาก openclaw cron {command_label} ไม่ถูกต้อง")
    return payload


def _job_belongs_to_session(job: dict[str, Any], session_key: str) -> bool:
    # job ที่ pin เข้า session ด้วย --session session:<key> จะเก็บ key ไว้ใน
    # sessionTarget ไม่ใช่ sessionKey — รองรับทั้งสองรูปแบบ
    return session_key in (
        job.get("sessionKey"),
        str(job.get("sessionTarget", "")).removeprefix("session:"),
    )


def list_session_jobs(session_key: str) -> list[dict[str, Any]]:
    """คืนเฉพาะ job ที่ผูกกับ session นี้ (รวม disabled) เรียงตามรอบถัดไป"""

    payload = _load_json_payload(cron_list(), "list")
    if not isinstance(payload.get("jobs"), list):
        raise TypeError("รูปแบบข้อมูลจาก openclaw cron list ไม่ถูกต้อง")
    jobs = [
        job
        for job in payload["jobs"]
        if isinstance(job, dict) and _job_belongs_to_session(job, session_key)
    ]
    return sorted(jobs, key=_next_run_sort_key)


def _next_run_sort_key(job: dict[str, Any]) -> float:
    # ต้องแยกจาก None/ค่าที่ไม่ใช่ตัวเลขด้วย isinstance ตรงๆ — ใช้ "or" ไม่ได้
    # เพราะ 0 (เวลาที่ครบกำหนดไปแล้ว) จะถูกตีความเป็นค่าว่างไปด้วย
    value = job.get("nextRunAtMs")
    return float(value) if isinstance(value, (int, float)) else float("inf")


def resolve_session_job(job_ref: str, session_key: str) -> dict[str, Any]:
    """หา job ของ session นี้จาก id หรือ prefix ของ id — ห้ามแตะ job ของ session อื่น"""

    ref = job_ref.strip().lower()
    if not ref:
        raise ScheduleCommandError("กรุณาระบุ id ของงานที่ต้องการยกเลิก")
    matches = [
        job
        for job in list_session_jobs(session_key)
        if str(job.get("id", "")).lower().startswith(ref)
    ]
    if not matches:
        raise ScheduleCommandError(f"ไม่พบงานตั้งเวลา id '{job_ref}' ใน thread นี้")
    if len(matches) > 1:
        raise ScheduleCommandError(
            f"id '{job_ref}' ตรงกับหลายงาน กรุณาระบุ id ให้ยาวขึ้น"
        )
    return matches[0]


def add_session_job(spec: ScheduleSpec, session_key: str) -> dict[str, Any]:
    payload = _load_json_payload(cron_add(build_cron_add_argv(spec, session_key)), "add")
    if not isinstance(payload.get("id"), str):
        raise TypeError("รูปแบบผลลัพธ์จาก openclaw cron add ไม่ถูกต้อง")
    return payload


def remove_session_job(job_id: str) -> None:
    _load_json_payload(cron_remove(job_id), "rm")


__all__ = [
    "MAX_SCHEDULE_LEAD_DAYS",
    "SCHEDULE_TIMEZONE",
    "USAGE_TEXT",
    "ParsedScheduleCommand",
    "ScheduleCommandError",
    "ScheduleSpec",
    "add_session_job",
    "build_cron_add_argv",
    "list_session_jobs",
    "parse_duration_seconds",
    "parse_schedule_args",
    "remove_session_job",
    "resolve_at_value",
    "resolve_session_job",
]
