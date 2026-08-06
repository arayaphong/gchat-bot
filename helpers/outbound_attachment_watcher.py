from __future__ import annotations

import fcntl
import hashlib
import os
import queue
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Self

DEFAULT_SOURCE_DIRS = (
    Path("~/.openclaw/workspace/uploads").expanduser(),
    Path("~/.openclaw/media/tool-image-generation").expanduser(),
)

# Linux inotify masks.  Keeping the small set used by the service here makes the
# event-processing code importable in tests.  The production factory below is
# still backed by inotify_simple.
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000

WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF


class InotifyEvent(Protocol):
    wd: int
    mask: int
    name: str


class InotifyHandle(Protocol):
    def add_watch(self, path: str, mask: int) -> int: ...

    def read(self, timeout: int | None = None) -> list[InotifyEvent]: ...

    def close(self) -> None: ...


InotifyFactory = Callable[[], InotifyHandle]


def _inotify_simple_factory() -> InotifyHandle:
    try:
        from inotify_simple import INotify
    except ImportError as error:  # pragma: no cover - depends on deployment
        raise RuntimeError(
            "outbound attachment watching requires the inotify-simple package"
        ) from error
    return INotify(nonblocking=True)


class DeliveryDisposition(Enum):
    """Result returned by the injected, one-file delivery callback."""

    DELIVERED = "delivered"
    FAILED = "failed"
    DEFERRED = "deferred"


class AttachmentSubmissionDisposition(Enum):
    """Durable-ingress result for an explicitly referenced local file."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


_DELIVERY_ID_NAMESPACE = uuid.UUID("20f3495c-a130-4e3b-a6b5-cda5dc0ef596")


@dataclass(frozen=True)
class OutboundAttachment:
    source_path: Path
    source_root: Path
    staged_path: Path | None
    display_name: str
    size: int
    sha256: str
    delivery_id: str = ""
    drive_file_id: str = ""
    web_view_link: str = ""


@dataclass(frozen=True)
class OutboundDeliveryResult:
    """Delivery disposition plus a reusable remote-upload receipt."""

    disposition: DeliveryDisposition
    drive_file_id: str = ""
    web_view_link: str = ""


@dataclass(frozen=True)
class AttachmentSubmissionResult:
    disposition: AttachmentSubmissionDisposition
    error_category: str = ""


@dataclass(frozen=True)
class FinalDeliveryFailure:
    attachment: OutboundAttachment
    attempts: int
    error_category: str


DeliveryCallback = Callable[
    [OutboundAttachment], OutboundDeliveryResult | DeliveryDisposition | bool
]
FinalFailureCallback = Callable[[FinalDeliveryFailure], None]


@dataclass(frozen=True)
class OutboundAttachmentConfig:
    source_dirs: tuple[Path, Path]
    watched_source_dirs: tuple[Path, ...]
    state_dir: Path
    max_file_bytes: int = 20 * 1024 * 1024
    stability_checks: int = 2
    stability_interval_seconds: float = 0.25
    readiness_timeout_seconds: float = 30.0
    max_delivery_attempts: int = 3
    retry_delays_seconds: tuple[float, ...] = (1.0, 5.0)
    capture_retry_delays_seconds: tuple[float, ...] = (0.25, 1.0)
    deferral_delay_seconds: float = 1.0
    notification_retry_delay_seconds: float = 5.0
    worker_poll_seconds: float = 0.1
    inotify_read_timeout_ms: int = 200
    lock_retry_seconds: float = 0.5

    def __post_init__(self) -> None:
        normalized_sources = tuple(
            path.expanduser().resolve(strict=False) for path in self.source_dirs
        )
        if len(normalized_sources) != 2 or len(set(normalized_sources)) != 2:
            raise ValueError("source_dirs must contain exactly two distinct paths")
        object.__setattr__(self, "source_dirs", normalized_sources)
        normalized_watched_sources = tuple(
            path.expanduser().resolve(strict=False) for path in self.watched_source_dirs
        )
        if (
            not normalized_watched_sources
            or len(set(normalized_watched_sources)) != len(normalized_watched_sources)
            or any(
                path not in normalized_sources for path in normalized_watched_sources
            )
        ):
            raise ValueError(
                "watched_source_dirs must be a non-empty distinct subset of source_dirs"
            )
        object.__setattr__(self, "watched_source_dirs", normalized_watched_sources)
        normalized_state = self.state_dir.expanduser().resolve(strict=False)
        object.__setattr__(self, "state_dir", normalized_state)
        if any(
            normalized_state == source
            or normalized_state.is_relative_to(source)
            or source.is_relative_to(normalized_state)
            for source in normalized_sources
        ):
            raise ValueError("state_dir and source_dirs must not contain one another")

        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if self.stability_checks < 1:
            raise ValueError("stability_checks must be positive")
        if self.stability_interval_seconds < 0:
            raise ValueError("stability_interval_seconds cannot be negative")
        if self.readiness_timeout_seconds <= 0:
            raise ValueError("readiness_timeout_seconds must be positive")
        if self.max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if not self.retry_delays_seconds and self.max_delivery_attempts > 1:
            raise ValueError(
                "retry_delays_seconds is required when retries are enabled"
            )
        if any(delay < 0 for delay in self.retry_delays_seconds):
            raise ValueError("retry delays cannot be negative")
        if any(delay < 0 for delay in self.capture_retry_delays_seconds):
            raise ValueError("capture retry delays cannot be negative")
        if self.deferral_delay_seconds <= 0:
            raise ValueError("deferral_delay_seconds must be positive")
        if self.notification_retry_delay_seconds <= 0:
            raise ValueError("notification_retry_delay_seconds must be positive")
        if self.worker_poll_seconds <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        if self.inotify_read_timeout_ms < 1:
            raise ValueError("inotify_read_timeout_ms must be positive")
        if self.lock_retry_seconds <= 0:
            raise ValueError("lock_retry_seconds must be positive")

    @classmethod
    def default(cls, state_dir: Path | None = None) -> OutboundAttachmentConfig:
        if state_dir is None:
            xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
            state_root = (
                Path(xdg_state).expanduser()
                if xdg_state
                else Path("~/.local/state").expanduser()
            )
            state_dir = state_root / "gchat-bot" / "outbound-attachments"
        return cls(
            source_dirs=DEFAULT_SOURCE_DIRS,
            watched_source_dirs=(DEFAULT_SOURCE_DIRS[0],),
            state_dir=state_dir,
        )


class _SingletonProcessLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class _StagingTransactionLock:
    """Serialize staging cleanup and cross-process explicit submissions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_exc_info: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _DeliveryRecord:
    record_id: int
    attachment: OutboundAttachment
    attempts: int
    last_error_category: str


