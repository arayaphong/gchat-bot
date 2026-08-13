from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from .models import (
    _DELIVERY_ID_NAMESPACE,
    OutboundAttachment,
    _DeliveryRecord,
    _FileIdentity,
    _ThreadUploadRoute,
)

_LEDGER_SCHEMA_VERSION = 3


class _Ledger:
    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        schema_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if schema_version > _LEDGER_SCHEMA_VERSION:
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
                    destination_space TEXT NOT NULL DEFAULT '',
                    destination_thread TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_upload_routes (
                    directory_name TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL UNIQUE,
                    destination_space TEXT NOT NULL,
                    destination_thread TEXT NOT NULL,
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
                "destination_space": "TEXT NOT NULL DEFAULT ''",
                "destination_thread": "TEXT NOT NULL DEFAULT ''",
            }
            for column, declaration in optional_columns.items():
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}"
                    )
            self._connection.execute(
                f"PRAGMA user_version = {_LEDGER_SCHEMA_VERSION}"
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS artifacts_due
                ON artifacts(status, next_attempt_at, id)
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def register_thread_upload_route(
        self,
        route: _ThreadUploadRoute,
        baseline_entries: Sequence[tuple[str, Path, Path, _FileIdentity]],
    ) -> bool:
        """Persist one immutable folder-to-thread binding and its baseline."""

        now = time.time()
        with self._lock, self._connection:
            directory_row = self._connection.execute(
                """
                SELECT * FROM thread_upload_routes WHERE directory_name = ?
                """,
                (route.directory_name,),
            ).fetchone()
            session_row = self._connection.execute(
                """
                SELECT * FROM thread_upload_routes WHERE session_key = ?
                """,
                (route.session_key,),
            ).fetchone()
            existing = directory_row or session_row
            if existing is not None:
                expected = (
                    route.directory_name,
                    route.session_key,
                    route.destination_space,
                    route.destination_thread,
                )
                actual = (
                    str(existing["directory_name"]),
                    str(existing["session_key"]),
                    str(existing["destination_space"]),
                    str(existing["destination_thread"]),
                )
                if actual != expected or (
                    directory_row is not None
                    and session_row is not None
                    and directory_row["directory_name"]
                    != session_row["directory_name"]
                ):
                    raise ValueError("thread upload route conflicts with stored state")
                return False

            self._connection.execute(
                """
                INSERT INTO thread_upload_routes(
                    directory_name, session_key, destination_space,
                    destination_thread, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    route.directory_name,
                    route.session_key,
                    route.destination_space,
                    route.destination_thread,
                    now,
                    now,
                ),
            )
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO artifacts(
                    signature, source_root, source_path, display_name,
                    device, inode, size, mtime_ns, status, created_at, updated_at,
                    destination_space, destination_thread
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'baseline', ?, ?, ?, ?)
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
                        route.destination_space,
                        route.destination_thread,
                    )
                    for signature, source_root, source_path, identity
                    in baseline_entries
                ],
            )
        return True

    def has_thread_upload_route(self, route: _ThreadUploadRoute) -> bool:
        with self._lock:
            directory_row = self._connection.execute(
                """
                SELECT * FROM thread_upload_routes WHERE directory_name = ?
                """,
                (route.directory_name,),
            ).fetchone()
            session_row = self._connection.execute(
                """
                SELECT * FROM thread_upload_routes WHERE session_key = ?
                """,
                (route.session_key,),
            ).fetchone()
        existing = directory_row or session_row
        if existing is None:
            return False
        expected = (
            route.directory_name,
            route.session_key,
            route.destination_space,
            route.destination_thread,
        )
        actual = (
            str(existing["directory_name"]),
            str(existing["session_key"]),
            str(existing["destination_space"]),
            str(existing["destination_thread"]),
        )
        if actual != expected or (
            directory_row is not None
            and session_row is not None
            and directory_row["directory_name"] != session_row["directory_name"]
        ):
            raise ValueError("thread upload route conflicts with stored state")
        return True

    def thread_upload_routes(self) -> list[tuple[str, str, str, str]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT directory_name, session_key, destination_space,
                       destination_thread
                FROM thread_upload_routes
                ORDER BY directory_name
                """
            ).fetchall()
        return [
            (
                str(row["directory_name"]),
                str(row["session_key"]),
                str(row["destination_space"]),
                str(row["destination_thread"]),
            )
            for row in rows
        ]

    def has_thread_upload_directory(self, directory_name: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM thread_upload_routes WHERE directory_name = ?
                """,
                (directory_name,),
            ).fetchone()
        return row is not None

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
        destination_space: str = "",
        destination_thread: str = "",
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
                        destination_space = ?, destination_thread = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        sha256,
                        str(staged_path),
                        destination_space,
                        destination_thread,
                        now,
                        row["id"],
                    ),
                )
                return True

            self._connection.execute(
                """
                INSERT INTO artifacts(
                    signature, source_root, source_path, display_name,
                    device, inode, size, mtime_ns, sha256, staged_path,
                    destination_space, destination_thread,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
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
                    destination_space,
                    destination_thread,
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
        destination_space: str = "",
        destination_thread: str = "",
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
                        next_notification_at = ?, destination_space = ?,
                        destination_thread = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        error_category,
                        now,
                        destination_space,
                        destination_thread,
                        now,
                        row["id"],
                    ),
                )
                return True

            self._connection.execute(
                """
                INSERT INTO artifacts(
                    signature, source_root, source_path, display_name,
                    device, inode, size, mtime_ns, status, attempts,
                    last_error_category, next_notification_at,
                    destination_space, destination_thread,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', 0, ?, ?, ?, ?, ?, ?)
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
                    destination_space,
                    destination_thread,
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
        with self._lock:
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
                destination_space=str(row["destination_space"]),
                destination_thread=str(row["destination_thread"]),
            ),
            attempts=int(row["attempts"]),
            last_error_category=str(row["last_error_category"]),
        )
