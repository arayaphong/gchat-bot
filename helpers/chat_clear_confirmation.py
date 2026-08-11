from __future__ import annotations

import hashlib
import html
import math
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from helpers.chat_clear_store import (
    CHAT_WRITE_MIN_INTERVAL_SECONDS,
    ChatClearStore,
    ChatClearStoreConflictError,
    ClearJob,
    CredentialPartition,
    JobStatus,
    SnapshotItem,
)
from helpers.chat_gateway import (
    ChatGateway,
    ChatMessageNotFoundError,
)
from helpers.chat_history_client import (
    ChatHistoryClient,
    ChatHistoryClientError,
    ChatHistoryClientErrorCode,
)
from helpers.chat_history_service import (
    BANGKOK,
    ChatHistoryStatsError,
    ChatHistoryStatsErrorCode,
    _parse_create_time,
)

_UTC = timezone.utc
_SNAPSHOT_BATCH_SIZE = 250
_RECONCILIATION_DELAYS = (0.0, 0.25, 0.5, 1.0)
_MAX_DELIVERY_GENERATIONS_PER_RUN = 2


def _message_card(widgets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cardsV2": [
            {
                "cardId": "jinx-chat-clear",
                "card": {
                    "header": {"title": "🧹 ล้างประวัติแชท"},
                    "sections": [{"widgets": widgets}],
                },
            }
        ]
    }


class ChatClearPresenter:
    @staticmethod
    def _paragraph(text: str) -> dict[str, Any]:
        return {"textParagraph": {"text": html.escape(text)}}

    def build_confirmation_message(
        self,
        job: ClearJob,
        *,
        confirm_handle: str,
        cancel_handle: str,
        action_url: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        cutoff = datetime.fromisoformat(job.cutoff_utc.replace("Z", "+00:00"))
        cutoff_display = cutoff.astimezone(BANGKOK).strftime(
            "%d/%m/%Y %H:%M:%S น. (Asia/Bangkok)"
        )
        writes = job.candidate_human + job.candidate_bot + 2
        minimum_seconds = math.ceil(writes * CHAT_WRITE_MIN_INTERVAL_SECONDS)

        def button(text: str, handle: str) -> dict[str, Any]:
            return {
                "text": text,
                "onClick": {
                    "action": {
                        "function": action_url,
                        "parameters": [
                            {"key": "historyActionHandle", "value": handle}
                        ],
                    }
                },
            }

        widgets = [
            self._paragraph(
                f"จะลบเฉพาะข้อความที่สร้างก่อน {cutoff_display} อย่างเคร่งครัด"
            ),
            self._paragraph(
                "ข้อความผู้ใช้: "
                f"{job.candidate_human} | ข้อความบอต: {job.candidate_bot} | "
                f"ข้าม: {job.candidate_skipped}"
            ),
            self._paragraph(
                f"ใช้เวลาอย่างน้อยประมาณ {minimum_seconds} วินาที "
                f"และคำยืนยันหมดอายุใน {math.ceil(ttl_seconds / 60)} นาที"
            ),
            {
                "buttonList": {
                    "buttons": [
                        button("ยืนยันการลบ", confirm_handle),
                        button("ยกเลิก", cancel_handle),
                    ]
                }
            },
        ]
        return _message_card(widgets)

    def build_noop_message(self, job: ClearJob) -> dict[str, Any]:
        return _message_card(
            [
                self._paragraph("ไม่พบข้อความที่สามารถลบได้ก่อนเวลาที่กำหนด"),
                self._paragraph(f"ข้ามข้อความที่ไม่เข้าเงื่อนไข: {job.candidate_skipped}"),
            ]
        )

    def build_status_message(self, status: JobStatus | None) -> dict[str, Any]:
        notices = {
            JobStatus.DELETE_QUEUED: "รับคำยืนยันแล้ว และกำลังรอดำเนินการ",
            JobStatus.RUNNING: "กำลังดำเนินการล้างประวัติแชท",
            JobStatus.CANCELLED: "ยกเลิกการล้างประวัติแชทแล้ว",
            JobStatus.EXPIRED: "คำยืนยันนี้หมดอายุแล้ว กรุณาส่งคำสั่งใหม่",
            JobStatus.COMPLETED: "งานนี้เสร็จสิ้นแล้ว",
            JobStatus.PARTIAL_FAILED: "งานนี้จบโดยมีบางรายการไม่สำเร็จ",
            JobStatus.FAILED: "งานนี้ไม่สามารถดำเนินการได้",
        }
        return _message_card(
            [self._paragraph(notices.get(status, "ไม่สามารถใช้คำยืนยันนี้ได้"))]
        )

    @staticmethod
    def addon_update(message: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "updateMessageAction": {"message": dict(message)}
                }
            }
        }

    @staticmethod
    def addon_create(message: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "createMessageAction": {"message": dict(message)}
                }
            }
        }


