from __future__ import annotations

import hashlib
import html
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from helpers.chat_gateway import ChatGateway
from helpers.chat_history_client import ChatHistoryClient, ChatHistoryClientError
from helpers.services import CredentialReadinessError

BANGKOK = ZoneInfo("Asia/Bangkok")

_RESOURCE_SEGMENT = r"[^\s/\x00-\x1f\x7f]+"
_MESSAGE_NAME_RE = re.compile(
    rf"^(?P<space>spaces/{_RESOURCE_SEGMENT})/messages/{_RESOURCE_SEGMENT}$"
)
_CREATE_TIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


class ChatHistoryStatsErrorCode(str, Enum):
    INVALID_SOURCE_MESSAGE = "invalid_source_message"
    MALFORMED_MESSAGE = "malformed_message"


class ChatHistoryStatsError(RuntimeError):
    def __init__(self, code: ChatHistoryStatsErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class _ParsedTimestamp:
    instant_nanoseconds: int
    utc_datetime: datetime


@dataclass(frozen=True, slots=True)
class ChatMessageStats:
    total: int
    human: int
    bot: int
    unknown: int
    first_create_time: datetime | None
    latest_create_time: datetime | None
    duration_seconds: float | None


def _parse_create_time(value: object) -> _ParsedTimestamp:
    if not isinstance(value, str) or len(value) > 64:
        raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
    match = _CREATE_TIME_RE.fullmatch(value)
    if match is None:
        raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)

    fraction = match.group("fraction") or ""
    microseconds = (fraction + "000000")[:6]
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    normalized = f"{match.group('date')}T{match.group('time')}.{microseconds}{zone}"
    try:
        parsed = datetime.fromisoformat(normalized)
        utc_value = parsed.astimezone(timezone.utc)
        epoch_seconds = int(utc_value.replace(microsecond=0).timestamp())
    except (OverflowError, ValueError):
        raise ChatHistoryStatsError(
            ChatHistoryStatsErrorCode.MALFORMED_MESSAGE
        ) from None
    nanoseconds = int((fraction + "000000000")[:9])
    return _ParsedTimestamp(
        instant_nanoseconds=(epoch_seconds * 1_000_000_000) + nanoseconds,
        utc_datetime=utc_value,
    )


def reduce_chat_message_stats(
    messages: Iterable[Mapping[str, Any]],
    *,
    space_name: str,
    source_message_name: str,
) -> ChatMessageStats:
    """Reduce metadata only; malformed records abort rather than return partial data."""

    source_match = _MESSAGE_NAME_RE.fullmatch(source_message_name)
    if source_match is None or source_match.group("space") != space_name:
        raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.INVALID_SOURCE_MESSAGE)

    total = human = bot = unknown = 0
    first: _ParsedTimestamp | None = None
    latest: _ParsedTimestamp | None = None

    for message in messages:
        if not isinstance(message, Mapping):
            raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
        name = message.get("name")
        name_match = _MESSAGE_NAME_RE.fullmatch(name) if isinstance(name, str) else None
        if name_match is None or name_match.group("space") != space_name:
            raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
        if name == source_message_name:
            continue

        created = _parse_create_time(message.get("createTime"))
        sender = message.get("sender")
        sender_type: str | None = None
        if sender is not None:
            if not isinstance(sender, Mapping):
                raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
            raw_sender_type = sender.get("type")
            raw_sender_name = sender.get("name")
            if raw_sender_type is not None and not isinstance(raw_sender_type, str):
                raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
            if raw_sender_name is not None and not isinstance(raw_sender_name, str):
                raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
            sender_type = raw_sender_type

        total += 1
        if sender_type == "HUMAN":
            human += 1
        elif sender_type == "BOT":
            bot += 1
        else:
            unknown += 1

        if first is None or created.instant_nanoseconds < first.instant_nanoseconds:
            first = created
        if latest is None or created.instant_nanoseconds > latest.instant_nanoseconds:
            latest = created

    duration = None
    if first is not None and latest is not None:
        duration = (
            latest.instant_nanoseconds - first.instant_nanoseconds
        ) / 1_000_000_000
    return ChatMessageStats(
        total=total,
        human=human,
        bot=bot,
        unknown=unknown,
        first_create_time=first.utc_datetime if first else None,
        latest_create_time=latest.utc_datetime if latest else None,
        duration_seconds=duration,
    )


