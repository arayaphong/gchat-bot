from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

CHAT_WRITE_MIN_INTERVAL_SECONDS = 1.1
CHAT_HISTORY_DATABASE_NAME = "history.sqlite3"

_SPACE_NAME_RE = re.compile(r"^spaces/[^\s/\x00-\x1f\x7f]+$")


class ChatWritePacerError(RuntimeError):
    """Safe failure raised when a durable write reservation is unavailable."""


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
        self.state_dir = Path(state_dir).expanduser()
        self.database_path = self.state_dir / CHAT_HISTORY_DATABASE_NAME
        self.min_interval_seconds = float(min_interval_seconds)
        self._clock = clock
        self._sleeper = sleeper
        self._initialization_lock = threading.Lock()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            try:
                if self.state_dir.exists():
                    mode = self.state_dir.lstat().st_mode
                    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                        raise ChatWritePacerError("history state directory is invalid")
                    self.state_dir.chmod(0o700)
                else:
                    self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                    mode = self.state_dir.lstat().st_mode
                    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                        raise ChatWritePacerError("history state directory is invalid")
                    self.state_dir.chmod(0o700)

                connection = sqlite3.connect(self.database_path, timeout=5)
                try:
                    connection.execute("PRAGMA busy_timeout = 5000")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS chat_write_pacer (
                            space_name TEXT PRIMARY KEY,
                            next_write_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        )
                        """
                    )
                    connection.commit()
                finally:
                    connection.close()
                os.chmod(self.database_path, 0o600)
            except ChatWritePacerError:
                raise
            except (OSError, sqlite3.Error, ValueError):
                raise ChatWritePacerError(
                    "history write pacing state is unavailable"
                ) from None
            self._initialized = True

    def reserve(self, space_name: str) -> float:
        """Atomically reserve and return a wall-clock slot without sleeping."""

        if (
            not isinstance(space_name, str)
            or _SPACE_NAME_RE.fullmatch(space_name) is None
        ):
            raise ChatWritePacerError("invalid Chat space resource")
        self._ensure_initialized()
        now = float(self._clock())
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=5,
                isolation_level=None,
            )
            try:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT next_write_at FROM chat_write_pacer WHERE space_name = ?",
                    (space_name,),
                ).fetchone()
                previous = float(row[0]) if row is not None else now
                slot = max(now, previous)
                next_write_at = slot + self.min_interval_seconds
                connection.execute(
                    """
                    INSERT INTO chat_write_pacer(space_name, next_write_at, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(space_name) DO UPDATE SET
                        next_write_at = excluded.next_write_at,
                        updated_at = excluded.updated_at
                    """,
                    (space_name, next_write_at, now),
                )
                connection.execute("COMMIT")
                return slot
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        except ChatWritePacerError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise ChatWritePacerError(
                "history write pacing reservation failed"
            ) from None

    def wait_for_turn(self, space_name: str) -> float:
        """Reserve a slot, release the DB transaction, then wait if necessary."""

        slot = self.reserve(space_name)
        delay = max(0.0, slot - float(self._clock()))
        if delay:
            self._sleeper(delay)
        return slot


__all__ = [
    "CHAT_HISTORY_DATABASE_NAME",
    "CHAT_WRITE_MIN_INTERVAL_SECONDS",
    "ChatWritePacer",
    "ChatWritePacerError",
]
