from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHAT_WRITE_MIN_INTERVAL_SECONDS = 1.1
CHAT_HISTORY_DATABASE_NAME = "history.sqlite3"
CHAT_HISTORY_SCHEMA_VERSION = 1
CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS = 5_000
CHAT_HISTORY_TERMINAL_RETENTION_DAYS = 30
CHAT_HISTORY_MAINTENANCE_BATCH = 100
CHAT_HISTORY_MAX_SNAPSHOT_ITEMS = 10_000
CHAT_HISTORY_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
CHAT_HISTORY_RUNNING_MAX_AGE_SECONDS = 24 * 60 * 60

_UTC = timezone.utc
_RESOURCE_SEGMENT = r"[^\s/\x00-\x1f\x7f]+"
_USER_NAME_RE = re.compile(rf"^users/{_RESOURCE_SEGMENT}$")
_SPACE_NAME_RE = re.compile(rf"^spaces/{_RESOURCE_SEGMENT}$")
_MESSAGE_NAME_RE = re.compile(
    rf"^(?P<space>spaces/{_RESOURCE_SEGMENT})/messages/{_RESOURCE_SEGMENT}$"
)
_CLIENT_MESSAGE_ID_RE = re.compile(r"^client-[a-z0-9](?:[a-z0-9-]{0,54}[a-z0-9])?$")
_OPERATION_ID_RE = re.compile(r"^op-[0-9a-f]{32}$")
_SAFE_CATEGORY_RE = re.compile(r"^[a-z0-9_:-]{0,128}$")
_TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z$"
)


class ChatClearStoreError(RuntimeError):
    """Sanitized durable-state failure."""


class ChatClearStoreUnavailableError(ChatClearStoreError):
    pass


class ChatClearStoreSchemaError(ChatClearStoreError):
    pass


class ChatClearStoreValidationError(ChatClearStoreError):
    pass


class ChatClearStoreConflictError(ChatClearStoreError):
    pass


class ActiveDeleteJobError(ChatClearStoreConflictError):
    pass


class SnapshotFrozenError(ChatClearStoreConflictError):
    pass


class ChatWritePacerError(ChatClearStoreError):
    """Safe failure raised when a durable write reservation is unavailable."""


class JobStatus(str, Enum):
    PREVIEW_QUEUED = "PREVIEW_QUEUED"
    PREPARING = "PREPARING"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    DELETE_QUEUED = "DELETE_QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ItemStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DELETED = "DELETED"
    ALREADY_ABSENT = "ALREADY_ABSENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class CredentialPartition(str, Enum):
    USER = "USER"
    BOT = "BOT"
    NONE = "NONE"


class ActionKind(str, Enum):
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


ACTIVE_JOB_STATUSES = (
    JobStatus.PREVIEW_QUEUED,
    JobStatus.PREPARING,
    JobStatus.PENDING_CONFIRMATION,
    JobStatus.DELETE_QUEUED,
    JobStatus.RUNNING,
)
PRE_DELETE_JOB_STATUSES = (
    JobStatus.PREVIEW_QUEUED,
    JobStatus.PREPARING,
    JobStatus.PENDING_CONFIRMATION,
)
TERMINAL_JOB_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.PARTIAL_FAILED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.EXPIRED,
)


@dataclass(frozen=True, slots=True)
class ClearJobRequest:
    source_message_name: str
    requester_name: str
    space_name: str
    source_event_time_utc: str
    reference_time_utc: str
    cutoff_utc: str
    display_timezone: str
    normalized_argument: str


@dataclass(frozen=True, slots=True)
class ClearJob:
    operation_id: str
    source_message_name: str
    requester_name: str
    space_name: str
    source_event_time_utc: str
    reference_time_utc: str
    cutoff_utc: str
    display_timezone: str
    normalized_argument: str
    status: JobStatus
    snapshot_complete: bool
    confirmation_message_name: str | None
    confirmation_client_message_id: str | None
    confirmation_delivery_generation: int
    final_message_name: str | None
    final_client_message_id: str
    final_delivery_generation: int
    created_at: str
    updated_at: str
    expires_at: str | None
    next_attempt_at: str | None
    candidate_human: int
    candidate_bot: int
    candidate_skipped: int
    deleted_count: int
    already_absent_count: int
    skipped_count: int
    failed_count: int
    user_partition_error: str
    bot_partition_error: str
    final_notification_state: NotificationStatus
    final_notification_attempts: int
    final_notification_next_attempt_at: str | None
    safe_error_category: str


@dataclass(frozen=True, slots=True)
class CreateJobResult:
    job: ClearJob
    created: bool
    superseded_operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    message_name: str
    create_time_utc: str
    sender_name: str
    sender_type: str
    credential_partition: CredentialPartition


@dataclass(frozen=True, slots=True)
class ClearItem:
    operation_id: str
    message_name: str
    create_time_utc: str
    sender_name: str
    sender_type: str
    credential_partition: CredentialPartition
    status: ItemStatus
    attempts: int
    claimed_at: str | None
    next_attempt_at: str | None
    safe_error_category: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    operation_id: str | None
    status: JobStatus | None
    authorized: bool
    transitioned: bool
    action: ActionKind | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    rebuilt_snapshots: int
    expired_jobs: int
    recovered_items: int
    recovered_notifications: int
    stale_jobs: int


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    pruned_jobs: int
    checkpoint_busy: int
    checkpoint_log_frames: int
    checkpointed_frames: int


@dataclass(frozen=True, slots=True)
class StoreDiagnostics:
    job_counts: Mapping[str, int]
    item_counts: Mapping[str, int]
    oldest_active_age_seconds: float | None
    worker_owner: str | None
    worker_heartbeat_at: str | None
    partition_failure_counts: Mapping[str, int]
    pending_final_notifications: int
    wal_bytes: int


@dataclass(frozen=True, slots=True)
class StorePreflight:
    database_path: Path
    schema_version: int
    journal_mode: str
    synchronous: int
    foreign_keys: bool
    busy_timeout_milliseconds: int
    display_timezone: str


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ChatClearStoreValidationError("timestamp must be timezone-aware")
    utc_value = value.astimezone(_UTC)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _format_epoch(value: float) -> str:
    try:
        return _format_datetime(datetime.fromtimestamp(value, tz=_UTC))
    except (OverflowError, OSError, ValueError):
        raise ChatClearStoreValidationError("invalid wall-clock timestamp") from None


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ChatClearStoreValidationError("invalid UTC timestamp")
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ChatClearStoreValidationError("invalid UTC timestamp")
    fraction = match.group("fraction") or ""
    normalized = (
        f"{match.group('date')}T{match.group('time')}.{(fraction + '000000')[:6]}+00:00"
    )
    try:
        return datetime.fromisoformat(normalized).astimezone(_UTC)
    except (OverflowError, ValueError):
        raise ChatClearStoreValidationError("invalid UTC timestamp") from None


def _canonical_job_timestamp(value: object) -> str:
    return _format_datetime(_parse_timestamp(value))


def _canonical_item_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ChatClearStoreValidationError("invalid item timestamp")
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ChatClearStoreValidationError("invalid item timestamp")
    _parse_timestamp(value)
    fraction = match.group("fraction") or ""
    return (
        f"{match.group('date')}T{match.group('time')}.{(fraction + '000000000')[:9]}Z"
    )


def _timestamp_to_epoch(value: str) -> float:
    return _parse_timestamp(value).timestamp()


def _validate_safe_category(value: str) -> str:
    if not isinstance(value, str) or _SAFE_CATEGORY_RE.fullmatch(value) is None:
        raise ChatClearStoreValidationError("invalid safe error category")
    return value


def _validate_operation_id(value: str) -> str:
    if not isinstance(value, str) or _OPERATION_ID_RE.fullmatch(value) is None:
        raise ChatClearStoreValidationError("invalid operation ID")
    return value


def _validate_space(value: str) -> str:
    if not isinstance(value, str) or _SPACE_NAME_RE.fullmatch(value) is None:
        raise ChatClearStoreValidationError("invalid space resource")
    return value


def _validate_user(value: str) -> str:
    if not isinstance(value, str) or _USER_NAME_RE.fullmatch(value) is None:
        raise ChatClearStoreValidationError("invalid user resource")
    return value


def _validate_message(value: str, space_name: str) -> str:
    match = _MESSAGE_NAME_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None or match.group("space") != space_name:
        raise ChatClearStoreValidationError("invalid message resource")
    return value


def _validate_client_message_id(value: str) -> str:
    if not isinstance(value, str) or _CLIENT_MESSAGE_ID_RE.fullmatch(value) is None:
        raise ChatClearStoreValidationError("invalid client message ID")
    return value