class ChatHistoryPresenter:
    @staticmethod
    def _card(paragraphs: list[str]) -> dict[str, Any]:
        widgets = [
            {"textParagraph": {"text": html.escape(paragraph)}}
            for paragraph in paragraphs
        ]
        return {
            "cardsV2": [
                {
                    "cardId": "jinx-chat-history",
                    "card": {
                        "header": {"title": "🛠️ ผู้ดูแลระบบ"},
                        "sections": [{"widgets": widgets}],
                    },
                }
            ]
        }

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.astimezone(BANGKOK).strftime("%d/%m/%Y %H:%M:%S น. (Asia/Bangkok)")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        remaining = max(0, int(seconds))
        days, remaining = divmod(remaining, 86_400)
        hours, remaining = divmod(remaining, 3_600)
        minutes, seconds_part = divmod(remaining, 60)
        pieces: list[str] = []
        if days:
            pieces.append(f"{days} วัน")
        if hours:
            pieces.append(f"{hours} ชั่วโมง")
        if minutes:
            pieces.append(f"{minutes} นาที")
        if seconds_part or not pieces:
            pieces.append(f"{seconds_part} วินาที")
        return " ".join(pieces)

    def build_stats_message(self, stats: ChatMessageStats) -> dict[str, Any]:
        paragraphs = [
            f"ข้อความทั้งหมด: {stats.total}",
            (f"ผู้ใช้: {stats.human} | บอต: {stats.bot} | ไม่ทราบประเภท: {stats.unknown}"),
        ]
        if (
            stats.total
            and stats.first_create_time is not None
            and stats.latest_create_time is not None
            and stats.duration_seconds is not None
        ):
            paragraphs.extend(
                [
                    f"ข้อความแรก: {self._format_time(stats.first_create_time)}",
                    f"ข้อความล่าสุด: {self._format_time(stats.latest_create_time)}",
                    f"ช่วงเวลา: {self._format_duration(stats.duration_seconds)}",
                ]
            )
        return self._card(paragraphs)

    def build_failure_message(self, category: str) -> dict[str, Any]:
        if category in {"missing_granted_scope", "reauthorize_required"}:
            notice = "❌ สิทธิ์ Google Chat สำหรับอ่านประวัติไม่พร้อม กรุณาอนุญาตบัญชีผู้ใช้อีกครั้ง"
        elif category in {
            "unauthorized_user",
            "unauthorized_space",
            "invalid_resource_name",
        }:
            notice = "❌ บัญชีผู้ใช้หรือห้องสนทนานี้ไม่ได้รับอนุญาต"
        elif category == "not_direct_message":
            notice = "❌ คำสั่งนี้ใช้ได้เฉพาะห้องข้อความส่วนตัวกับบอต"
        elif category == "invalid_source_message":
            notice = "❌ ไม่พบตัวตนข้อความคำสั่งที่ปลอดภัย จึงไม่อ่านประวัติ"
        else:
            notice = "❌ ไม่สามารถสรุปประวัติแชทได้ในขณะนี้ กรุณาลองใหม่ภายหลัง"
        return self._card([notice])