def _snapshot_item(
    message: Mapping[str, Any], *, requester_name: str, space_name: str, cutoff_utc: str
) -> SnapshotItem:
    name = message.get("name")
    if (
        not isinstance(name, str)
        or not name.startswith(f"{space_name}/messages/")
        or len(name.encode("utf-8")) > 2048
    ):
        raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
    created = message.get("createTime")
    created_parsed = _parse_create_time(created)
    cutoff_parsed = _parse_create_time(cutoff_utc)
    if created_parsed.instant_nanoseconds >= cutoff_parsed.instant_nanoseconds:
        raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)

    sender = message.get("sender")
    sender_name = ""
    sender_type = ""
    if sender is not None:
        if not isinstance(sender, Mapping):
            raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
        raw_name = sender.get("name", "")
        raw_type = sender.get("type", "")
        if not isinstance(raw_name, str) or not isinstance(raw_type, str):
            raise ChatHistoryStatsError(ChatHistoryStatsErrorCode.MALFORMED_MESSAGE)
        sender_name = raw_name
        sender_type = raw_type

    if sender_type == "HUMAN" and sender_name == requester_name:
        partition = CredentialPartition.USER
    elif sender_type == "BOT":
        partition = CredentialPartition.BOT
    else:
        partition = CredentialPartition.NONE
    return SnapshotItem(
        message_name=name,
        create_time_utc=str(created),
        sender_name=sender_name,
        sender_type=sender_type,
        credential_partition=partition,
    )