def _secure_regular_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise ChatClearStoreUnavailableError(
            "private state file is unavailable"
        ) from None
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise ChatClearStoreUnavailableError("private state file is invalid")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _prepare_state_directory(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ChatClearStoreUnavailableError("history state directory must be absolute")

    current = Path(expanded.anchor)
    try:
        for part in expanded.parts[1:]:
            current = current / part
            if current.exists() or current.is_symlink():
                mode = current.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ChatClearStoreUnavailableError(
                        "history state directory cannot contain symlinks"
                    )
        expanded.mkdir(parents=True, mode=0o700, exist_ok=True)
        mode = expanded.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise ChatClearStoreUnavailableError("history state directory is invalid")
        expanded.chmod(0o700)
    except ChatClearStoreUnavailableError:
        raise
    except OSError:
        raise ChatClearStoreUnavailableError(
            "history state directory is unavailable"
        ) from None
    return expanded


def _filesystem_type(path: Path) -> str | None:
    """Return the deepest Linux mount type without invoking external commands."""

    try:
        resolved = path.resolve(strict=True)
        best_length = -1
        best_type: str | None = None
        for line in (
            Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        ):
            fields = line.split()
            separator = fields.index("-")
            mount_point = fields[4].replace("\\040", " ").replace("\\134", "\\")
            filesystem_type = fields[separator + 1]
            mount_path = Path(mount_point)
            if resolved == mount_path or mount_path in resolved.parents:
                length = len(str(mount_path))
                if length > best_length:
                    best_length = length
                    best_type = filesystem_type
        return best_type
    except (OSError, UnicodeError, ValueError):
        return None


class _MigrationLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> _MigrationLock:
        _secure_regular_file(self._path)
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.fchmod(descriptor, 0o600)
        except OSError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise ChatClearStoreUnavailableError(
                "schema migration lock failed"
            ) from None
        self._descriptor = descriptor
        return self

    def __exit__(self, *_exc_info: object) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


_SCHEMA_SQL = """
CREATE TABLE clear_jobs (
    operation_id TEXT PRIMARY KEY,
    source_message_name TEXT NOT NULL UNIQUE,
    requester_name TEXT NOT NULL,
    space_name TEXT NOT NULL,
    source_event_time_utc TEXT NOT NULL,
    reference_time_utc TEXT NOT NULL,
    cutoff_utc TEXT NOT NULL,
    display_timezone TEXT NOT NULL,
    normalized_argument TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PREVIEW_QUEUED','PREPARING','PENDING_CONFIRMATION','DELETE_QUEUED',
        'RUNNING','COMPLETED','PARTIAL_FAILED','FAILED','CANCELLED','EXPIRED'
    )),
    snapshot_complete INTEGER NOT NULL DEFAULT 0 CHECK(snapshot_complete IN (0,1)),
    snapshot_item_count INTEGER NOT NULL DEFAULT 0 CHECK(snapshot_item_count >= 0),
    snapshot_bytes INTEGER NOT NULL DEFAULT 0 CHECK(snapshot_bytes >= 0),
    confirmation_message_name TEXT,
    confirmation_client_message_id TEXT UNIQUE,
    confirmation_delivery_generation INTEGER NOT NULL DEFAULT 0
        CHECK(confirmation_delivery_generation >= 0),
    final_message_name TEXT,
    final_client_message_id TEXT NOT NULL UNIQUE,
    final_delivery_generation INTEGER NOT NULL DEFAULT 0
        CHECK(final_delivery_generation >= 0),
    confirm_token_hash BLOB,
    cancel_token_hash BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    next_attempt_at TEXT,
    candidate_human INTEGER NOT NULL DEFAULT 0 CHECK(candidate_human >= 0),
    candidate_bot INTEGER NOT NULL DEFAULT 0 CHECK(candidate_bot >= 0),
    candidate_skipped INTEGER NOT NULL DEFAULT 0 CHECK(candidate_skipped >= 0),
    deleted_count INTEGER NOT NULL DEFAULT 0 CHECK(deleted_count >= 0),
    already_absent_count INTEGER NOT NULL DEFAULT 0 CHECK(already_absent_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK(skipped_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_count >= 0),
    user_partition_error TEXT NOT NULL DEFAULT '',
    bot_partition_error TEXT NOT NULL DEFAULT '',
    final_notification_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(final_notification_state IN ('PENDING','SENDING','SENT','FAILED')),
    final_notification_attempts INTEGER NOT NULL DEFAULT 0
        CHECK(final_notification_attempts >= 0),
    final_notification_next_attempt_at TEXT,
    safe_error_category TEXT NOT NULL DEFAULT ''
);

CREATE TABLE clear_items (
    operation_id TEXT NOT NULL REFERENCES clear_jobs(operation_id) ON DELETE CASCADE,
    message_name TEXT NOT NULL,
    create_time_utc TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    sender_type TEXT NOT NULL,
    credential_partition TEXT NOT NULL CHECK(credential_partition IN ('USER','BOT','NONE')),
    status TEXT NOT NULL CHECK(status IN (
        'PENDING','RUNNING','DELETED','ALREADY_ABSENT','SKIPPED','FAILED'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    claimed_at TEXT,
    next_attempt_at TEXT,
    safe_error_category TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(operation_id, message_name)
);

CREATE TABLE space_write_pacer (
    space_name TEXT PRIMARY KEY,
    next_write_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE worker_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    owner_id TEXT,
    heartbeat_at TEXT
);

INSERT INTO worker_state(singleton, owner_id, heartbeat_at) VALUES(1, NULL, NULL);

CREATE INDEX clear_jobs_due
ON clear_jobs(status, next_attempt_at, created_at);

CREATE INDEX clear_items_due
ON clear_items(operation_id, status, next_attempt_at, create_time_utc, message_name);

CREATE UNIQUE INDEX one_active_clear_job_per_space
ON clear_jobs(space_name)
WHERE status IN (
    'PREVIEW_QUEUED','PREPARING','PENDING_CONFIRMATION','DELETE_QUEUED','RUNNING'
);

CREATE TRIGGER clear_jobs_identity_immutable
BEFORE UPDATE OF operation_id, source_message_name, requester_name, space_name,
    source_event_time_utc, reference_time_utc, cutoff_utc, display_timezone,
    normalized_argument, final_client_message_id
ON clear_jobs
WHEN OLD.operation_id IS NOT NEW.operation_id
  OR OLD.source_message_name IS NOT NEW.source_message_name
  OR OLD.requester_name IS NOT NEW.requester_name
  OR OLD.space_name IS NOT NEW.space_name
  OR OLD.source_event_time_utc IS NOT NEW.source_event_time_utc
  OR OLD.reference_time_utc IS NOT NEW.reference_time_utc
  OR OLD.cutoff_utc IS NOT NEW.cutoff_utc
  OR OLD.display_timezone IS NOT NEW.display_timezone
  OR OLD.normalized_argument IS NOT NEW.normalized_argument
  OR OLD.final_client_message_id IS NOT NEW.final_client_message_id
BEGIN
    SELECT RAISE(ABORT, 'immutable clear job identity');
END;

CREATE TRIGGER clear_items_frozen_update
BEFORE UPDATE OF operation_id, message_name, create_time_utc, sender_name,
    sender_type, credential_partition
ON clear_items
WHEN (SELECT snapshot_complete FROM clear_jobs
      WHERE operation_id = OLD.operation_id) = 1
 AND (
    OLD.operation_id IS NOT NEW.operation_id
    OR OLD.message_name IS NOT NEW.message_name
    OR OLD.create_time_utc IS NOT NEW.create_time_utc
    OR OLD.sender_name IS NOT NEW.sender_name
    OR OLD.sender_type IS NOT NEW.sender_type
    OR OLD.credential_partition IS NOT NEW.credential_partition
 )
BEGIN
    SELECT RAISE(ABORT, 'immutable clear item identity');
END;

CREATE TRIGGER clear_items_frozen_insert
BEFORE INSERT ON clear_items
WHEN (SELECT snapshot_complete FROM clear_jobs
      WHERE operation_id = NEW.operation_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'snapshot is frozen');
END;
"""


class _HistoryDatabase:
    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / CHAT_HISTORY_DATABASE_NAME
        self.migration_lock_path = self.state_dir / "migration.lock"
        self.worker_lock_path = self.state_dir / "worker.lock"
        self._thread_lock = threading.RLock()
        self._initialized = False

    def _repair_modes(self) -> None:
        try:
            self.state_dir.chmod(0o700)
            for path in (
                self.database_path,
                Path(f"{self.database_path}-wal"),
                Path(f"{self.database_path}-shm"),
                self.migration_lock_path,
                self.worker_lock_path,
            ):
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ChatClearStoreUnavailableError(
                            "history state file is invalid"
                        )
                    path.chmod(0o600)
        except ChatClearStoreUnavailableError:
            raise
        except OSError:
            raise ChatClearStoreUnavailableError(
                "history state permissions are unavailable"
            ) from None

    def ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._thread_lock:
            if self._initialized:
                return
            self.state_dir = _prepare_state_directory(self.state_dir)
            self.database_path = self.state_dir / CHAT_HISTORY_DATABASE_NAME
            self.migration_lock_path = self.state_dir / "migration.lock"
            self.worker_lock_path = self.state_dir / "worker.lock"
            _secure_regular_file(self.database_path)
            with _MigrationLock(self.migration_lock_path):
                try:
                    connection = sqlite3.connect(
                        self.database_path,
                        timeout=CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS / 1000,
                        isolation_level=None,
                    )
                    try:
                        connection.execute(
                            f"PRAGMA busy_timeout={CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS}"
                        )
                        version = int(
                            connection.execute("PRAGMA user_version").fetchone()[0]
                        )
                        if version > CHAT_HISTORY_SCHEMA_VERSION:
                            raise ChatClearStoreSchemaError(
                                "history database was created by a newer version"
                            )
                        if version == 0:
                            self._migrate_zero_to_one(connection)
                        elif version != CHAT_HISTORY_SCHEMA_VERSION:
                            raise ChatClearStoreSchemaError(
                                "unsupported history database schema"
                            )
                        self._verify_schema(connection)
                        connection.execute("PRAGMA journal_mode=WAL")
                        connection.execute("PRAGMA synchronous=FULL")
                    finally:
                        connection.close()
                except ChatClearStoreError:
                    raise
                except sqlite3.DatabaseError:
                    raise ChatClearStoreSchemaError(
                        "history database is corrupt or incompatible"
                    ) from None
                except (OSError, ValueError):
                    raise ChatClearStoreUnavailableError(
                        "history database initialization failed"
                    ) from None
            self._repair_modes()
            self._initialized = True

    def _migrate_zero_to_one(self, connection: sqlite3.Connection) -> None:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not table_names.issubset({"chat_write_pacer"}):
            raise ChatClearStoreSchemaError(
                "unversioned history database is incompatible"
            )

        legacy_rows: list[tuple[str, float, float]] = []
        if "chat_write_pacer" in table_names:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(chat_write_pacer)")
            }
            if columns != {"space_name", "next_write_at", "updated_at"}:
                raise ChatClearStoreSchemaError("legacy pacer schema is incompatible")
            for row in connection.execute(
                "SELECT space_name, next_write_at, updated_at FROM chat_write_pacer"
            ):
                legacy_rows.append((str(row[0]), float(row[1]), float(row[2])))

        drop_legacy = (
            "DROP TABLE chat_write_pacer;" if "chat_write_pacer" in table_names else ""
        )
        try:
            connection.executescript(f"BEGIN EXCLUSIVE;\n{drop_legacy}\n{_SCHEMA_SQL}")
            for space_name, next_write_at, updated_at in legacy_rows:
                _validate_space(space_name)
                connection.execute(
                    """
                    INSERT INTO space_write_pacer(
                        space_name, next_write_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        space_name,
                        _format_epoch(next_write_at),
                        _format_epoch(updated_at),
                    ),
                )
            connection.execute(f"PRAGMA user_version={CHAT_HISTORY_SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        required = {
            "clear_jobs",
            "clear_items",
            "space_write_pacer",
            "worker_state",
        }
        actual = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not required.issubset(actual):
            raise ChatClearStoreSchemaError("history database schema is incomplete")
        required_columns = {
            "clear_jobs": {
                "operation_id",
                "source_message_name",
                "requester_name",
                "space_name",
                "status",
                "snapshot_complete",
                "confirmation_message_name",
                "confirm_token_hash",
                "cancel_token_hash",
                "final_client_message_id",
                "final_notification_state",
                "created_at",
                "updated_at",
            },
            "clear_items": {
                "operation_id",
                "message_name",
                "create_time_utc",
                "credential_partition",
                "status",
                "attempts",
                "updated_at",
            },
            "space_write_pacer": {
                "space_name",
                "next_write_at_utc",
                "updated_at_utc",
            },
            "worker_state": {"singleton", "owner_id", "heartbeat_at"},
        }
        for table, expected_columns in required_columns.items():
            actual_columns = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not expected_columns.issubset(actual_columns):
                raise ChatClearStoreSchemaError("history database schema is incomplete")
        required_indexes = {
            "clear_jobs_due",
            "clear_items_due",
            "one_active_clear_job_per_space",
        }
        actual_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        required_triggers = {
            "clear_jobs_identity_immutable",
            "clear_items_frozen_update",
            "clear_items_frozen_insert",
        }
        actual_triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        if not required_indexes.issubset(
            actual_indexes
        ) or not required_triggers.issubset(actual_triggers):
            raise ChatClearStoreSchemaError(
                "history database invariants are incomplete"
            )
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]) != "ok":
            raise ChatClearStoreSchemaError("history database integrity check failed")

    def connect(self) -> sqlite3.Connection:
        self.ensure_schema()
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout={CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS}"
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except sqlite3.DatabaseError:
            raise ChatClearStoreUnavailableError(
                "history database connection failed"
            ) from None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except sqlite3.DatabaseError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise ChatClearStoreUnavailableError(
                "history database transaction failed"
            ) from None
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
            self._repair_modes()


def _job_from_row(row: sqlite3.Row) -> ClearJob:
    return ClearJob(
        operation_id=str(row["operation_id"]),
        source_message_name=str(row["source_message_name"]),
        requester_name=str(row["requester_name"]),
        space_name=str(row["space_name"]),
        source_event_time_utc=str(row["source_event_time_utc"]),
        reference_time_utc=str(row["reference_time_utc"]),
        cutoff_utc=str(row["cutoff_utc"]),
        display_timezone=str(row["display_timezone"]),
        normalized_argument=str(row["normalized_argument"]),
        status=JobStatus(str(row["status"])),
        snapshot_complete=bool(row["snapshot_complete"]),
        confirmation_message_name=(
            str(row["confirmation_message_name"])
            if row["confirmation_message_name"] is not None
            else None
        ),
        confirmation_client_message_id=(
            str(row["confirmation_client_message_id"])
            if row["confirmation_client_message_id"] is not None
            else None
        ),
        confirmation_delivery_generation=int(row["confirmation_delivery_generation"]),
        final_message_name=(
            str(row["final_message_name"])
            if row["final_message_name"] is not None
            else None
        ),
        final_client_message_id=str(row["final_client_message_id"]),
        final_delivery_generation=int(row["final_delivery_generation"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        expires_at=str(row["expires_at"]) if row["expires_at"] is not None else None,
        next_attempt_at=(
            str(row["next_attempt_at"]) if row["next_attempt_at"] is not None else None
        ),
        candidate_human=int(row["candidate_human"]),
        candidate_bot=int(row["candidate_bot"]),
        candidate_skipped=int(row["candidate_skipped"]),
        deleted_count=int(row["deleted_count"]),
        already_absent_count=int(row["already_absent_count"]),
        skipped_count=int(row["skipped_count"]),
        failed_count=int(row["failed_count"]),
        user_partition_error=str(row["user_partition_error"]),
        bot_partition_error=str(row["bot_partition_error"]),
        final_notification_state=NotificationStatus(
            str(row["final_notification_state"])
        ),
        final_notification_attempts=int(row["final_notification_attempts"]),
        final_notification_next_attempt_at=(
            str(row["final_notification_next_attempt_at"])
            if row["final_notification_next_attempt_at"] is not None
            else None
        ),
        safe_error_category=str(row["safe_error_category"]),
    )


def _item_from_row(row: sqlite3.Row) -> ClearItem:
    return ClearItem(
        operation_id=str(row["operation_id"]),
        message_name=str(row["message_name"]),
        create_time_utc=str(row["create_time_utc"]),
        sender_name=str(row["sender_name"]),
        sender_type=str(row["sender_type"]),
        credential_partition=CredentialPartition(str(row["credential_partition"])),
        status=ItemStatus(str(row["status"])),
        attempts=int(row["attempts"]),
        claimed_at=str(row["claimed_at"]) if row["claimed_at"] is not None else None,
        next_attempt_at=(
            str(row["next_attempt_at"]) if row["next_attempt_at"] is not None else None
        ),
        safe_error_category=str(row["safe_error_category"]),
    )


class ChatClearStore:
    """SQLite state machine. Every method commits before returning."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        max_snapshot_items: int = CHAT_HISTORY_MAX_SNAPSHOT_ITEMS,
        max_snapshot_bytes: int = CHAT_HISTORY_MAX_SNAPSHOT_BYTES,
    ) -> None:
        if max_snapshot_items < 1 or max_snapshot_bytes < 1:
            raise ValueError("snapshot limits must be positive")
        self._database = _HistoryDatabase(state_dir)
        self.state_dir = self._database.state_dir
        self.database_path = self._database.database_path
        self.worker_lock_path = self._database.worker_lock_path
        self._clock = clock or (lambda: datetime.now(tz=_UTC))
        self.max_snapshot_items = max_snapshot_items
        self.max_snapshot_bytes = max_snapshot_bytes

    def _now(self, supplied: datetime | None = None) -> str:
        return _format_datetime(supplied if supplied is not None else self._clock())

    def preflight(self, *, delete_execution: bool = False) -> StorePreflight:
        try:
            ZoneInfo("Asia/Bangkok")
        except ZoneInfoNotFoundError:
            raise ChatClearStoreUnavailableError(
                "Asia/Bangkok timezone data is unavailable"
            ) from None
        self._database.ensure_schema()
        resolved = self._database.state_dir.resolve(strict=True)
        if delete_execution and (
            resolved == Path("/tmp") or Path("/tmp") in resolved.parents
        ):
            raise ChatClearStoreUnavailableError(
                "delete execution requires persistent state outside /tmp"
            )
        if delete_execution:
            filesystem_type = _filesystem_type(resolved)
            if filesystem_type is None or filesystem_type.lower() in {
                "9p",
                "cifs",
                "fuse",
                "fuseblk",
                "nfs",
                "nfs4",
                "overlay",
                "ramfs",
                "smb3",
                "tmpfs",
            }:
                raise ChatClearStoreUnavailableError(
                    "delete execution requires a supported persistent local filesystem"
                )
        connection = self._database.connect()
        try:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        self._database._repair_modes()
        if (
            journal_mode.lower() != "wal"
            or synchronous != 2
            or not foreign_keys
            or busy_timeout != CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS
            or version != CHAT_HISTORY_SCHEMA_VERSION
        ):
            raise ChatClearStoreSchemaError("history database pragmas are incompatible")
        return StorePreflight(
            database_path=self._database.database_path,
            schema_version=version,
            journal_mode=journal_mode.lower(),
            synchronous=synchronous,
            foreign_keys=foreign_keys,
            busy_timeout_milliseconds=busy_timeout,
            display_timezone="Asia/Bangkok",
        )

    def get_job(self, operation_id: str) -> ClearJob | None:
        _validate_operation_id(operation_id)
        connection = self._database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return _job_from_row(row) if row is not None else None
        finally:
            connection.close()

    def create_job(self, request: ClearJobRequest) -> CreateJobResult:
        space_name = _validate_space(request.space_name)
        requester_name = _validate_user(request.requester_name)
        source_message_name = _validate_message(request.source_message_name, space_name)
        source_event = _canonical_job_timestamp(request.source_event_time_utc)
        reference = _canonical_job_timestamp(request.reference_time_utc)
        cutoff = _canonical_job_timestamp(request.cutoff_utc)
        if _timestamp_to_epoch(cutoff) > _timestamp_to_epoch(reference):
            raise ChatClearStoreValidationError("cutoff cannot be after reference")
        if request.display_timezone != "Asia/Bangkok":
            raise ChatClearStoreValidationError("unsupported display timezone")
        if (
            not isinstance(request.normalized_argument, str)
            or not request.normalized_argument
            or len(request.normalized_argument.encode("utf-8")) > 64
            or any(ord(character) < 0x20 for character in request.normalized_argument)
        ):
            raise ChatClearStoreValidationError("invalid normalized argument")

        operation_id = f"op-{uuid.uuid4().hex}"
        final_digest = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:32]
        final_client_id = f"client-jinx-hf-{final_digest}"
        now = self._now()
        superseded: list[str] = []
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM clear_jobs WHERE source_message_name = ?",
                (source_message_name,),
            ).fetchone()
            if existing is not None:
                return CreateJobResult(_job_from_row(existing), created=False)

            blocking = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE space_name = ? AND status IN ('DELETE_QUEUED','RUNNING')
                LIMIT 1
                """,
                (space_name,),
            ).fetchone()
            if blocking is not None:
                raise ActiveDeleteJobError("an active delete job already exists")

            rows = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE space_name = ?
                  AND status IN ('PREVIEW_QUEUED','PREPARING','PENDING_CONFIRMATION')
                """,
                (space_name,),
            ).fetchall()
            superseded = [str(row[0]) for row in rows]
            if superseded:
                placeholders = ",".join("?" for _ in superseded)
                connection.execute(
                    f"""
                    UPDATE clear_jobs
                    SET status='CANCELLED', safe_error_category='superseded',
                        updated_at=?, expires_at=NULL, next_attempt_at=NULL
                    WHERE operation_id IN ({placeholders})
                    """,
                    (now, *superseded),
                )

            connection.execute(
                """
                INSERT INTO clear_jobs(
                    operation_id, source_message_name, requester_name, space_name,
                    source_event_time_utc, reference_time_utc, cutoff_utc,
                    display_timezone, normalized_argument, status,
                    final_client_message_id, created_at, updated_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREVIEW_QUEUED', ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    source_message_name,
                    requester_name,
                    space_name,
                    source_event,
                    reference,
                    cutoff,
                    request.display_timezone,
                    request.normalized_argument,
                    final_client_id,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise ChatClearStoreUnavailableError("job insert did not persist")
            return CreateJobResult(
                _job_from_row(row),
                created=True,
                superseded_operation_ids=tuple(superseded),
            )

    def claim_preview(self, *, now: datetime | None = None) -> ClearJob | None:
        now_text = self._now(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM clear_jobs
                WHERE (status='PREVIEW_QUEUED'
                       AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                   OR (status='PREPARING' AND snapshot_complete=1)
                ORDER BY created_at, operation_id
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            operation_id = str(row["operation_id"])
            if str(row["status"]) == JobStatus.PREVIEW_QUEUED.value:
                connection.execute(
                    """
                    UPDATE clear_jobs
                    SET status='PREPARING', updated_at=?, next_attempt_at=NULL
                    WHERE operation_id=? AND status='PREVIEW_QUEUED'
                    """,
                    (now_text, operation_id),
                )
            claimed = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(claimed)

    def reset_incomplete_snapshot(self, operation_id: str) -> None:
        _validate_operation_id(operation_id)
        now = self._now()
        with self._database.transaction() as connection:
            job = connection.execute(
                "SELECT status, snapshot_complete FROM clear_jobs WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if job is None:
                raise ChatClearStoreConflictError("job does not exist")
            if bool(job["snapshot_complete"]):
                raise SnapshotFrozenError("snapshot is already complete")
            if str(job["status"]) != JobStatus.PREPARING.value:
                raise ChatClearStoreConflictError("job is not preparing")
            connection.execute(
                "DELETE FROM clear_items WHERE operation_id=?", (operation_id,)
            )
            connection.execute(
                """
                UPDATE clear_jobs
                SET snapshot_item_count=0, snapshot_bytes=0,
                    candidate_human=0, candidate_bot=0, candidate_skipped=0,
                    updated_at=?
                WHERE operation_id=?
                """,
                (now, operation_id),
            )

    def append_snapshot_items(
        self, operation_id: str, items: Iterable[SnapshotItem]
    ) -> int:
        _validate_operation_id(operation_id)
        materialized = tuple(items)
        if not materialized:
            return 0
        now = self._now()
        with self._database.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if job is None:
                raise ChatClearStoreConflictError("job does not exist")
            if bool(job["snapshot_complete"]):
                raise SnapshotFrozenError("snapshot is already complete")
            if str(job["status"]) != JobStatus.PREPARING.value:
                raise ChatClearStoreConflictError("job is not preparing")
            space_name = str(job["space_name"])

            prepared: list[tuple[Any, ...]] = []
            added_bytes = 0
            for item in materialized:
                message_name = _validate_message(item.message_name, space_name)
                create_time = _canonical_item_timestamp(item.create_time_utc)
                if not isinstance(item.credential_partition, CredentialPartition):
                    raise ChatClearStoreValidationError("invalid credential partition")
                if (
                    not isinstance(item.sender_name, str)
                    or len(item.sender_name.encode("utf-8")) > 1024
                ):
                    raise ChatClearStoreValidationError("invalid sender resource")
                if item.sender_name:
                    _validate_user(item.sender_name)
                if (
                    not isinstance(item.sender_type, str)
                    or len(item.sender_type.encode("utf-8")) > 64
                    or any(ord(character) < 0x20 for character in item.sender_type)
                ):
                    raise ChatClearStoreValidationError("invalid sender type")
                status = (
                    ItemStatus.SKIPPED
                    if item.credential_partition is CredentialPartition.NONE
                    else ItemStatus.PENDING
                )
                added_bytes += sum(
                    len(value.encode("utf-8"))
                    for value in (
                        message_name,
                        create_time,
                        item.sender_name,
                        item.sender_type,
                        item.credential_partition.value,
                    )
                )
                prepared.append(
                    (
                        operation_id,
                        message_name,
                        create_time,
                        item.sender_name,
                        item.sender_type,
                        item.credential_partition.value,
                        status.value,
                        now,
                    )
                )

            new_count = int(job["snapshot_item_count"]) + len(prepared)
            new_bytes = int(job["snapshot_bytes"]) + added_bytes
            if (
                new_count > self.max_snapshot_items
                or new_bytes > self.max_snapshot_bytes
            ):
                raise ChatClearStoreConflictError("snapshot limit exceeded")
            try:
                connection.executemany(
                    """
                    INSERT INTO clear_items(
                        operation_id, message_name, create_time_utc, sender_name,
                        sender_type, credential_partition, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    prepared,
                )
            except sqlite3.IntegrityError:
                raise ChatClearStoreConflictError(
                    "snapshot contains duplicate or invalid items"
                ) from None
            connection.execute(
                """
                UPDATE clear_jobs
                SET snapshot_item_count=?, snapshot_bytes=?, updated_at=?
                WHERE operation_id=?
                """,
                (new_count, new_bytes, now, operation_id),
            )
            return len(prepared)

    def finalize_snapshot(self, operation_id: str) -> ClearJob:
        _validate_operation_id(operation_id)
        now = self._now()
        with self._database.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if job is None:
                raise ChatClearStoreConflictError("job does not exist")
            if bool(job["snapshot_complete"]):
                raise SnapshotFrozenError("snapshot is already complete")
            if str(job["status"]) != JobStatus.PREPARING.value:
                raise ChatClearStoreConflictError("job is not preparing")
            counts = {
                str(row["credential_partition"]): int(row["amount"])
                for row in connection.execute(
                    """
                    SELECT credential_partition, COUNT(*) AS amount
                    FROM clear_items WHERE operation_id=?
                    GROUP BY credential_partition
                    """,
                    (operation_id,),
                )
            }
            human = counts.get(CredentialPartition.USER.value, 0)
            bot = counts.get(CredentialPartition.BOT.value, 0)
            skipped = counts.get(CredentialPartition.NONE.value, 0)
            status = JobStatus.COMPLETED if human + bot == 0 else JobStatus.PREPARING
            connection.execute(
                """
                UPDATE clear_jobs
                SET snapshot_complete=1, candidate_human=?, candidate_bot=?,
                    candidate_skipped=?, skipped_count=?, status=?, updated_at=?
                WHERE operation_id=?
                """,
                (human, bot, skipped, skipped, status.value, now, operation_id),
            )
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def mark_preview_retry(
        self,
        operation_id: str,
        *,
        next_attempt_at: datetime,
        safe_error_category: str,
    ) -> bool:
        _validate_operation_id(operation_id)
        category = _validate_safe_category(safe_error_category)
        retry_at = self._now(next_attempt_at)
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET status='PREVIEW_QUEUED', next_attempt_at=?,
                    safe_error_category=?, updated_at=?
                WHERE operation_id=? AND status='PREPARING'
                  AND snapshot_complete=0
                """,
                (retry_at, category, now, operation_id),
            )
            return result.rowcount == 1

    def mark_preview_failed(self, operation_id: str, safe_error_category: str) -> bool:
        _validate_operation_id(operation_id)
        category = _validate_safe_category(safe_error_category)
        now = self._now()
        with self._database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM clear_items
                WHERE operation_id=? AND EXISTS (
                    SELECT 1 FROM clear_jobs
                    WHERE operation_id=? AND snapshot_complete=0
                )
                """,
                (operation_id, operation_id),
            )
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET status='FAILED', safe_error_category=?, updated_at=?,
                    next_attempt_at=NULL
                WHERE operation_id=? AND status IN ('PREVIEW_QUEUED','PREPARING')
                """,
                (category, now, operation_id),
            )
            return result.rowcount == 1

    def prepare_confirmation_delivery(
        self,
        operation_id: str,
        *,
        requester_name: str,
        space_name: str,
        confirmation_client_message_id: str,
        delivery_generation: int,
        confirm_token_hash: bytes,
        cancel_token_hash: bytes,
    ) -> ClearJob:
        """Persist one immutable card generation before any remote create."""

        _validate_operation_id(operation_id)
        _validate_user(requester_name)
        _validate_space(space_name)
        _validate_client_message_id(confirmation_client_message_id)
        if delivery_generation < 1:
            raise ChatClearStoreValidationError("invalid delivery generation")
        if len(confirm_token_hash) != 32 or len(cancel_token_hash) != 32:
            raise ChatClearStoreValidationError("action token hashes must be SHA-256")
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET confirmation_client_message_id=?,
                    confirmation_delivery_generation=?, confirm_token_hash=?,
                    cancel_token_hash=?, updated_at=?
                WHERE operation_id=? AND requester_name=? AND space_name=?
                  AND status='PREPARING' AND snapshot_complete=1
                  AND confirmation_message_name IS NULL
                  AND confirmation_client_message_id IS NULL
                  AND confirmation_delivery_generation=?
                """,
                (
                    confirmation_client_message_id,
                    delivery_generation,
                    bytes(confirm_token_hash),
                    bytes(cancel_token_hash),
                    now,
                    operation_id,
                    requester_name,
                    space_name,
                    delivery_generation - 1,
                ),
            )
            if result.rowcount != 1:
                raise ChatClearStoreConflictError(
                    "confirmation preparation lost its CAS"
                )
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def abandon_confirmation_delivery(
        self,
        operation_id: str,
        *,
        confirmation_client_message_id: str,
        delivery_generation: int,
    ) -> bool:
        """Abandon only the still-unbound generation selected by the caller."""

        _validate_operation_id(operation_id)
        _validate_client_message_id(confirmation_client_message_id)
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET confirmation_client_message_id=NULL, confirm_token_hash=NULL,
                    cancel_token_hash=NULL, updated_at=?
                WHERE operation_id=? AND status='PREPARING'
                  AND snapshot_complete=1 AND confirmation_message_name IS NULL
                  AND confirmation_client_message_id=?
                  AND confirmation_delivery_generation=?
                """,
                (
                    now,
                    operation_id,
                    confirmation_client_message_id,
                    delivery_generation,
                ),
            )
            return result.rowcount == 1

    def bind_confirmation(
        self,
        operation_id: str,
        *,
        requester_name: str,
        space_name: str,
        confirmation_message_name: str,
        confirmation_client_message_id: str,
        delivery_generation: int,
        confirm_token_hash: bytes,
        cancel_token_hash: bytes,
        expires_at: datetime,
    ) -> ClearJob:
        _validate_operation_id(operation_id)
        _validate_user(requester_name)
        _validate_space(space_name)
        _validate_message(confirmation_message_name, space_name)
        _validate_client_message_id(confirmation_client_message_id)
        if delivery_generation < 1:
            raise ChatClearStoreValidationError("invalid delivery generation")
        if len(confirm_token_hash) != 32 or len(cancel_token_hash) != 32:
            raise ChatClearStoreValidationError("action token hashes must be SHA-256")
        expiry = self._now(expires_at)
        now = self._now()
        if _timestamp_to_epoch(expiry) <= _timestamp_to_epoch(now):
            raise ChatClearStoreValidationError(
                "confirmation expiry must be in the future"
            )

        with self._database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if current is None:
                raise ChatClearStoreConflictError("confirmation bind lost its CAS")
            prepared_id = current["confirmation_client_message_id"]
            if prepared_id is not None:
                hashes_match = (
                    isinstance(current["confirm_token_hash"], bytes)
                    and isinstance(current["cancel_token_hash"], bytes)
                    and hmac.compare_digest(
                        current["confirm_token_hash"], bytes(confirm_token_hash)
                    )
                    and hmac.compare_digest(
                        current["cancel_token_hash"], bytes(cancel_token_hash)
                    )
                )
                if (
                    str(prepared_id) != confirmation_client_message_id
                    or int(current["confirmation_delivery_generation"])
                    != delivery_generation
                    or not hashes_match
                ):
                    raise ChatClearStoreConflictError(
                        "confirmation bind lost its CAS"
                    )
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET status='PENDING_CONFIRMATION', confirmation_message_name=?,
                    confirmation_client_message_id=?,
                    confirmation_delivery_generation=?, confirm_token_hash=?,
                    cancel_token_hash=?, expires_at=?, updated_at=?
                WHERE operation_id=? AND requester_name=? AND space_name=?
                  AND status='PREPARING' AND snapshot_complete=1
                  AND confirmation_message_name IS NULL
                  AND (confirmation_client_message_id IS NULL
                       OR confirmation_client_message_id=?)
                """,
                (
                    confirmation_message_name,
                    confirmation_client_message_id,
                    delivery_generation,
                    bytes(confirm_token_hash),
                    bytes(cancel_token_hash),
                    expiry,
                    now,
                    operation_id,
                    requester_name,
                    space_name,
                    confirmation_client_message_id,
                ),
            )
            if result.rowcount != 1:
                raise ChatClearStoreConflictError("confirmation bind lost its CAS")
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def bind_prepared_confirmation(
        self,
        operation_id: str,
        *,
        requester_name: str,
        space_name: str,
        confirmation_message_name: str,
        confirmation_client_message_id: str,
        delivery_generation: int,
        expires_at: datetime,
    ) -> ClearJob:
        """Bind a recovered create without exposing persisted handle hashes."""

        _validate_operation_id(operation_id)
        _validate_user(requester_name)
        _validate_space(space_name)
        _validate_message(confirmation_message_name, space_name)
        _validate_client_message_id(confirmation_client_message_id)
        if delivery_generation < 1:
            raise ChatClearStoreValidationError("invalid delivery generation")
        expiry = self._now(expires_at)
        now = self._now()
        if _timestamp_to_epoch(expiry) <= _timestamp_to_epoch(now):
            raise ChatClearStoreValidationError(
                "confirmation expiry must be in the future"
            )
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET status='PENDING_CONFIRMATION', confirmation_message_name=?,
                    expires_at=?, updated_at=?
                WHERE operation_id=? AND requester_name=? AND space_name=?
                  AND status='PREPARING' AND snapshot_complete=1
                  AND confirmation_message_name IS NULL
                  AND confirmation_client_message_id=?
                  AND confirmation_delivery_generation=?
                  AND length(confirm_token_hash)=32
                  AND length(cancel_token_hash)=32
                """,
                (
                    confirmation_message_name,
                    expiry,
                    now,
                    operation_id,
                    requester_name,
                    space_name,
                    confirmation_client_message_id,
                    delivery_generation,
                ),
            )
            if result.rowcount != 1:
                raise ChatClearStoreConflictError("confirmation bind lost its CAS")
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def apply_handle(
        self,
        *,
        token_hash: bytes,
        requester_name: str,
        space_name: str,
        confirmation_message_name: str,
        now: datetime | None = None,
    ) -> ActionResult:
        """Infer confirm/cancel from the two stored hashes in one transaction."""

        if len(token_hash) != 32:
            return ActionResult(None, None, False, False)
        if (
            _USER_NAME_RE.fullmatch(requester_name) is None
            or _SPACE_NAME_RE.fullmatch(space_name) is None
        ):
            return ActionResult(None, None, False, False)
        message_match = _MESSAGE_NAME_RE.fullmatch(confirmation_message_name)
        if message_match is None or message_match.group("space") != space_name:
            return ActionResult(None, None, False, False)
        now_text = self._now(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE confirmation_message_name=? LIMIT 1",
                (confirmation_message_name,),
            ).fetchone()
            if row is None:
                return ActionResult(None, None, False, False)
            confirm_hash = row["confirm_token_hash"]
            cancel_hash = row["cancel_token_hash"]
            is_confirm = isinstance(confirm_hash, bytes) and hmac.compare_digest(
                confirm_hash, bytes(token_hash)
            )
            is_cancel = isinstance(cancel_hash, bytes) and hmac.compare_digest(
                cancel_hash, bytes(token_hash)
            )
            authorized = (
                str(row["requester_name"]) == requester_name
                and str(row["space_name"]) == space_name
                and (is_confirm ^ is_cancel)
            )
            if not authorized:
                return ActionResult(None, None, False, False)
            action = ActionKind.CONFIRM if is_confirm else ActionKind.CANCEL
            operation_id = str(row["operation_id"])
            status = JobStatus(str(row["status"]))
            if status is not JobStatus.PENDING_CONFIRMATION:
                return ActionResult(operation_id, status, True, False, action)
            expires_at = row["expires_at"]
            if not isinstance(expires_at, str) or expires_at <= now_text:
                connection.execute(
                    """
                    UPDATE clear_jobs SET status='EXPIRED', updated_at=?
                    WHERE operation_id=? AND status='PENDING_CONFIRMATION'
                    """,
                    (now_text, operation_id),
                )
                return ActionResult(
                    operation_id, JobStatus.EXPIRED, True, True, action
                )
            target = (
                JobStatus.DELETE_QUEUED
                if action is ActionKind.CONFIRM
                else JobStatus.CANCELLED
            )
            result = connection.execute(
                """
                UPDATE clear_jobs SET status=?, updated_at=?, next_attempt_at=?
                WHERE operation_id=? AND status='PENDING_CONFIRMATION'
                """,
                (
                    target.value,
                    now_text,
                    now_text if target is JobStatus.DELETE_QUEUED else None,
                    operation_id,
                ),
            )
            return ActionResult(
                operation_id, target, True, result.rowcount == 1, action
            )

    def apply_action(
        self,
        *,
        action: ActionKind,
        token_hash: bytes,
        requester_name: str,
        space_name: str,
        confirmation_message_name: str,
        now: datetime | None = None,
    ) -> ActionResult:
        if not isinstance(action, ActionKind) or len(token_hash) != 32:
            return ActionResult(None, None, False, False)
        if (
            _USER_NAME_RE.fullmatch(requester_name) is None
            or _SPACE_NAME_RE.fullmatch(space_name) is None
        ):
            return ActionResult(None, None, False, False)
        message_match = _MESSAGE_NAME_RE.fullmatch(confirmation_message_name)
        if message_match is None or message_match.group("space") != space_name:
            return ActionResult(None, None, False, False)
        now_text = self._now(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM clear_jobs
                WHERE confirmation_message_name=?
                LIMIT 1
                """,
                (confirmation_message_name,),
            ).fetchone()
            if row is None:
                return ActionResult(None, None, False, False)
            operation_id = str(row["operation_id"])
            status = JobStatus(str(row["status"]))
            expected = (
                row["confirm_token_hash"]
                if action is ActionKind.CONFIRM
                else row["cancel_token_hash"]
            )
            authorized = (
                str(row["requester_name"]) == requester_name
                and str(row["space_name"]) == space_name
                and isinstance(expected, bytes)
                and hmac.compare_digest(expected, bytes(token_hash))
            )
            if not authorized:
                return ActionResult(None, None, False, False)
            if status is not JobStatus.PENDING_CONFIRMATION:
                return ActionResult(operation_id, status, True, False)
            expires_at = row["expires_at"]
            if not isinstance(expires_at, str) or expires_at <= now_text:
                connection.execute(
                    """
                    UPDATE clear_jobs SET status='EXPIRED', updated_at=?
                    WHERE operation_id=? AND status='PENDING_CONFIRMATION'
                    """,
                    (now_text, operation_id),
                )
                return ActionResult(operation_id, JobStatus.EXPIRED, True, True)
            target = (
                JobStatus.DELETE_QUEUED
                if action is ActionKind.CONFIRM
                else JobStatus.CANCELLED
            )
            result = connection.execute(
                """
                UPDATE clear_jobs SET status=?, updated_at=?, next_attempt_at=?
                WHERE operation_id=? AND status='PENDING_CONFIRMATION'
                """,
                (
                    target.value,
                    now_text,
                    now_text if target is JobStatus.DELETE_QUEUED else None,
                    operation_id,
                ),
            )
            return ActionResult(operation_id, target, True, result.rowcount == 1)

    def expire_pending(self, *, now: datetime | None = None) -> int:
        now_text = self._now(now)
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs SET status='EXPIRED', updated_at=?
                WHERE status='PENDING_CONFIRMATION'
                  AND (expires_at IS NULL OR expires_at <= ?)
                """,
                (now_text, now_text),
            )
            return result.rowcount

    def claim_confirmation_cleanup(
        self, *, now: datetime | None = None
    ) -> ClearJob | None:
        """Claim one terminal confirmation card for best-effort button removal."""

        now_text = self._now(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE status IN ('CANCELLED','EXPIRED')
                  AND confirmation_message_name IS NOT NULL
                  AND final_notification_state='PENDING'
                  AND (final_notification_next_attempt_at IS NULL
                       OR final_notification_next_attempt_at <= ?)
                ORDER BY updated_at, operation_id LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            operation_id = str(row["operation_id"])
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state='SENDING',
                    final_notification_attempts=final_notification_attempts+1,
                    final_notification_next_attempt_at=NULL, updated_at=?
                WHERE operation_id=? AND final_notification_state='PENDING'
                """,
                (now_text, operation_id),
            )
            if result.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(claimed)

    def record_confirmation_cleanup_sent(self, operation_id: str) -> bool:
        _validate_operation_id(operation_id)
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state='SENT',
                    final_message_name=confirmation_message_name,
                    final_notification_next_attempt_at=NULL, updated_at=?
                WHERE operation_id=? AND status IN ('CANCELLED','EXPIRED')
                  AND confirmation_message_name IS NOT NULL
                  AND final_notification_state='SENDING'
                """,
                (now, operation_id),
            )
            return result.rowcount == 1

    def claim_delete_job(
        self,
        operation_id: str,
        *,
        delete_enabled: bool,
    ) -> ClearJob | None:
        _validate_operation_id(operation_id)
        if not delete_enabled:
            return None
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs SET status='RUNNING', updated_at=?
                WHERE operation_id=? AND status='DELETE_QUEUED'
                  AND snapshot_complete=1
                """,
                (now, operation_id),
            )
            if result.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def claim_next_delete_job(
        self,
        *,
        delete_enabled: bool,
        now: datetime | None = None,
    ) -> ClearJob | None:
        """Resume the singleton running job or atomically start the oldest queue."""

        if not delete_enabled:
            return None
        now_text = self._now(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM clear_jobs
                WHERE status IN ('RUNNING','DELETE_QUEUED')
                  AND snapshot_complete=1
                ORDER BY CASE status WHEN 'RUNNING' THEN 0 ELSE 1 END,
                         created_at, operation_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            operation_id = str(row["operation_id"])
            if str(row["status"]) == JobStatus.DELETE_QUEUED.value:
                result = connection.execute(
                    """
                    UPDATE clear_jobs SET status='RUNNING', updated_at=?
                    WHERE operation_id=? AND status='DELETE_QUEUED'
                      AND snapshot_complete=1
                    """,
                    (now_text, operation_id),
                )
                if result.rowcount != 1:
                    return None
            claimed = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(claimed)

    def claim_next_item(
        self, operation_id: str, *, now: datetime | None = None
    ) -> ClearItem | None:
        _validate_operation_id(operation_id)
        now_text = self._now(now)
        with self._database.transaction() as connection:
            job = connection.execute(
                "SELECT status FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if job is None or str(job["status"]) != JobStatus.RUNNING.value:
                return None
            row = connection.execute(
                """
                SELECT * FROM clear_items
                WHERE operation_id=? AND status='PENDING'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY create_time_utc, message_name
                LIMIT 1
                """,
                (operation_id, now_text),
            ).fetchone()
            if row is None:
                return None
            message_name = str(row["message_name"])
            connection.execute(
                """
                UPDATE clear_items
                SET status='RUNNING', attempts=attempts+1,
                    claimed_at=?, updated_at=?, next_attempt_at=NULL
                WHERE operation_id=? AND message_name=? AND status='PENDING'
                """,
                (now_text, now_text, operation_id, message_name),
            )
            connection.execute(
                "UPDATE clear_jobs SET updated_at=? WHERE operation_id=?",
                (now_text, operation_id),
            )
            claimed = connection.execute(
                """
                SELECT * FROM clear_items
                WHERE operation_id=? AND message_name=?
                """,
                (operation_id, message_name),
            ).fetchone()
            return _item_from_row(claimed)

    def record_item_retry(
        self,
        operation_id: str,
        message_name: str,
        *,
        next_attempt_at: datetime,
        safe_error_category: str,
    ) -> bool:
        _validate_operation_id(operation_id)
        category = _validate_safe_category(safe_error_category)
        retry_at = self._now(next_attempt_at)
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_items
                SET status='PENDING', claimed_at=NULL, next_attempt_at=?,
                    safe_error_category=?, updated_at=?
                WHERE operation_id=? AND message_name=? AND status='RUNNING'
                """,
                (retry_at, category, now, operation_id, message_name),
            )
            if result.rowcount == 1:
                connection.execute(
                    "UPDATE clear_jobs SET updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
            return result.rowcount == 1

    def recover_job_claims(
        self,
        operation_id: str,
        *,
        now: datetime | None = None,
        safe_error_category: str = "claim_recovery",
    ) -> int:
        """Release interrupted item leases without touching terminal outcomes."""

        _validate_operation_id(operation_id)
        category = _validate_safe_category(safe_error_category)
        now_text = self._now(now)
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_items
                SET status='PENDING', claimed_at=NULL, next_attempt_at=?,
                    safe_error_category=?, updated_at=?
                WHERE operation_id=? AND status='RUNNING'
                  AND EXISTS (
                    SELECT 1 FROM clear_jobs
                    WHERE operation_id=? AND status='RUNNING'
                  )
                """,
                (
                    now_text,
                    category,
                    now_text,
                    operation_id,
                    operation_id,
                ),
            )
            if result.rowcount:
                connection.execute(
                    "UPDATE clear_jobs SET updated_at=? WHERE operation_id=?",
                    (now_text, operation_id),
                )
            return result.rowcount

    def record_item_result(
        self,
        operation_id: str,
        message_name: str,
        *,
        status: ItemStatus,
        safe_error_category: str = "",
    ) -> bool:
        _validate_operation_id(operation_id)
        if status not in {
            ItemStatus.DELETED,
            ItemStatus.ALREADY_ABSENT,
            ItemStatus.FAILED,
        }:
            raise ChatClearStoreValidationError("invalid terminal item result")
        category = _validate_safe_category(safe_error_category)
        now = self._now()
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_items
                SET status=?, claimed_at=NULL, next_attempt_at=NULL,
                    safe_error_category=?, updated_at=?
                WHERE operation_id=? AND message_name=? AND status='RUNNING'
                """,
                (status.value, category, now, operation_id, message_name),
            )
            if result.rowcount == 1:
                connection.execute(
                    "UPDATE clear_jobs SET updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
            return result.rowcount == 1

    def fail_partition(
        self,
        operation_id: str,
        partition: CredentialPartition,
        safe_error_category: str,
    ) -> int:
        _validate_operation_id(operation_id)
        if partition not in {CredentialPartition.USER, CredentialPartition.BOT}:
            raise ChatClearStoreValidationError("invalid failure partition")
        category = _validate_safe_category(safe_error_category)
        now = self._now()
        column = (
            "user_partition_error"
            if partition is CredentialPartition.USER
            else "bot_partition_error"
        )
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_items
                SET status='FAILED', claimed_at=NULL, next_attempt_at=NULL,
                    safe_error_category=?, updated_at=?
                WHERE operation_id=? AND credential_partition=?
                  AND status IN ('PENDING','RUNNING')
                """,
                (category, now, operation_id, partition.value),
            )
            connection.execute(
                f"UPDATE clear_jobs SET {column}=?, updated_at=? WHERE operation_id=?",
                (category, now, operation_id),
            )
            return result.rowcount

    def reconcile_job(self, operation_id: str, *, finalize: bool = False) -> ClearJob:
        _validate_operation_id(operation_id)
        now = self._now()
        with self._database.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if job is None:
                raise ChatClearStoreConflictError("job does not exist")
            counts = {
                str(row["status"]): int(row["amount"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS amount FROM clear_items
                    WHERE operation_id=? GROUP BY status
                    """,
                    (operation_id,),
                )
            }
            deleted = counts.get(ItemStatus.DELETED.value, 0)
            absent = counts.get(ItemStatus.ALREADY_ABSENT.value, 0)
            skipped = counts.get(ItemStatus.SKIPPED.value, 0)
            failed = counts.get(ItemStatus.FAILED.value, 0)
            status = JobStatus(str(job["status"]))
            remaining = counts.get(ItemStatus.PENDING.value, 0) + counts.get(
                ItemStatus.RUNNING.value, 0
            )
            if finalize:
                if status is not JobStatus.RUNNING or remaining:
                    raise ChatClearStoreConflictError("job cannot be finalized")
                expected_total = (
                    int(job["candidate_human"])
                    + int(job["candidate_bot"])
                    + int(job["candidate_skipped"])
                )
                terminal_total = deleted + absent + skipped + failed
                if expected_total != terminal_total:
                    raise ChatClearStoreConflictError(
                        "item outcome counts do not match the frozen snapshot"
                    )
                successes = deleted + absent
                if failed and successes:
                    status = JobStatus.PARTIAL_FAILED
                elif failed:
                    status = JobStatus.FAILED
                else:
                    status = JobStatus.COMPLETED
            connection.execute(
                """
                UPDATE clear_jobs
                SET deleted_count=?, already_absent_count=?, skipped_count=?,
                    failed_count=?, status=?, updated_at=?
                WHERE operation_id=?
                """,
                (
                    deleted,
                    absent,
                    skipped,
                    failed,
                    status.value,
                    now,
                    operation_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def claim_final_notification(
        self, operation_id: str, *, now: datetime | None = None
    ) -> ClearJob | None:
        _validate_operation_id(operation_id)
        now_text = self._now(now)
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state='SENDING',
                    final_notification_attempts=final_notification_attempts+1,
                    final_notification_next_attempt_at=NULL, updated_at=?
                WHERE operation_id=?
                  AND status IN ('COMPLETED','PARTIAL_FAILED','FAILED')
                  AND final_notification_state='PENDING'
                  AND (final_notification_next_attempt_at IS NULL
                       OR final_notification_next_attempt_at <= ?)
                """,
                (now_text, operation_id, now_text),
            )
            if result.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(row)

    def claim_next_final_notification(
        self, *, now: datetime | None = None
    ) -> ClearJob | None:
        now_text = self._now(now)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE status IN ('COMPLETED','PARTIAL_FAILED','FAILED')
                  AND final_notification_state='PENDING'
                  AND (final_notification_next_attempt_at IS NULL
                       OR final_notification_next_attempt_at <= ?)
                ORDER BY updated_at, operation_id LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            operation_id = str(row["operation_id"])
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state='SENDING',
                    final_notification_attempts=final_notification_attempts+1,
                    final_notification_next_attempt_at=NULL, updated_at=?
                WHERE operation_id=? AND final_notification_state='PENDING'
                """,
                (now_text, operation_id),
            )
            if result.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM clear_jobs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return _job_from_row(claimed)

    def record_final_notification_sent(
        self,
        operation_id: str,
        *,
        message_name: str,
    ) -> bool:
        _validate_operation_id(operation_id)
        now = self._now()
        with self._database.transaction() as connection:
            job = connection.execute(
                "SELECT space_name FROM clear_jobs WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if job is None:
                return False
            _validate_message(message_name, str(job["space_name"]))
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state='SENT', final_message_name=?,
                    final_notification_next_attempt_at=NULL, updated_at=?
                WHERE operation_id=? AND final_notification_state='SENDING'
                """,
                (message_name, now, operation_id),
            )
            return result.rowcount == 1

    def record_final_notification_failure(
        self,
        operation_id: str,
        *,
        safe_error_category: str,
        retry_at: datetime | None = None,
        permanent: bool = False,
    ) -> bool:
        _validate_operation_id(operation_id)
        category = _validate_safe_category(safe_error_category)
        if not permanent and retry_at is None:
            raise ChatClearStoreValidationError("retry time is required")
        now = self._now()
        retry_text = self._now(retry_at) if retry_at is not None else None
        target = NotificationStatus.FAILED if permanent else NotificationStatus.PENDING
        with self._database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state=?,
                    final_notification_next_attempt_at=?,
                    safe_error_category=?, updated_at=?
                WHERE operation_id=? AND final_notification_state='SENDING'
                """,
                (target.value, retry_text, category, now, operation_id),
            )
            return result.rowcount == 1

    def list_items(self, operation_id: str) -> tuple[ClearItem, ...]:
        _validate_operation_id(operation_id)
        connection = self._database.connect()
        try:
            return tuple(
                _item_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM clear_items WHERE operation_id=?
                    ORDER BY create_time_utc, message_name
                    """,
                    (operation_id,),
                )
            )
        finally:
            connection.close()

    def recover_interrupted(
        self,
        *,
        now: datetime | None = None,
        running_max_age_seconds: float = CHAT_HISTORY_RUNNING_MAX_AGE_SECONDS,
    ) -> RecoveryResult:
        now_value = now if now is not None else self._clock()
        now_text = self._now(now_value)
        stale_before = self._now(
            now_value - timedelta(seconds=max(0.0, running_max_age_seconds))
        )
        rebuilt = expired = recovered_items = recovered_notifications = stale_jobs = 0
        with self._database.transaction() as connection:
            incomplete = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE status='PREPARING' AND snapshot_complete=0
                """
            ).fetchall()
            for row in incomplete:
                operation_id = str(row["operation_id"])
                connection.execute(
                    "DELETE FROM clear_items WHERE operation_id=?", (operation_id,)
                )
                connection.execute(
                    """
                    UPDATE clear_jobs
                    SET status='PREVIEW_QUEUED', snapshot_item_count=0,
                        snapshot_bytes=0, candidate_human=0, candidate_bot=0,
                        candidate_skipped=0, next_attempt_at=?, updated_at=?,
                        safe_error_category='snapshot_recovery'
                    WHERE operation_id=?
                    """,
                    (now_text, now_text, operation_id),
                )
                rebuilt += 1

            expiry_result = connection.execute(
                """
                UPDATE clear_jobs SET status='EXPIRED', updated_at=?
                WHERE status='PENDING_CONFIRMATION'
                  AND (expires_at IS NULL OR expires_at <= ?)
                """,
                (now_text, now_text),
            )
            expired = expiry_result.rowcount

            stale_rows = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE status='RUNNING' AND updated_at <= ?
                """,
                (stale_before,),
            ).fetchall()
            for row in stale_rows:
                operation_id = str(row["operation_id"])
                connection.execute(
                    """
                    UPDATE clear_items
                    SET status='FAILED', claimed_at=NULL, next_attempt_at=NULL,
                        safe_error_category='job_age_exceeded', updated_at=?
                    WHERE operation_id=? AND status IN ('PENDING','RUNNING')
                    """,
                    (now_text, operation_id),
                )
                stale_jobs += 1

            item_result = connection.execute(
                """
                UPDATE clear_items
                SET status='PENDING', claimed_at=NULL, next_attempt_at=?,
                    safe_error_category='claim_recovery', updated_at=?
                WHERE status='RUNNING'
                  AND operation_id IN (
                    SELECT operation_id FROM clear_jobs WHERE status='RUNNING'
                  )
                """,
                (now_text, now_text),
            )
            recovered_items = item_result.rowcount

            notification_result = connection.execute(
                """
                UPDATE clear_jobs
                SET final_notification_state='PENDING',
                    final_notification_next_attempt_at=?, updated_at=?
                WHERE final_notification_state='SENDING'
                """,
                (now_text, now_text),
            )
            recovered_notifications = notification_result.rowcount

            for row in stale_rows:
                operation_id = str(row["operation_id"])
                counts = {
                    str(count_row["status"]): int(count_row["amount"])
                    for count_row in connection.execute(
                        """
                        SELECT status, COUNT(*) AS amount FROM clear_items
                        WHERE operation_id=? GROUP BY status
                        """,
                        (operation_id,),
                    )
                }
                deleted = counts.get(ItemStatus.DELETED.value, 0)
                absent = counts.get(ItemStatus.ALREADY_ABSENT.value, 0)
                failed = counts.get(ItemStatus.FAILED.value, 0)
                skipped = counts.get(ItemStatus.SKIPPED.value, 0)
                status = (
                    JobStatus.PARTIAL_FAILED
                    if failed and deleted + absent
                    else JobStatus.FAILED
                )
                connection.execute(
                    """
                    UPDATE clear_jobs SET status=?, deleted_count=?,
                        already_absent_count=?, failed_count=?, skipped_count=?,
                        safe_error_category='job_age_exceeded', updated_at=?
                    WHERE operation_id=?
                    """,
                    (
                        status.value,
                        deleted,
                        absent,
                        failed,
                        skipped,
                        now_text,
                        operation_id,
                    ),
                )

            running_rows = connection.execute(
                "SELECT operation_id FROM clear_jobs WHERE status='RUNNING'"
            ).fetchall()
            for row in running_rows:
                operation_id = str(row["operation_id"])
                counts = {
                    str(count_row["status"]): int(count_row["amount"])
                    for count_row in connection.execute(
                        """
                        SELECT status, COUNT(*) AS amount FROM clear_items
                        WHERE operation_id=? GROUP BY status
                        """,
                        (operation_id,),
                    )
                }
                remaining = counts.get(ItemStatus.PENDING.value, 0) + counts.get(
                    ItemStatus.RUNNING.value, 0
                )
                if remaining:
                    continue
                deleted = counts.get(ItemStatus.DELETED.value, 0)
                absent = counts.get(ItemStatus.ALREADY_ABSENT.value, 0)
                failed = counts.get(ItemStatus.FAILED.value, 0)
                skipped = counts.get(ItemStatus.SKIPPED.value, 0)
                target = (
                    JobStatus.PARTIAL_FAILED
                    if failed and deleted + absent
                    else JobStatus.FAILED
                    if failed
                    else JobStatus.COMPLETED
                )
                connection.execute(
                    """
                    UPDATE clear_jobs SET status=?, deleted_count=?,
                        already_absent_count=?, failed_count=?, skipped_count=?,
                        updated_at=? WHERE operation_id=? AND status='RUNNING'
                    """,
                    (
                        target.value,
                        deleted,
                        absent,
                        failed,
                        skipped,
                        now_text,
                        operation_id,
                    ),
                )

        return RecoveryResult(
            rebuilt_snapshots=rebuilt,
            expired_jobs=expired,
            recovered_items=recovered_items,
            recovered_notifications=recovered_notifications,
            stale_jobs=stale_jobs,
        )

    def record_worker_heartbeat(
        self, owner_id: str, *, now: datetime | None = None
    ) -> None:
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 128:
            raise ChatClearStoreValidationError("invalid worker owner")
        now_text = self._now(now)
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE worker_state SET owner_id=?, heartbeat_at=? WHERE singleton=1
                """,
                (owner_id, now_text),
            )

    def clear_worker_heartbeat(self, owner_id: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE worker_state SET owner_id=NULL, heartbeat_at=NULL
                WHERE singleton=1 AND owner_id=?
                """,
                (owner_id,),
            )

    def diagnostics(self, *, now: datetime | None = None) -> StoreDiagnostics:
        now_value = now if now is not None else self._clock()
        connection = self._database.connect()
        try:
            job_counts = {
                str(row["status"]): int(row["amount"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS amount FROM clear_jobs GROUP BY status"
                )
            }
            item_counts = {
                str(row["status"]): int(row["amount"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS amount FROM clear_items GROUP BY status"
                )
            }
            oldest = connection.execute(
                """
                SELECT MIN(created_at) FROM clear_jobs
                WHERE status IN (
                    'PREVIEW_QUEUED','PREPARING','PENDING_CONFIRMATION',
                    'DELETE_QUEUED','RUNNING'
                )
                """
            ).fetchone()[0]
            worker = connection.execute(
                "SELECT owner_id, heartbeat_at FROM worker_state WHERE singleton=1"
            ).fetchone()
            partition_failures = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN user_partition_error != '' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN bot_partition_error != '' THEN 1 ELSE 0 END)
                FROM clear_jobs
                """
            ).fetchone()
            pending_final_notifications = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM clear_jobs
                    WHERE status IN ('COMPLETED','PARTIAL_FAILED','FAILED')
                      AND final_notification_state IN ('PENDING','SENDING')
                    """
                ).fetchone()[0]
            )
        finally:
            connection.close()
        age = (
            max(0.0, now_value.timestamp() - _timestamp_to_epoch(str(oldest)))
            if oldest is not None
            else None
        )
        wal_path = Path(f"{self.database_path}-wal")
        try:
            wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        except OSError:
            wal_bytes = 0
        return StoreDiagnostics(
            job_counts=job_counts,
            item_counts=item_counts,
            oldest_active_age_seconds=age,
            worker_owner=(
                str(worker["owner_id"])
                if worker is not None and worker["owner_id"] is not None
                else None
            ),
            worker_heartbeat_at=(
                str(worker["heartbeat_at"])
                if worker is not None and worker["heartbeat_at"] is not None
                else None
            ),
            partition_failure_counts={
                CredentialPartition.USER.value: int(partition_failures[0] or 0),
                CredentialPartition.BOT.value: int(partition_failures[1] or 0),
            },
            pending_final_notifications=pending_final_notifications,
            wal_bytes=wal_bytes,
        )

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        normalized = mode.upper()
        if normalized not in {"PASSIVE", "TRUNCATE"}:
            raise ChatClearStoreValidationError("unsupported checkpoint mode")
        connection = self._database.connect()
        try:
            if normalized == "TRUNCATE":
                active = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM clear_jobs WHERE status IN (
                            'PREVIEW_QUEUED','PREPARING','PENDING_CONFIRMATION',
                            'DELETE_QUEUED','RUNNING'
                        )
                        """
                    ).fetchone()[0]
                )
                if active:
                    raise ChatClearStoreConflictError(
                        "truncate checkpoint requires no active jobs"
                    )
            row = connection.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
            return int(row[0]), int(row[1]), int(row[2])
        finally:
            connection.close()
            self._database._repair_modes()

    def maintain(
        self,
        *,
        now: datetime | None = None,
        retention_days: int = CHAT_HISTORY_TERMINAL_RETENTION_DAYS,
        batch_size: int = CHAT_HISTORY_MAINTENANCE_BATCH,
    ) -> MaintenanceResult:
        if retention_days < 1 or batch_size < 1:
            raise ChatClearStoreValidationError("invalid maintenance limits")
        now_value = now if now is not None else self._clock()
        cutoff = self._now(now_value - timedelta(days=retention_days))
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT operation_id FROM clear_jobs
                WHERE status IN ('COMPLETED','PARTIAL_FAILED','FAILED','CANCELLED','EXPIRED')
                  AND updated_at < ?
                ORDER BY updated_at, operation_id
                LIMIT ?
                """,
                (cutoff, batch_size),
            ).fetchall()
            operation_ids = [str(row[0]) for row in rows]
            if operation_ids:
                placeholders = ",".join("?" for _ in operation_ids)
                connection.execute(
                    f"DELETE FROM clear_jobs WHERE operation_id IN ({placeholders})",
                    operation_ids,
                )
        busy, log_frames, checkpointed = self.checkpoint("PASSIVE")
        return MaintenanceResult(
            pruned_jobs=len(operation_ids),
            checkpoint_busy=busy,
            checkpoint_log_frames=log_frames,
            checkpointed_frames=checkpointed,
        )