class ChatHistoryService:
    """Schedule read-only stats after the webhook response can be acknowledged."""

    def __init__(
        self,
        client: ChatHistoryClient,
        gateway: ChatGateway,
        presenter: ChatHistoryPresenter | None = None,
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._client = client
        self._gateway = gateway
        self._presenter = presenter or ChatHistoryPresenter()
        self._thread_factory = thread_factory

    @staticmethod
    def deterministic_message_id(
        source_message_name: str | None,
        *,
        actor_name: str | None = None,
        space_name: str | None = None,
    ) -> str:
        identity = (
            f"source:{source_message_name}"
            if source_message_name
            else f"missing:{actor_name or ''}:{space_name or ''}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return f"client-jinx-hs-{digest}"

    @staticmethod
    def _source_is_valid(
        source_message_name: str | None, space_name: str | None
    ) -> bool:
        match = (
            _MESSAGE_NAME_RE.fullmatch(source_message_name)
            if isinstance(source_message_name, str)
            else None
        )
        return match is not None and match.group("space") == space_name

    def submit_stats(
        self,
        *,
        actor_name: str | None,
        space_name: str | None,
        thread_name: str | None,
        source_message_name: str | None,
    ) -> bool:
        initial_error: BaseException | None = None
        try:
            self._client.validate_authority(actor_name, space_name)
            if not self._source_is_valid(source_message_name, space_name):
                raise ChatHistoryStatsError(
                    ChatHistoryStatsErrorCode.INVALID_SOURCE_MESSAGE
                )
        except (ChatHistoryClientError, ChatHistoryStatsError) as error:
            initial_error = error

        try:
            worker = self._thread_factory(
                target=self._run_stats,
                kwargs={
                    "actor_name": actor_name,
                    "space_name": space_name,
                    "thread_name": thread_name,
                    "source_message_name": source_message_name,
                    "initial_error": initial_error,
                },
                name="jinx-chat-history-stats",
                daemon=True,
            )
            worker.start()
            return True
        except Exception as error:  # noqa: BLE001
            print(
                f"❌ [chat-history] cannot start stats worker: {type(error).__name__}"
            )
            return False

    @staticmethod
    def _error_category(error: BaseException) -> str:
        if isinstance(error, ChatHistoryClientError):
            return error.code.value
        if isinstance(error, ChatHistoryStatsError):
            return error.code.value
        if isinstance(error, CredentialReadinessError):
            return error.code
        return "stats_failure"

    def _run_stats(
        self,
        *,
        actor_name: str | None,
        space_name: str | None,
        thread_name: str | None,
        source_message_name: str | None,
        initial_error: BaseException | None = None,
    ) -> None:
        category = ""
        try:
            if initial_error is not None:
                raise initial_error
            if not self._source_is_valid(source_message_name, space_name):
                raise ChatHistoryStatsError(
                    ChatHistoryStatsErrorCode.INVALID_SOURCE_MESSAGE
                )
            messages = self._client.iter_messages(
                actor_name=actor_name,
                space_name=space_name,
            )
            stats = reduce_chat_message_stats(
                messages,
                space_name=space_name,
                source_message_name=source_message_name,
            )
            message = self._presenter.build_stats_message(stats)
        except Exception as error:  # noqa: BLE001
            category = self._error_category(error)
            print(f"❌ [chat-history] stats failed: {category}")
            message = self._presenter.build_failure_message(category)

        if not isinstance(space_name, str) or space_name != self._client.allowed_space:
            return
        message_id = self.deterministic_message_id(
            source_message_name,
            actor_name=actor_name,
            space_name=space_name,
        )
        delivered = self._gateway.send_structured_card(
            space_name,
            thread_name or "",
            message,
            message_id=message_id,
        )
        if delivered is None:
            safe_category = category or "delivery_failure"
            print(f"❌ [chat-history] stats card not delivered: {safe_category}")


__all__ = [
    "BANGKOK",
    "ChatHistoryPresenter",
    "ChatHistoryService",
    "ChatHistoryStatsError",
    "ChatHistoryStatsErrorCode",
    "ChatMessageStats",
    "reduce_chat_message_stats",
]
