from __future__ import annotations

import html
import random
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from helpers.chat_clear_store import (
    ChatClearStore,
    ChatWritePacer,
    ChatWritePacerError,
    ClearItem,
    ClearJob,
    CredentialPartition,
    ItemStatus,
    JobStatus,
)
from helpers.chat_gateway import ChatGateway, ChatMessageNotFoundError
from helpers.services import CredentialReadinessError, CredentialService

CHAT_DELETE_TIMEOUT_SECONDS = 15
CHAT_DELETE_MAX_ATTEMPTS = 5
CHAT_DELETE_BACKOFF_CAP_SECONDS = 60.0
CHAT_DELETE_RETRY_AFTER_CAP_SECONDS = 3600.0
CHAT_FINAL_NOTIFICATION_MAX_ATTEMPTS = 5

_UTC = timezone.utc
_RESOURCE_SEGMENT = r"[^\s/\x00-\x1f\x7f]+"
_MESSAGE_NAME_RE = re.compile(
    rf"^(?P<space>spaces/{_RESOURCE_SEGMENT})/messages/{_RESOURCE_SEGMENT}$"
)
_SYSTEMIC_PERMISSION_REASONS = frozenset(
    {
        "access_token_scope_insufficient",
        "api_disabled",
        "autherror",
        "insufficientpermissions",
        "service_disabled",
    }
)


class ChatDeleteError(RuntimeError):
    """Sanitized delete-side failure; never contains a Google response body."""


class ChatDeleteValidationError(ChatDeleteError):
    pass


class ChatDeleteApiError(ChatDeleteError):
    def __init__(
        self,
        status_code: int,
        *,
        retry_after: str | None = None,
        systemic_permission: bool = False,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.systemic_permission = systemic_permission
        super().__init__(f"chat_delete_http_{status_code}")


def _header(response: object, name: str) -> str | None:
    value: object | None = None
    if isinstance(response, Mapping):
        value = response.get(name) or response.get(name.lower())
    else:
        getter = getattr(response, "get", None)
        if callable(getter):
            value = getter(name) or getter(name.lower())
    return value.strip() if isinstance(value, str) and value.strip() else None


def _systemic_permission(error: HttpError) -> bool:
    try:
        details = error.error_details
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(details, list):
        return False
    for detail in details:
        if not isinstance(detail, Mapping):
            continue
        reason = detail.get("reason")
        if (
            isinstance(reason, str)
            and reason.lower() in _SYSTEMIC_PERMISSION_REASONS
        ):
            return True
    return False


def _sanitize_http_error(error: HttpError) -> ChatDeleteApiError:
    status = getattr(error.resp, "status", 0)
    status_code = status if isinstance(status, int) else 0
    return ChatDeleteApiError(
        status_code,
        retry_after=_header(error.resp, "Retry-After"),
        systemic_permission=(status_code == 403 and _systemic_permission(error)),
    )


class ChatDeleteClient:
    """One credential-bound Chat delete client with no partition fallback."""

    partition: CredentialPartition

    def __init__(
        self,
        credential_service: CredentialService,
        *,
        allowed_space: str,
        partition: CredentialPartition,
        api_factory: Callable[[bool], Any] | None = None,
    ) -> None:
        if partition not in {CredentialPartition.USER, CredentialPartition.BOT}:
            raise ValueError("delete client requires USER or BOT partition")
        if re.fullmatch(rf"spaces/{_RESOURCE_SEGMENT}", allowed_space) is None:
            raise ValueError("invalid allowed Chat space")
        self._credential_service = credential_service
        self.allowed_space = allowed_space
        self.partition = partition
        self._api_factory = api_factory
        self._api: Any | None = None

    def _build_api(self, *, force_refresh: bool) -> Any:
        if self._api_factory is not None:
            return self._api_factory(force_refresh)
        if self.partition is CredentialPartition.USER:
            credentials = (
                self._credential_service.refresh_user_creds()
                if force_refresh
                else self._credential_service.get_user_creds()
            )
        else:
            credentials = (
                self._credential_service.refresh_bot_creds()
                if force_refresh
                else self._credential_service.get_bot_creds()
            )
        authed_http = google_auth_httplib2.AuthorizedHttp(
            credentials,
            http=httplib2.Http(timeout=CHAT_DELETE_TIMEOUT_SECONDS),
        )
        return build("chat", "v1", http=authed_http, cache_discovery=False)

    def refresh_credentials(self) -> None:
        self._api = self._build_api(force_refresh=True)

    def delete(self, *, name: str, force: bool = False) -> None:
        match = _MESSAGE_NAME_RE.fullmatch(name) if isinstance(name, str) else None
        if match is None or match.group("space") != self.allowed_space:
            raise ChatDeleteValidationError("message is outside the allowed space")
        if force is not False:
            raise ChatDeleteValidationError("forced Chat deletion is forbidden")
        if self._api is None:
            self._api = self._build_api(force_refresh=False)
        try:
            (
                self._api.spaces()
                .messages()
                .delete(name=name, force=False)
                .execute(num_retries=0)
            )
        except HttpError as error:
            raise _sanitize_http_error(error) from None


class UserChatDeleteClient(ChatDeleteClient):
    def __init__(
        self,
        credential_service: CredentialService,
        *,
        allowed_space: str,
        api_factory: Callable[[bool], Any] | None = None,
    ) -> None:
        super().__init__(
            credential_service,
            allowed_space=allowed_space,
            partition=CredentialPartition.USER,
            api_factory=api_factory,
        )


class BotChatDeleteClient(ChatDeleteClient):
    def __init__(
        self,
        credential_service: CredentialService,
        *,
        allowed_space: str,
        api_factory: Callable[[bool], Any] | None = None,
    ) -> None:
        super().__init__(
            credential_service,
            allowed_space=allowed_space,
            partition=CredentialPartition.BOT,
            api_factory=api_factory,
        )


class _DecisionKind(str, Enum):
    ABSENT = "ABSENT"
    RETRY = "RETRY"
    REFRESH = "REFRESH"
    ITEM_FAILED = "ITEM_FAILED"
    PARTITION_FAILED = "PARTITION_FAILED"


@dataclass(frozen=True, slots=True)
class _DeleteDecision:
    kind: _DecisionKind
    category: str
    delay_seconds: float = 0.0


def _retry_after_seconds(value: str | None, *, now: datetime) -> float:
    if value is None:
        return 0.0
    if value.isascii() and value.isdecimal():
        return min(float(int(value)), CHAT_DELETE_RETRY_AFTER_CAP_SECONDS)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_UTC)
        seconds = (parsed.astimezone(_UTC) - now.astimezone(_UTC)).total_seconds()
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return min(max(0.0, seconds), CHAT_DELETE_RETRY_AFTER_CAP_SECONDS)