class _Ledger:
    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        schema_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if schema_version > 1:
            self._connection.close()
            raise RuntimeError(
                "outbound attachment ledger was created by a newer version"
            )
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signature TEXT NOT NULL UNIQUE,
                    source_root TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    staged_path TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error_category TEXT NOT NULL DEFAULT '',
                    failure_notified INTEGER NOT NULL DEFAULT 0,
                    next_notification_at REAL NOT NULL DEFAULT 0,
                    drive_file_id TEXT NOT NULL DEFAULT '',
                    web_view_link TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(artifacts)")
            }
            optional_columns = {
                "sha256": "TEXT NOT NULL DEFAULT ''",
                "staged_path": "TEXT",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "REAL NOT NULL DEFAULT 0",
                "last_error_category": "TEXT NOT NULL DEFAULT ''",
                "failure_notified": "INTEGER NOT NULL DEFAULT 0",
                "next_notification_at": "REAL NOT NULL DEFAULT 0",
                "drive_file_id": "TEXT NOT NULL DEFAULT ''",
                "web_view_link": "TEXT NOT NULL DEFAULT ''",
            }
            for column, declaration in optional_columns.items():
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}"
                    )
            self._connection.execute("PRAGMA user_version = 1")
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS artifacts_due
                ON artifacts(status, next_attempt_at, id)
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def baseline_state(self) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'baseline-state'"
            ).fetchone()
        return str(row["value"]) if row else "new"

    def begin_baseline(self) -> int:
        cutover_ns = time.time_ns()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('baseline-cutover-ns', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(cutover_ns),),
            )
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('baseline-state', 'started')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
        return cutover_ns

    def baseline_cutover_ns(self) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'baseline-cutover-ns'"
            ).fetchone()
        if row is None:
            return None
        try:
            cutover_ns = int(row["value"])
        except (TypeError, ValueError):
            return None
        return cutover_ns if cutover_ns > 0 else None

    def repair_missing_baseline_cutover(self) -> int:
        """Add a safe cutover for a legacy interrupted-baseline ledger."""
        cutover_ns = time.time_ns()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('baseline-cutover-ns', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(cutover_ns),),
            )
        return cutover_ns

    def finish_baseline(
        self,
        entries: Sequence[tuple[str, Path, Path, _FileIdentity]],
    ) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO artifacts(
                    signature, source_root, source_path, display_name,
                    device, inode, size, mtime_ns, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'baseline', ?, ?)
                """,
                [
                    (
                        signature,
                        str(source_root),
                        str(source_path),
                        source_path.name,
                        identity.device,
                        identity.inode,
                        identity.size,
                        identity.mtime_ns,
                        now,
                        now,
                    )
                    for signature, source_root, source_path, identity in entries
                ],
            )
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('baseline-state', 'complete')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def signature_status(self, signature: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM artifacts WHERE signature = ?", (signature,)
            ).fetchone()
        return str(row["status"]) if row else None

    def register_staged(
        self,
        *,
        signature: str,
        source_root: Path,
        source_path: Path,
        identity: _FileIdentity,
        sha256: str,
        staged_path: Path,
        promote_baseline: bool,
    ) -> bool:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT id, status FROM artifacts WHERE signature = ?", (signature,)
            ).fetchone()
            if row is not None:
                if row["status"] != "baseline" or not promote_baseline:
                    return False
                self._connection.execute(
                    """
                    UPDATE artifacts
                    SET sha256 = ?, staged_path = ?, status = 'pending',
                        attempts = 0, next_attempt_at = 0,
                        last_error_category = '', failure_notified = 0,
                        drive_file_id = '', web_view_link = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (sha256, str(staged_path), now, row["id"]),
                )
                return True

            self._connection.execute(
                """
                INSERT INTO artifacts(
                    signature, source_root, source_path, display_name,
                    device, inode, size, mtime_ns, sha256, staged_path,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    signature,
                    str(source_root),
                    str(source_path),
                    source_path.name,
                    identity.device,
                    identity.inode,
                    identity.size,
                    identity.mtime_ns,
                    sha256,
                    str(staged_path),
                    now,
                    now,
                ),
            )
            return True

    def register_validation_failure(
        self,
        *,
        signature: str,
        source_root: Path,
        source_path: Path,
        identity: _FileIdentity,
        error_category: str,
        promote_baseline: bool,
    ) -> bool:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT id, status FROM artifacts WHERE signature = ?", (signature,)
            ).fetchone()
            if row is not None:
                if row["status"] != "baseline" or not promote_baseline:
                    return False
                self._connection.execute(
                    """
                    UPDATE artifacts
                    SET status = 'failed', attempts = 0,
                        last_error_category = ?, failure_notified = 0,
                        next_notification_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error_category, now, now, row["id"]),
                )
                return True

            self._connection.execute(
                """
                INSERT INTO artifacts(
                    signature, source_root, source_path, display_name,
                    device, inode, size, mtime_ns, status, attempts,
                    last_error_category, next_notification_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', 0, ?, ?, ?, ?)
                """,
                (
                    signature,
                    str(source_root),
                    str(source_path),
                    source_path.name,
                    identity.device,
                    identity.inode,
                    identity.size,
                    identity.mtime_ns,
                    error_category,
                    now,
                    now,
                    now,
                ),
            )
            return True

    def recover_interrupted(self) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE artifacts
                SET status = 'retry', next_attempt_at = ?, updated_at = ?
                WHERE status = 'delivering'
                """,
                (now, now),
            )

    def claim_due(self) -> _DeliveryRecord | None:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT * FROM artifacts
                WHERE status IN ('pending', 'retry', 'deferred')
                  AND next_attempt_at <= ?
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            updated = self._connection.execute(
                """
                UPDATE artifacts SET status = 'delivering', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retry', 'deferred')
                """,
                (now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
        return self._record_from_row(row)

    def mark_delivered(self, record_id: int) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE artifacts
                SET status = 'sent', last_error_category = '', updated_at = ?
                WHERE id = ?
                """,
                (now, record_id),
            )

    def clear_staged_path(self, record_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE artifacts SET staged_path = NULL, updated_at = ? WHERE id = ?",
                (time.time(), record_id),
            )

    def store_drive_receipt(
        self, record_id: int, drive_file_id: str, web_view_link: str
    ) -> None:
        normalized_link = web_view_link.strip()
        if not normalized_link:
            return
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE artifacts
                SET drive_file_id = ?, web_view_link = ?, updated_at = ?
                WHERE id = ?
                """,
                (drive_file_id, normalized_link, time.time(), record_id),
            )

    def mark_deferred(self, record_id: int, delay: float) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE artifacts
                SET status = 'deferred', next_attempt_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now + delay, now, record_id),
            )

    def mark_failed_attempt(
        self,
        record_id: int,
        *,
        error_category: str,
        max_attempts: int,
        retry_delay: float,
    ) -> tuple[bool, int]:
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT attempts FROM artifacts WHERE id = ?", (record_id,)
            ).fetchone()
            attempts = (int(row["attempts"]) if row else 0) + 1
            final = attempts >= max_attempts
            self._connection.execute(
                """
                UPDATE artifacts
                SET status = ?, attempts = ?, next_attempt_at = ?,
                    last_error_category = ?, next_notification_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "failed" if final else "retry",
                    attempts,
                    0 if final else now + retry_delay,
                    error_category,
                    now if final else 0,
                    now,
                    record_id,
                ),
            )
        return final, attempts

    def next_failure_notification(self) -> _DeliveryRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM artifacts
                WHERE status = 'failed' AND failure_notified = 0
                  AND next_notification_at <= ?
                ORDER BY id
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def mark_failure_notified(self, record_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE artifacts
                SET failure_notified = 1, updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (time.time(), record_id),
            )

    def defer_failure_notification(self, record_id: int, delay: float) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE artifacts
                SET next_notification_at = ?, updated_at = ?
                WHERE id = ? AND status = 'failed' AND failure_notified = 0
                """,
                (now + delay, now, record_id),
            )

    def sent_staging_rows(self) -> list[tuple[int, Path]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, staged_path FROM artifacts
                WHERE staged_path IS NOT NULL
                  AND (status = 'sent'
                       OR (status = 'failed' AND failure_notified = 1))
                """
            ).fetchall()
        return [(int(row["id"]), Path(row["staged_path"])) for row in rows]

    def referenced_staging_paths(self) -> set[Path]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT staged_path FROM artifacts WHERE staged_path IS NOT NULL"
            ).fetchall()
        return {Path(str(row["staged_path"])) for row in rows}

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM artifacts GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> _DeliveryRecord:
        staged_path = row["staged_path"]
        signature = str(row["signature"])
        return _DeliveryRecord(
            record_id=int(row["id"]),
            attachment=OutboundAttachment(
                source_path=Path(row["source_path"]),
                source_root=Path(row["source_root"]),
                staged_path=Path(staged_path) if staged_path else None,
                display_name=str(row["display_name"]),
                size=int(row["size"]),
                sha256=str(row["sha256"]),
                delivery_id=str(uuid.uuid5(_DELIVERY_ID_NAMESPACE, signature)),
                drive_file_id=str(row["drive_file_id"]),
                web_view_link=str(row["web_view_link"]),
            ),
            attempts=int(row["attempts"]),
            last_error_category=str(row["last_error_category"]),
        )