class ChatWritePacer:
    """Durably reserve per-space Chat write slots across threads/processes."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        min_interval_seconds: float = CHAT_WRITE_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be positive")
        self._database = _HistoryDatabase(state_dir)
        self.state_dir = self._database.state_dir
        self.database_path = self._database.database_path
        self.min_interval_seconds = float(min_interval_seconds)
        self._clock = clock
        self._sleeper = sleeper

    def reserve(self, space_name: str) -> float:
        try:
            _validate_space(space_name)
            now = float(self._clock())
            now_text = _format_epoch(now)
            with self._database.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT next_write_at_utc FROM space_write_pacer
                    WHERE space_name=?
                    """,
                    (space_name,),
                ).fetchone()
                previous = _timestamp_to_epoch(str(row[0])) if row is not None else now
                slot = max(now, previous)
                connection.execute(
                    """
                    INSERT INTO space_write_pacer(
                        space_name, next_write_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(space_name) DO UPDATE SET
                        next_write_at_utc=excluded.next_write_at_utc,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    (
                        space_name,
                        _format_epoch(slot + self.min_interval_seconds),
                        now_text,
                    ),
                )
                return slot
        except ChatClearStoreError as error:
            raise ChatWritePacerError(str(error)) from None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise ChatWritePacerError(
                "history write pacing reservation failed"
            ) from None

    def record_external_write(
        self, space_name: str, *, at: float | None = None
    ) -> float:
        """Conservatively pace a synchronous callback update before waking workers."""

        try:
            _validate_space(space_name)
            callback_time = float(self._clock() if at is None else at)
            with self._database.transaction() as connection:
                row = connection.execute(
                    "SELECT next_write_at_utc FROM space_write_pacer WHERE space_name=?",
                    (space_name,),
                ).fetchone()
                previous = (
                    _timestamp_to_epoch(str(row[0]))
                    if row is not None
                    else callback_time
                )
                next_write = max(
                    previous,
                    callback_time + self.min_interval_seconds,
                )
                connection.execute(
                    """
                    INSERT INTO space_write_pacer(
                        space_name, next_write_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(space_name) DO UPDATE SET
                        next_write_at_utc=excluded.next_write_at_utc,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    (
                        space_name,
                        _format_epoch(next_write),
                        _format_epoch(callback_time),
                    ),
                )
                return next_write
        except ChatClearStoreError as error:
            raise ChatWritePacerError(str(error)) from None

    def wait_for_turn(self, space_name: str) -> float:
        slot = self.reserve(space_name)
        delay = max(0.0, slot - float(self._clock()))
        if delay:
            self._sleeper(delay)
        return slot


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS",
    "CHAT_HISTORY_DATABASE_NAME",
    "CHAT_HISTORY_SCHEMA_VERSION",
    "CHAT_WRITE_MIN_INTERVAL_SECONDS",
    "ActionKind",
    "ActionResult",
    "ActiveDeleteJobError",
    "ChatClearStore",
    "ChatClearStoreConflictError",
    "ChatClearStoreError",
    "ChatClearStoreSchemaError",
    "ChatClearStoreUnavailableError",
    "ChatClearStoreValidationError",
    "ChatWritePacer",
    "ChatWritePacerError",
    "ClearItem",
    "ClearJob",
    "ClearJobRequest",
    "CreateJobResult",
    "CredentialPartition",
    "ItemStatus",
    "JobStatus",
    "MaintenanceResult",
    "NotificationStatus",
    "RecoveryResult",
    "SnapshotFrozenError",
    "SnapshotItem",
    "StoreDiagnostics",
    "StorePreflight",
]