class ChatDeletePresenter:
    @staticmethod
    def _paragraph(text: str) -> dict[str, Any]:
        return {"textParagraph": {"text": html.escape(text)}}

    def build_final_message(
        self, job: ClearJob, items: tuple[ClearItem, ...]
    ) -> dict[str, Any]:
        user_deleted = sum(
            item.status in {ItemStatus.DELETED, ItemStatus.ALREADY_ABSENT}
            and item.credential_partition is CredentialPartition.USER
            for item in items
        )
        bot_deleted = sum(
            item.status in {ItemStatus.DELETED, ItemStatus.ALREADY_ABSENT}
            and item.credential_partition is CredentialPartition.BOT
            for item in items
        )
        title = {
            JobStatus.COMPLETED: "✅ ล้างประวัติแชทเสร็จแล้ว",
            JobStatus.PARTIAL_FAILED: "⚠️ ล้างประวัติแชทได้บางส่วน",
            JobStatus.FAILED: "❌ ไม่สามารถล้างประวัติแชทได้",
        }.get(job.status, "ℹ️ สรุปการล้างประวัติแชท")
        widgets = [
            self._paragraph(title),
            self._paragraph(
                f"ลบแล้ว: {job.deleted_count} | ไม่พบอยู่แล้ว: "
                f"{job.already_absent_count} | ข้าม: {job.skipped_count} | "
                f"ไม่สำเร็จ: {job.failed_count}"
            ),
            self._paragraph(
                f"ข้อความผู้ใช้ที่จบแล้ว: {user_deleted} | "
                f"ข้อความบอตที่จบแล้ว: {bot_deleted}"
            ),
        ]
        return {
            "cardsV2": [
                {
                    "cardId": "jinx-chat-clear-final",
                    "card": {
                        "header": {"title": "🧹 สรุปการล้างประวัติแชท"},
                        "sections": [{"widgets": widgets}],
                    },
                }
            ]
        }