_RESCAN = object()


@dataclass(frozen=True)
class _CandidateJob:
    path: Path
    promote_baseline: bool


class OutboundAttachmentService:
    """Capture allowed bot outputs and durably deliver each file once.

    ``start`` is intentionally non-blocking.  A process that cannot acquire the
    singleton lock remains a standby and retries until the active owner exits.
    ``wait_until_active`` is available for startup health checks and tests.
    Explicit submissions are persisted through SQLite and therefore also work
    when called by a standby process.
    """

    def __init__(
        self,
        *,
        delivery_callback: DeliveryCallback,
        final_failure_callback: FinalFailureCallback,
        config: OutboundAttachmentConfig | None = None,
        inotify_factory: InotifyFactory = _inotify_simple_factory,
    ) -> None:
        self._config = config or OutboundAttachmentConfig.default()
        self._delivery_callback = delivery_callback
        self._final_failure_callback = final_failure_callback
        self._inotify_factory = inotify_factory

        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._active_event = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._active_stop: threading.Event | None = None
        self._inotify: InotifyHandle | None = None
        self._ledger: _Ledger | None = None
        self._jobs: queue.Queue[_CandidateJob | object] | None = None
        self._watch_roots: dict[int, Path] = {}
        self._last_start_error: Exception | None = None
        self._process_lock = _SingletonProcessLock(
            self._config.state_dir / "watcher.lock"
        )

    @property
    def is_active(self) -> bool:
        return self._active_event.is_set()

    @property
    def last_start_error(self) -> Exception | None:
        return self._last_start_error

    def status_counts(self) -> dict[str, int]:
        ledger = self._ledger
        return ledger.status_counts() if ledger is not None else {}

    def submit_explicit(
        self,
        path: str | Path,
        *,
        idempotency_key: str,
    ) -> AttachmentSubmissionResult:
        """Securely stage a MEDIA-referenced file before acknowledging it."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")

        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "invalid_path",
            )
        if not candidate.is_absolute():
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "path_not_absolute",
            )
        try:
            candidate = Path(os.path.abspath(candidate))
        except (OSError, ValueError):
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "invalid_path",
            )
        source_root = self._source_root_for(candidate)
        if source_root is None:
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "path_not_allowed",
            )
        if source_root in self._config.watched_source_dirs:
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "path_already_watched",
            )
        if self._should_ignore_name(candidate.name):
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "ignored_name",
            )

        signature = self._explicit_signature(idempotency_key.strip())
        ledger: _Ledger | None = None
        staged_path: Path | None = None
        try:
            self._ensure_state_directories()
            with _StagingTransactionLock(self._config.state_dir / "staging.lock"):
                ledger = _Ledger(self._config.state_dir / "ledger.sqlite3")
                if ledger.signature_status(signature) is not None:
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                identity, last_identity, readiness_error = (
                    self._wait_for_explicit_identity(candidate, source_root)
                )
                if identity is None:
                    if readiness_error in {
                        "invalid_path",
                        "not_regular_file",
                        "source_root_unavailable",
                        "symlink_not_allowed",
                    }:
                        return AttachmentSubmissionResult(
                            AttachmentSubmissionDisposition.REJECTED,
                            readiness_error,
                        )
                    ledger.register_validation_failure(
                        signature=signature,
                        source_root=source_root,
                        source_path=candidate,
                        identity=last_identity
                        or _FileIdentity(
                            device=0,
                            inode=0,
                            size=0,
                            mtime_ns=0,
                            ctime_ns=0,
                        ),
                        error_category=readiness_error,
                        promote_baseline=False,
                    )
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                validation_error = (
                    "empty_file"
                    if identity.size == 0
                    else "file_too_large"
                    if identity.size > self._config.max_file_bytes
                    else ""
                )
                if validation_error:
                    ledger.register_validation_failure(
                        signature=signature,
                        source_root=source_root,
                        source_path=candidate,
                        identity=identity,
                        error_category=validation_error,
                        promote_baseline=False,
                    )
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                capture_error = "staging_failed"
                sha256 = ""
                for capture_attempt in range(
                    len(self._config.capture_retry_delays_seconds) + 1
                ):
                    try:
                        captured = self._capture_explicit_to_staging(
                            source_root,
                            candidate,
                            identity,
                        )
                    except (OSError, ValueError):
                        captured = None
                    if captured is not None:
                        staged_path, sha256 = captured
                        break

                    current = self._regular_identity(candidate)
                    if current is None:
                        capture_error = "source_unavailable"
                    else:
                        identity = current
                        capture_error = (
                            "empty_file"
                            if identity.size == 0
                            else "file_too_large"
                            if identity.size > self._config.max_file_bytes
                            else "staging_failed"
                        )
                    if capture_attempt < len(self._config.capture_retry_delays_seconds):
                        time.sleep(
                            self._config.capture_retry_delays_seconds[capture_attempt]
                        )

                if staged_path is None:
                    ledger.register_validation_failure(
                        signature=signature,
                        source_root=source_root,
                        source_path=candidate,
                        identity=identity,
                        error_category=capture_error,
                        promote_baseline=False,
                    )
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                registered = ledger.register_staged(
                    signature=signature,
                    source_root=source_root,
                    source_path=candidate,
                    identity=identity,
                    sha256=sha256,
                    staged_path=staged_path,
                    promote_baseline=False,
                )
                if not registered:
                    self._unlink_quietly(staged_path)
                return AttachmentSubmissionResult(
                    AttachmentSubmissionDisposition.ACCEPTED
                )
        except (OSError, RuntimeError, sqlite3.Error):
            if staged_path is not None:
                self._unlink_quietly(staged_path)
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.UNAVAILABLE,
                "durable_ingress_unavailable",
            )
        finally:
            if ledger is not None:
                ledger.close()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._supervisor_thread and self._supervisor_thread.is_alive():
                return
            self._stop_event.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervise,
                name="outbound-attachment-supervisor",
                daemon=True,
            )
            self._supervisor_thread.start()

    def wait_until_active(self, timeout: float | None = None) -> bool:
        return self._active_event.wait(timeout)

    def stop(self, timeout: float | None = 10.0) -> None:
        with self._lifecycle_lock:
            supervisor = self._supervisor_thread
            if supervisor is None:
                return
            self._stop_event.set()
            active_stop = self._active_stop
            if active_stop is not None:
                active_stop.set()
            inotify = self._inotify
            if inotify is not None:
                try:
                    inotify.close()
                except OSError:
                    pass

        supervisor.join(timeout)
        if supervisor.is_alive():
            raise TimeoutError("outbound attachment service did not stop in time")
        with self._lifecycle_lock:
            self._supervisor_thread = None

    def _supervise(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    acquired = self._process_lock.try_acquire()
                except OSError as error:
                    self._last_start_error = error
                    self._stop_event.wait(self._config.lock_retry_seconds)
                    continue
                if not acquired:
                    self._stop_event.wait(self._config.lock_retry_seconds)
                    continue
                try:
                    self._activate()
                    while not self._stop_event.is_set():
                        active_stop = self._active_stop
                        reader = self._reader_thread
                        worker = self._worker_thread
                        if (
                            active_stop is None
                            or active_stop.is_set()
                            or reader is None
                            or worker is None
                            or not reader.is_alive()
                            or not worker.is_alive()
                        ):
                            break
                        self._stop_event.wait(0.1)
                except Exception as error:  # noqa: BLE001
                    self._last_start_error = error
                finally:
                    self._deactivate()
                    self._process_lock.release()
                if not self._stop_event.is_set():
                    self._stop_event.wait(self._config.lock_retry_seconds)
        finally:
            self._deactivate()
            self._process_lock.release()

    def _activate(self) -> None:
        self._ensure_state_directories()
        for source_dir in self._config.watched_source_dirs:
            source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._active_stop = threading.Event()
        self._jobs = queue.Queue()
        self._watch_roots = {}
        self._ledger = _Ledger(self._config.state_dir / "ledger.sqlite3")
        self._ledger.recover_interrupted()
        with _StagingTransactionLock(self._config.state_dir / "staging.lock"):
            self._cleanup_sent_staging()
            self._cleanup_orphaned_staging()

        baseline_state = self._ledger.baseline_state()
        baseline_cutover_ns: int | None = None
        if baseline_state == "new":
            # Persist the boundary before installing watches. If this process
            # dies anywhere after this commit, the next owner can still tell
            # pre-existing files from output created during the failed start.
            baseline_cutover_ns = self._ledger.begin_baseline()
            baseline_state = "started"
        elif baseline_state == "started":
            baseline_cutover_ns = self._ledger.baseline_cutover_ns()
            if baseline_cutover_ns is None:
                # Compatibility for an interrupted ledger written before the
                # durable cutover marker existed. Its original boundary is
                # unknowable, so establish a conservative new one rather than
                # bulk-sending every file already present.
                baseline_cutover_ns = self._ledger.repair_missing_baseline_cutover()

        self._inotify = self._inotify_factory()
        for source_dir in self._config.watched_source_dirs:
            self._add_watch(source_dir)

        if baseline_state == "complete":
            self._schedule_reconciliation()
        else:
            if baseline_cutover_ns is None:
                raise RuntimeError("first baseline has no durable cutover")
            self._baseline_existing(baseline_cutover_ns)

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="outbound-attachment-worker",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="outbound-attachment-inotify",
            daemon=True,
        )
        self._worker_thread.start()
        self._reader_thread.start()
        self._last_start_error = None
        self._active_event.set()

    def _deactivate(self) -> None:
        self._active_event.clear()
        active_stop = self._active_stop
        if active_stop is not None:
            active_stop.set()
        inotify, self._inotify = self._inotify, None
        if inotify is not None:
            try:
                inotify.close()
            except OSError:
                pass
        for thread in (self._reader_thread, self._worker_thread):
            if thread is not None and thread is not threading.current_thread():
                # Never close the ledger or release the singleton lock while a
                # callback is still using them.  ``stop(timeout=...)`` may
                # report a timeout to its caller, but shutdown continues safely
                # and ownership transfers only after both threads have exited.
                thread.join()
        self._reader_thread = None
        self._worker_thread = None
        ledger, self._ledger = self._ledger, None
        if ledger is not None:
            ledger.close()
        self._jobs = None
        self._watch_roots = {}
        self._active_stop = None

    def _reader_loop(self) -> None:
        try:
            while not self._should_stop_active():
                inotify = self._inotify
                if inotify is None:
                    return
                try:
                    events = inotify.read(timeout=self._config.inotify_read_timeout_ms)
                except (OSError, ValueError):
                    if self._should_stop_active():
                        return
                    raise
                for event in events:
                    self._handle_inotify_event(event)
        except Exception as error:  # noqa: BLE001
            self._last_start_error = error
            if self._active_stop is not None:
                self._active_stop.set()

    def _handle_inotify_event(self, event: InotifyEvent) -> None:
        if event.mask & IN_Q_OVERFLOW:
            self._schedule_reconciliation()
            return

        source_root = self._watch_roots.get(event.wd)
        if source_root is None:
            return
        if event.mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED):
            self._repair_watch(event.wd, source_root)
            return
        if not event.name or event.mask & IN_ISDIR:
            return
        if self._should_ignore_name(event.name):
            return
        if event.mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
            self._schedule_candidate(source_root / event.name, promote_baseline=True)

    def _repair_watch(self, old_wd: int, source_root: Path) -> None:
        self._watch_roots.pop(old_wd, None)
        source_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._add_watch(source_root)
        self._schedule_reconciliation()

    def _add_watch(self, source_root: Path) -> None:
        inotify = self._inotify
        if inotify is None:
            raise RuntimeError("inotify is not initialized")
        wd = inotify.add_watch(str(source_root), WATCH_MASK)
        self._watch_roots[wd] = source_root

    def _worker_loop(self) -> None:
        try:
            while not self._should_stop_active():
                jobs = self._jobs
                if jobs is None:
                    return
                try:
                    job = jobs.get(timeout=self._config.worker_poll_seconds)
                except queue.Empty:
                    job = None

                if job is _RESCAN:
                    self._scan_and_schedule()
                elif isinstance(job, _CandidateJob):
                    self._stage_candidate(job)

                self._deliver_all_due()
                self._notify_all_due_failures()
        except Exception as error:  # noqa: BLE001
            self._last_start_error = error
            if self._active_stop is not None:
                self._active_stop.set()

    def _schedule_candidate(self, path: Path, *, promote_baseline: bool) -> None:
        jobs = self._jobs
        if jobs is None:
            return
        jobs.put(_CandidateJob(path=path, promote_baseline=promote_baseline))

    def _schedule_reconciliation(self) -> None:
        jobs = self._jobs
        if jobs is not None:
            jobs.put(_RESCAN)

    def _baseline_existing(self, cutover_ns: int) -> None:
        entries: list[tuple[str, Path, Path, _FileIdentity]] = []
        post_cutover_paths: list[Path] = []
        for source_root, path in self._iter_source_entries():
            identity = self._regular_identity(path)
            if identity is None:
                continue
            if identity.ctime_ns >= cutover_ns:
                post_cutover_paths.append(path)
                continue
            entries.append(
                (
                    self._signature(source_root, path, identity),
                    source_root,
                    path,
                    identity,
                )
            )
        self._require_ledger().finish_baseline(entries)
        # These files appeared after the persisted boundary, possibly before
        # watches were installed or while the initial scan was running. Queue
        # them explicitly so correctness does not depend on the corresponding
        # inotify event surviving an overflow.
        for path in post_cutover_paths:
            self._schedule_candidate(path, promote_baseline=True)

    def _scan_and_schedule(self) -> None:
        for _source_root, path in self._iter_source_entries():
            self._schedule_candidate(path, promote_baseline=False)

    def _iter_source_entries(self) -> Sequence[tuple[Path, Path]]:
        entries: list[tuple[Path, Path]] = []
        for source_root in self._config.watched_source_dirs:
            try:
                entries.extend(
                    (source_root, path)
                    for path in source_root.iterdir()
                    if not self._should_ignore_name(path.name)
                )
            except FileNotFoundError:
                source_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return entries

    def _stage_candidate(self, job: _CandidateJob) -> None:
        path = job.path
        promote_baseline = job.promote_baseline
        source_root = self._source_root_for(path)
        if source_root is None:
            return

        identity = self._wait_for_stable_identity(path)
        if identity is None:
            if self._should_stop_active():
                return
            current = self._regular_identity(path)
            if current is not None:
                self._register_capture_failure(
                    source_root,
                    path,
                    current,
                    "file_unstable",
                    promote_baseline,
                )
            return
        signature = self._signature(source_root, path, identity)
        ledger = self._require_ledger()
        status = ledger.signature_status(signature)
        if status is not None and not (status == "baseline" and promote_baseline):
            return

        validation_error = ""
        if identity.size == 0:
            validation_error = "empty_file"
        elif identity.size > self._config.max_file_bytes:
            validation_error = "file_too_large"
        if validation_error:
            ledger.register_validation_failure(
                signature=signature,
                source_root=source_root,
                source_path=path,
                identity=identity,
                error_category=validation_error,
                promote_baseline=promote_baseline,
            )
            return

        captured: tuple[Path, str] | None = None
        capture_error = "staging_failed"
        capture_delays = self._config.capture_retry_delays_seconds
        for capture_attempt in range(len(capture_delays) + 1):
            try:
                captured = self._capture_to_staging(path, identity)
            except OSError:
                captured = None
            if captured is not None:
                break

            current = self._regular_identity(path)
            if current is None:
                capture_error = "source_unavailable"
            else:
                identity = current
                signature = self._signature(source_root, path, identity)
                capture_error = (
                    "empty_file"
                    if identity.size == 0
                    else "file_too_large"
                    if identity.size > self._config.max_file_bytes
                    else "staging_failed"
                )
            if capture_attempt < len(capture_delays):
                active_stop = self._active_stop
                if active_stop is None or active_stop.wait(
                    capture_delays[capture_attempt]
                ):
                    return

        if captured is None:
            self._register_capture_failure(
                source_root,
                path,
                identity,
                capture_error,
                promote_baseline,
            )
            return
        staged_path, sha256 = captured
        registered = self._require_ledger().register_staged(
            signature=signature,
            source_root=source_root,
            source_path=path,
            identity=identity,
            sha256=sha256,
            staged_path=staged_path,
            promote_baseline=promote_baseline,
        )
        if not registered:
            self._unlink_quietly(staged_path)

    def _register_capture_failure(
        self,
        source_root: Path,
        path: Path,
        identity: _FileIdentity,
        error_category: str,
        promote_baseline: bool,
    ) -> None:
        self._require_ledger().register_validation_failure(
            signature=self._signature(source_root, path, identity),
            source_root=source_root,
            source_path=path,
            identity=identity,
            error_category=error_category,
            promote_baseline=promote_baseline,
        )

    def _wait_for_stable_identity(self, path: Path) -> _FileIdentity | None:
        deadline = time.monotonic() + self._config.readiness_timeout_seconds
        previous: _FileIdentity | None = None
        unchanged = 0
        while not self._should_stop_active() and time.monotonic() < deadline:
            current = self._regular_identity(path)
            if current is None:
                return None
            if current == previous:
                unchanged += 1
                if unchanged >= self._config.stability_checks:
                    return current
            else:
                previous = current
                unchanged = 0
            if self._config.stability_interval_seconds:
                active_stop = self._active_stop
                if active_stop and active_stop.wait(
                    self._config.stability_interval_seconds
                ):
                    return None
        return None

    def _wait_for_explicit_identity(
        self,
        path: Path,
        source_root: Path,
    ) -> tuple[_FileIdentity | None, _FileIdentity | None, str]:
        deadline = time.monotonic() + self._config.readiness_timeout_seconds
        previous: _FileIdentity | None = None
        last_identity: _FileIdentity | None = None
        unchanged = 0
        while time.monotonic() < deadline:
            try:
                root_stat = source_root.lstat()
                if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(
                    root_stat.st_mode
                ):
                    return None, last_identity, "source_root_unavailable"
                if source_root.resolve(strict=True) != source_root:
                    return None, last_identity, "source_root_unavailable"
                file_stat = path.lstat()
            except FileNotFoundError:
                current = None
            except (OSError, RuntimeError, ValueError):
                return None, last_identity, "invalid_path"
            else:
                if stat.S_ISLNK(file_stat.st_mode):
                    return None, last_identity, "symlink_not_allowed"
                if not stat.S_ISREG(file_stat.st_mode):
                    return None, last_identity, "not_regular_file"
                current = self._identity_from_stat(file_stat)

            if current is None:
                previous = None
                unchanged = 0
            elif current == previous:
                last_identity = current
                unchanged += 1
                if unchanged >= self._config.stability_checks:
                    return current, current, ""
            else:
                previous = current
                last_identity = current
                unchanged = 0
            if self._config.stability_interval_seconds:
                time.sleep(self._config.stability_interval_seconds)
        return (
            None,
            last_identity,
            "file_unstable" if last_identity is not None else "source_unavailable",
        )

    def _capture_to_staging(
        self, path: Path, expected: _FileIdentity
    ) -> tuple[Path, str] | None:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(path, flags)
        except FileNotFoundError:
            return None

        try:
            return self._capture_fd_to_staging(source_fd, path, expected)
        finally:
            os.close(source_fd)

    def _capture_explicit_to_staging(
        self,
        source_root: Path,
        path: Path,
        expected: _FileIdentity,
    ) -> tuple[Path, str] | None:
        root_flags = os.O_RDONLY | os.O_CLOEXEC
        root_flags |= getattr(os, "O_DIRECTORY", 0)
        root_flags |= getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(source_root, root_flags)
        source_fd: int | None = None
        try:
            opened_root = Path(f"/proc/self/fd/{root_fd}").resolve(strict=True)
            if opened_root != source_root:
                return None
            source_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
            source_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                source_fd = os.open(path.name, source_flags, dir_fd=root_fd)
            except FileNotFoundError:
                return None
            return self._capture_fd_to_staging(source_fd, path, expected)
        finally:
            if source_fd is not None:
                os.close(source_fd)
            os.close(root_fd)

    def _capture_fd_to_staging(
        self,
        source_fd: int,
        path: Path,
        expected: _FileIdentity,
    ) -> tuple[Path, str] | None:

        suffix = path.suffix[:32]
        staging_dir = self._config.state_dir / "staging"
        final_path = staging_dir / f"{uuid.uuid4().hex}{suffix}"
        temp_path = staging_dir / f".{uuid.uuid4().hex}.tmp"
        temp_fd: int | None = None
        try:
            before_stat = os.fstat(source_fd)
            before = self._identity_from_stat(before_stat)
            if before != expected or not stat.S_ISREG(before_stat.st_mode):
                return None
            if before.size == 0 or before.size > self._config.max_file_bytes:
                return None

            temp_fd = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._config.max_file_bytes:
                    return None
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(temp_fd, view)
                    view = view[written:]

            after = self._identity_from_stat(os.fstat(source_fd))
            if before != after or total != before.size:
                return None
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            os.replace(temp_path, final_path)
            directory_fd = os.open(staging_dir, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return final_path, digest.hexdigest()
        except BaseException:
            self._unlink_quietly(final_path)
            raise
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            self._unlink_quietly(temp_path)

    def _deliver_all_due(self) -> None:
        ledger = self._require_ledger()
        while not self._should_stop_active():
            record = ledger.claim_due()
            if record is None:
                return
            staged_path = record.attachment.staged_path
            has_remote_receipt = bool(record.attachment.web_view_link.strip())
            if (
                staged_path is None or not staged_path.is_file()
            ) and not has_remote_receipt:
                self._handle_delivery_failure(record, "staging_unavailable")
                continue

            try:
                callback_result = self._delivery_callback(record.attachment)
                delivery_result = self._normalize_delivery_result(callback_result)
            except Exception:  # noqa: BLE001
                delivery_result = OutboundDeliveryResult(DeliveryDisposition.FAILED)

            ledger.store_drive_receipt(
                record.record_id,
                delivery_result.drive_file_id,
                delivery_result.web_view_link,
            )
            disposition = delivery_result.disposition

            if disposition is DeliveryDisposition.DELIVERED:
                ledger.mark_delivered(record.record_id)
                if staged_path is not None:
                    self._unlink_quietly(staged_path)
                if staged_path is None or not staged_path.exists():
                    ledger.clear_staged_path(record.record_id)
            elif disposition is DeliveryDisposition.DEFERRED:
                ledger.mark_deferred(
                    record.record_id, self._config.deferral_delay_seconds
                )
            else:
                self._handle_delivery_failure(record, "delivery_failed")

    def _handle_delivery_failure(
        self, record: _DeliveryRecord, error_category: str
    ) -> None:
        next_attempt_number = record.attempts + 1
        retry_delay = self._retry_delay(next_attempt_number)
        self._require_ledger().mark_failed_attempt(
            record.record_id,
            error_category=error_category,
            max_attempts=self._config.max_delivery_attempts,
            retry_delay=retry_delay,
        )

    def _notify_all_due_failures(self) -> None:
        ledger = self._require_ledger()
        while not self._should_stop_active():
            record = ledger.next_failure_notification()
            if record is None:
                return
            failure = FinalDeliveryFailure(
                attachment=record.attachment,
                attempts=record.attempts,
                error_category=record.last_error_category,
            )
            try:
                self._final_failure_callback(failure)
            except Exception:  # noqa: BLE001
                ledger.defer_failure_notification(
                    record.record_id,
                    self._config.notification_retry_delay_seconds,
                )
                continue
            ledger.mark_failure_notified(record.record_id)
            staged_path = record.attachment.staged_path
            if staged_path is not None:
                self._unlink_quietly(staged_path)
                if not staged_path.exists():
                    ledger.clear_staged_path(record.record_id)

    def _retry_delay(self, attempt_number: int) -> float:
        delays = self._config.retry_delays_seconds
        if not delays:
            return 0
        return delays[min(max(attempt_number - 1, 0), len(delays) - 1)]

    def _cleanup_sent_staging(self) -> None:
        ledger = self._require_ledger()
        for record_id, staged_path in ledger.sent_staging_rows():
            self._unlink_quietly(staged_path)
            if not staged_path.exists():
                ledger.clear_staged_path(record_id)

    def _cleanup_orphaned_staging(self) -> None:
        staging_dir = self._config.state_dir / "staging"
        referenced = {
            path.resolve(strict=False)
            for path in self._require_ledger().referenced_staging_paths()
        }
        try:
            entries = list(staging_dir.iterdir())
        except FileNotFoundError:
            return
        for path in entries:
            if path.is_file() and path.resolve(strict=False) not in referenced:
                self._unlink_quietly(path)

    def _source_root_for(self, path: Path) -> Path | None:
        absolute = Path(os.path.abspath(path))
        for source_root in self._config.source_dirs:
            if absolute.parent == source_root:
                return source_root
        return None

    def _ensure_state_directories(self) -> None:
        self._config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._config.state_dir, 0o700)
        staging_dir = self._config.state_dir / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(staging_dir, 0o700)

    @staticmethod
    def _should_ignore_name(name: str) -> bool:
        lowered = name.lower()
        if not name or name.startswith((".", "~$")):
            return True
        if name.endswith("~"):
            return True
        temporary_markers = (
            ".tmp",
            ".part",
            ".partial",
            ".swp",
            ".swx",
            ".crdownload",
        )
        return any(
            lowered.endswith(marker) or f"{marker}." in lowered
            for marker in temporary_markers
        )

    def _regular_identity(self, path: Path) -> _FileIdentity | None:
        if self._source_root_for(path) is None:
            return None
        try:
            file_stat = path.lstat()
        except (FileNotFoundError, OSError, ValueError):
            return None
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            return None
        return self._identity_from_stat(file_stat)

    @staticmethod
    def _identity_from_stat(file_stat: os.stat_result) -> _FileIdentity:
        return _FileIdentity(
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            ctime_ns=file_stat.st_ctime_ns,
        )

    @staticmethod
    def _signature(source_root: Path, path: Path, identity: _FileIdentity) -> str:
        payload = "\0".join(
            (
                str(source_root),
                path.name,
                str(identity.device),
                str(identity.inode),
                str(identity.size),
                str(identity.mtime_ns),
                str(identity.ctime_ns),
            )
        ).encode("utf-8", errors="surrogateescape")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _explicit_signature(idempotency_key: str) -> str:
        return hashlib.sha256(
            f"explicit-media-v1\0{idempotency_key}".encode()
        ).hexdigest()

    @staticmethod
    def _normalize_delivery_result(
        value: OutboundDeliveryResult | DeliveryDisposition | bool,
    ) -> OutboundDeliveryResult:
        if isinstance(value, OutboundDeliveryResult):
            return value
        if isinstance(value, DeliveryDisposition):
            return OutboundDeliveryResult(value)
        if value is True:
            return OutboundDeliveryResult(DeliveryDisposition.DELIVERED)
        if value is False:
            return OutboundDeliveryResult(DeliveryDisposition.FAILED)
        raise TypeError(
            "delivery callback must return OutboundDeliveryResult, "
            "DeliveryDisposition, or bool"
        )

    def _require_ledger(self) -> _Ledger:
        ledger = self._ledger
        if ledger is None:
            raise RuntimeError("outbound attachment ledger is not active")
        return ledger

    def _should_stop_active(self) -> bool:
        active_stop = self._active_stop
        return self._stop_event.is_set() or active_stop is None or active_stop.is_set()

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "DEFAULT_SOURCE_DIRS",
    "AttachmentSubmissionDisposition",
    "AttachmentSubmissionResult",
    "DeliveryDisposition",
    "FinalDeliveryFailure",
    "OutboundAttachment",
    "OutboundAttachmentConfig",
    "OutboundAttachmentService",
    "OutboundDeliveryResult",
]