class ChatClearCoordinator:
    """Build immutable previews and deliver recoverable confirmation cards."""

    def __init__(
        self,
        store: ChatClearStore,
        client: ChatHistoryClient,
        gateway: ChatGateway,
        *,
        action_url: str,
        ttl_seconds: int = 600,
        presenter: ChatClearPresenter | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not action_url.startswith("https://") or ttl_seconds < 1:
            raise ValueError("invalid confirmation configuration")
        self._store = store
        self._client = client
        self._gateway = gateway
        self._action_url = action_url
        self._ttl_seconds = ttl_seconds
        self._presenter = presenter or ChatClearPresenter()
        self._clock = clock or (lambda: datetime.now(tz=_UTC))
        self._sleeper = sleeper

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        if isinstance(error, ChatHistoryClientError):
            return error.code.value
        if isinstance(error, ChatHistoryStatsError):
            return error.code.value
        if isinstance(error, ChatClearStoreConflictError):
            return "preview_conflict"
        return "preview_failure"

    def _build_snapshot(self, job: ClearJob) -> ClearJob | None:
        try:
            self._store.reset_incomplete_snapshot(job.operation_id)
            batch: list[SnapshotItem] = []
            for raw in self._client.iter_clear_candidates(
                actor_name=job.requester_name,
                space_name=job.space_name,
                cutoff_utc=job.cutoff_utc,
            ):
                batch.append(
                    _snapshot_item(
                        raw,
                        requester_name=job.requester_name,
                        space_name=job.space_name,
                        cutoff_utc=job.cutoff_utc,
                    )
                )
                if len(batch) >= _SNAPSHOT_BATCH_SIZE:
                    self._store.append_snapshot_items(job.operation_id, batch)
                    batch.clear()
            if batch:
                self._store.append_snapshot_items(job.operation_id, batch)
            return self._store.finalize_snapshot(job.operation_id)
        except Exception as error:  # noqa: BLE001
            category = self._safe_error(error)
            if (
                isinstance(error, ChatHistoryClientError)
                and error.code is ChatHistoryClientErrorCode.RETRY_EXHAUSTED
            ):
                self._store.mark_preview_retry(
                    job.operation_id,
                    next_attempt_at=self._clock() + timedelta(seconds=30),
                    safe_error_category=category,
                )
            else:
                self._store.mark_preview_failed(job.operation_id, category)
            return None

    def _bind_resource(self, job: ClearJob, resource: Mapping[str, Any]) -> bool:
        name = resource.get("name")
        if not isinstance(name, str):
            return False
        try:
            self._store.bind_prepared_confirmation(
                job.operation_id,
                requester_name=job.requester_name,
                space_name=job.space_name,
                confirmation_message_name=name,
                confirmation_client_message_id=(
                    job.confirmation_client_message_id or ""
                ),
                delivery_generation=job.confirmation_delivery_generation,
                expires_at=self._clock() + timedelta(seconds=self._ttl_seconds),
            )
            return True
        except ChatClearStoreConflictError:
            return False

    def _reconcile(self, job: ClearJob) -> bool:
        message_id = job.confirmation_client_message_id
        if not message_id:
            return False
        for delay in _RECONCILIATION_DELAYS:
            if delay:
                self._sleeper(delay)
            try:
                resource = self._gateway.get_message_by_client_id(
                    job.space_name, message_id=message_id
                )
            except ChatMessageNotFoundError:
                continue
            except Exception as error:  # noqa: BLE001
                print(
                    "❌ [chat-history] card reconciliation failed: "
                    f"{type(error).__name__}"
                )
                continue
            return self._bind_resource(job, resource)
        return False

    def _deliver_confirmation(self, job: ClearJob) -> None:
        current = self._store.get_job(job.operation_id)
        if current is None or current.status is not JobStatus.PREPARING:
            return
        if current.confirmation_client_message_id:
            if self._reconcile(current):
                return
            self._store.abandon_confirmation_delivery(
                current.operation_id,
                confirmation_client_message_id=current.confirmation_client_message_id,
                delivery_generation=current.confirmation_delivery_generation,
            )
            current = self._store.get_job(current.operation_id)
            if current is None:
                return

        for _attempt in range(_MAX_DELIVERY_GENERATIONS_PER_RUN):
            generation = current.confirmation_delivery_generation + 1
            # token_urlsafe(32) carries 256 bits and contains no operation metadata.
            confirm_handle = secrets.token_urlsafe(32)
            cancel_handle = secrets.token_urlsafe(32)
            confirm_hash = hashlib.sha256(confirm_handle.encode("ascii")).digest()
            cancel_hash = hashlib.sha256(cancel_handle.encode("ascii")).digest()
            client_id = f"client-jinx-hc-{secrets.token_hex(16)}-{generation % 100:02d}"
            try:
                prepared = self._store.prepare_confirmation_delivery(
                    current.operation_id,
                    requester_name=current.requester_name,
                    space_name=current.space_name,
                    confirmation_client_message_id=client_id,
                    delivery_generation=generation,
                    confirm_token_hash=confirm_hash,
                    cancel_token_hash=cancel_hash,
                )
            except ChatClearStoreConflictError:
                return
            body = self._presenter.build_confirmation_message(
                prepared,
                confirm_handle=confirm_handle,
                cancel_handle=cancel_handle,
                action_url=self._action_url,
                ttl_seconds=self._ttl_seconds,
            )
            latest = self._store.get_job(prepared.operation_id)
            if (
                latest is None
                or latest.status is not JobStatus.PREPARING
                or latest.confirmation_client_message_id != client_id
                or latest.confirmation_delivery_generation != generation
            ):
                return
            try:
                resource = self._gateway.create_structured_card(
                    prepared.space_name, "", body, message_id=client_id
                )
                if self._bind_resource(prepared, resource):
                    return
            except Exception:  # noqa: BLE001
                if self._reconcile(prepared):
                    return
            if not self._store.abandon_confirmation_delivery(
                prepared.operation_id,
                confirmation_client_message_id=client_id,
                delivery_generation=generation,
            ):
                return
            current = self._store.get_job(prepared.operation_id)
            if current is None:
                return
        self._store.mark_preview_failed(current.operation_id, "card_delivery_failure")

    def handle_preview(self, job: ClearJob) -> None:
        current = job
        if not current.snapshot_complete:
            rebuilt = self._build_snapshot(current)
            if rebuilt is None:
                return
            current = rebuilt
        if current.status is JobStatus.COMPLETED:
            claimed = self._store.claim_final_notification(
                current.operation_id, now=self._clock()
            )
            if claimed is None:
                return
            resource = self._gateway.send_structured_card(
                current.space_name,
                "",
                self._presenter.build_noop_message(current),
                message_id=current.final_client_message_id,
            )
            name = resource.get("name") if isinstance(resource, Mapping) else None
            if isinstance(name, str):
                self._store.record_final_notification_sent(
                    current.operation_id, message_name=name
                )
            else:
                self._store.record_final_notification_failure(
                    current.operation_id,
                    safe_error_category="noop_delivery_failure",
                    retry_at=self._clock() + timedelta(seconds=30),
                )
            return
        if current.status is JobStatus.PREPARING and current.snapshot_complete:
            self._deliver_confirmation(current)

    def handle_confirmation_cleanup(self, job: ClearJob) -> None:
        if job.confirmation_message_name is None:
            return
        try:
            self._gateway.update_structured_card(
                job.confirmation_message_name,
                self._presenter.build_status_message(job.status),
            )
        except Exception as error:  # noqa: BLE001
            print(
                "❌ [chat-history] confirmation cleanup failed: "
                f"{type(error).__name__}"
            )
            self._store.record_final_notification_failure(
                job.operation_id,
                safe_error_category="confirmation_cleanup_failure",
                retry_at=self._clock() + timedelta(seconds=30),
            )
            return
        self._store.record_confirmation_cleanup_sent(job.operation_id)


__all__ = ["ChatClearCoordinator", "ChatClearPresenter"]