class ChatHistoryDeleteExecutor:
    """Execute only frozen snapshot items, one credential partition at a time."""

    def __init__(
        self,
        store: ChatClearStore,
        user_client: UserChatDeleteClient,
        bot_client: BotChatDeleteClient,
        gateway: ChatGateway,
        write_pacer: ChatWritePacer,
        *,
        delete_enabled: Callable[[], bool],
        presenter: ChatDeletePresenter | None = None,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = CHAT_DELETE_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("delete max attempts must be positive")
        self._store = store
        self._clients = {
            CredentialPartition.USER: user_client,
            CredentialPartition.BOT: bot_client,
        }
        self._gateway = gateway
        self._write_pacer = write_pacer
        self._delete_enabled = delete_enabled
        self._presenter = presenter or ChatDeletePresenter()
        self._clock = clock or (lambda: datetime.now(tz=_UTC))
        self._jitter = jitter
        self._max_attempts = max_attempts

    def _enabled(self) -> bool:
        try:
            return self._delete_enabled() is True
        except Exception:  # noqa: BLE001
            return False

    def _transient_delay(self, attempts: int) -> float:
        ceiling = min(
            CHAT_DELETE_BACKOFF_CAP_SECONDS,
            float(2 ** min(6, max(0, attempts - 1))),
        )
        return max(0.0, self._jitter(0.0, ceiling))

    @staticmethod
    def _require_store_write(applied: bool | int) -> None:
        if not applied:
            raise ChatDeleteError("durable item transition lost its CAS")

    def _classify(
        self,
        error: BaseException,
        item: ClearItem,
        *,
        partition_refreshed: bool,
    ) -> _DeleteDecision:
        if isinstance(error, ChatDeleteApiError):
            status = error.status_code
            if status == 404:
                return _DeleteDecision(_DecisionKind.ABSENT, "already_absent")
            if status == 401:
                if partition_refreshed:
                    return _DeleteDecision(
                        _DecisionKind.PARTITION_FAILED,
                        "unauthorized_after_refresh",
                    )
                return _DeleteDecision(_DecisionKind.REFRESH, "auth_refreshed")
            if status == 403 and error.systemic_permission:
                return _DeleteDecision(
                    _DecisionKind.PARTITION_FAILED,
                    "partition_permission_failure",
                )
            if status == 429 or status >= 500:
                retry_after = _retry_after_seconds(
                    error.retry_after, now=self._clock()
                )
                return _DeleteDecision(
                    _DecisionKind.RETRY,
                    "rate_limited" if status == 429 else "server_error",
                    max(retry_after, self._transient_delay(item.attempts)),
                )
            if 400 <= status < 500:
                return _DeleteDecision(
                    _DecisionKind.ITEM_FAILED, f"http_{status}"
                )
            return _DeleteDecision(_DecisionKind.ITEM_FAILED, "api_failure")
        if isinstance(error, CredentialReadinessError):
            return _DeleteDecision(
                _DecisionKind.PARTITION_FAILED, "credential_unavailable"
            )
        if isinstance(
            error,
            (TimeoutError, ConnectionError, socket.timeout, OSError),
        ):
            return _DeleteDecision(
                _DecisionKind.RETRY,
                "transport_failure",
                self._transient_delay(item.attempts),
            )
        if isinstance(error, ChatDeleteValidationError):
            return _DeleteDecision(
                _DecisionKind.PARTITION_FAILED, "snapshot_validation_failure"
            )
        return _DeleteDecision(_DecisionKind.ITEM_FAILED, "delete_failure")

    def _partition_was_refreshed(
        self, operation_id: str, partition: CredentialPartition
    ) -> bool:
        return any(
            candidate.credential_partition is partition
            and candidate.safe_error_category.startswith("auth_refreshed")
            for candidate in self._store.list_items(operation_id)
        )

    @staticmethod
    def _category_after_refresh(item: ClearItem, category: str = "") -> str:
        return (
            "auth_refreshed"
            if item.safe_error_category.startswith("auth_refreshed")
            else category
        )

    def _record_decision(
        self,
        job: ClearJob,
        item: ClearItem,
        decision: _DeleteDecision,
    ) -> None:
        if decision.kind is _DecisionKind.ABSENT:
            self._require_store_write(
                self._store.record_item_result(
                    job.operation_id,
                    item.message_name,
                    status=ItemStatus.ALREADY_ABSENT,
                    safe_error_category=self._category_after_refresh(item),
                )
            )
            return
        if decision.kind is _DecisionKind.PARTITION_FAILED:
            self._require_store_write(
                self._store.fail_partition(
                    job.operation_id,
                    item.credential_partition,
                    decision.category,
                )
            )
            return
        if decision.kind is _DecisionKind.RETRY:
            if item.attempts >= self._max_attempts:
                self._require_store_write(
                    self._store.record_item_result(
                        job.operation_id,
                        item.message_name,
                        status=ItemStatus.FAILED,
                        safe_error_category=self._category_after_refresh(
                            item, "retry_exhausted"
                        ),
                    )
                )
            else:
                self._require_store_write(
                    self._store.record_item_retry(
                        job.operation_id,
                        item.message_name,
                        next_attempt_at=self._clock()
                        + timedelta(seconds=decision.delay_seconds),
                        safe_error_category=self._category_after_refresh(
                            item, decision.category
                        ),
                    )
                )
            return
        self._require_store_write(
            self._store.record_item_result(
                job.operation_id,
                item.message_name,
                status=ItemStatus.FAILED,
                safe_error_category=self._category_after_refresh(
                    item, decision.category
                ),
            )
        )

    def handle_delete(self, job: ClearJob) -> None:
        if job.status is not JobStatus.RUNNING:
            return
        started = time.monotonic()
        while self._enabled():
            item = self._store.claim_next_item(
                job.operation_id, now=self._clock()
            )
            if item is None:
                remaining = any(
                    candidate.status in {ItemStatus.PENDING, ItemStatus.RUNNING}
                    for candidate in self._store.list_items(job.operation_id)
                )
                if remaining:
                    return
                final = self._store.reconcile_job(
                    job.operation_id, finalize=True
                )
                print(
                    "ℹ️ [chat-history] delete finalized "
                    f"operation={final.operation_id} state={final.status.value} "
                    f"deleted={final.deleted_count} absent={final.already_absent_count} "
                    f"skipped={final.skipped_count} failed={final.failed_count} "
                    f"duration_ms={int((time.monotonic() - started) * 1000)}"
                )
                return

            try:
                self._write_pacer.wait_for_turn(job.space_name)
            except ChatWritePacerError:
                self._require_store_write(
                    self._store.record_item_retry(
                        job.operation_id,
                        item.message_name,
                        next_attempt_at=self._clock() + timedelta(seconds=30),
                        safe_error_category="pacer_failure",
                    )
                )
                return
            if not self._enabled():
                self._require_store_write(
                    self._store.record_item_retry(
                        job.operation_id,
                        item.message_name,
                        next_attempt_at=self._clock() + timedelta(seconds=5),
                        safe_error_category="kill_switch",
                    )
                )
                return

            client = self._clients[item.credential_partition]
            try:
                client.delete(name=item.message_name, force=False)
            except Exception as error:  # noqa: BLE001
                decision = self._classify(
                    error,
                    item,
                    partition_refreshed=self._partition_was_refreshed(
                        job.operation_id, item.credential_partition
                    ),
                )
                if decision.kind is _DecisionKind.REFRESH:
                    try:
                        client.refresh_credentials()
                    except Exception:  # noqa: BLE001
                        decision = _DeleteDecision(
                            _DecisionKind.PARTITION_FAILED,
                            "credential_refresh_failure",
                        )
                    else:
                        self._require_store_write(
                            self._store.record_item_retry(
                                job.operation_id,
                                item.message_name,
                                next_attempt_at=self._clock(),
                                safe_error_category=decision.category,
                            )
                        )
                        continue
                self._record_decision(job, item, decision)
                continue

            self._require_store_write(
                self._store.record_item_result(
                    job.operation_id,
                    item.message_name,
                    status=ItemStatus.DELETED,
                    safe_error_category=self._category_after_refresh(item),
                )
            )

    def handle_final_notification(self, job: ClearJob) -> None:
        if job.status not in {
            JobStatus.COMPLETED,
            JobStatus.PARTIAL_FAILED,
            JobStatus.FAILED,
        }:
            return
        body = self._presenter.build_final_message(
            job, self._store.list_items(job.operation_id)
        )
        resource: Mapping[str, Any] | None = None
        try:
            resource = self._gateway.create_structured_card(
                job.space_name,
                "",
                body,
                message_id=job.final_client_message_id,
            )
        except Exception:  # noqa: BLE001
            try:
                resource = self._gateway.get_message_by_client_id(
                    job.space_name, message_id=job.final_client_message_id
                )
            except ChatMessageNotFoundError:
                resource = None
            except Exception:  # noqa: BLE001
                resource = None

        name = resource.get("name") if isinstance(resource, Mapping) else None
        if isinstance(name, str):
            self._require_store_write(
                self._store.record_final_notification_sent(
                    job.operation_id, message_name=name
                )
            )
            return
        if job.final_notification_attempts >= CHAT_FINAL_NOTIFICATION_MAX_ATTEMPTS:
            self._require_store_write(
                self._store.record_final_notification_failure(
                    job.operation_id,
                    safe_error_category="final_delivery_exhausted",
                    permanent=True,
                )
            )
            return
        delay = self._transient_delay(job.final_notification_attempts)
        self._require_store_write(
            self._store.record_final_notification_failure(
                job.operation_id,
                safe_error_category="final_delivery_failure",
                retry_at=self._clock() + timedelta(seconds=delay),
            )
        )


__all__ = [
    "CHAT_DELETE_BACKOFF_CAP_SECONDS",
    "CHAT_DELETE_MAX_ATTEMPTS",
    "CHAT_DELETE_RETRY_AFTER_CAP_SECONDS",
    "CHAT_DELETE_TIMEOUT_SECONDS",
    "BotChatDeleteClient",
    "ChatDeleteApiError",
    "ChatDeleteClient",
    "ChatDeleteError",
    "ChatDeletePresenter",
    "ChatDeleteValidationError",
    "ChatHistoryDeleteExecutor",
    "UserChatDeleteClient",
]
