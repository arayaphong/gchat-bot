"""Sanitized operational checks and SQLite-safe history backups."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from helpers.chat_clear_store import (
    CHAT_HISTORY_SCHEMA_VERSION,
    ChatClearStore,
    ChatClearStoreError,
)
from helpers.chat_history_client import ChatHistoryClient, ChatHistoryClientError
from helpers.chat_history_settings import ChatHistorySettings
from helpers.services import CredentialReadinessError, CredentialService

_UTC = timezone.utc


class HistoryWorkerStatus(Protocol):
    owner_id: str

    @property
    def is_active(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class HistoryPreflightResult:
    ok: bool
    enabled: bool
    delete_enabled: bool
    checks: tuple[str, ...]
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "enabled": self.enabled,
            "delete_enabled": self.delete_enabled,
            "checks": list(self.checks),
        }
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        return payload


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, CredentialReadinessError):
        return f"credential_{error.code}"
    if isinstance(error, ChatHistoryClientError):
        return f"chat_{error.code.value}"
    if isinstance(error, ChatClearStoreError):
        return "state_unavailable"
    return "preflight_failure"


def run_history_preflight(
    settings: ChatHistorySettings | None,
    *,
    configuration_error: BaseException | None,
    store: ChatClearStore,
    credential_service: CredentialService,
    client: ChatHistoryClient | None,
    remote: bool,
) -> HistoryPreflightResult:
    """Check configuration/state and optionally credentials plus DM access.

    Results intentionally contain categories only: never resource names, paths,
    OAuth responses, exception text, or token material.
    """

    if configuration_error is not None or settings is None:
        return HistoryPreflightResult(
            ok=False,
            enabled=False,
            delete_enabled=False,
            checks=(),
            error_code="configuration_invalid",
        )
    if not settings.enabled:
        return HistoryPreflightResult(
            ok=True,
            enabled=False,
            delete_enabled=False,
            checks=("configuration",),
        )

    checks = ["configuration"]
    try:
        store.preflight(delete_execution=settings.delete_enabled)
        checks.extend(("timezone", "state", "schema"))
        if remote:
            if client is None:
                return HistoryPreflightResult(
                    ok=False,
                    enabled=True,
                    delete_enabled=settings.delete_enabled,
                    checks=tuple(checks),
                    error_code="client_unavailable",
                )
            credential_service.get_user_creds()
            checks.append("user_scopes")
            client.preflight_access()
            checks.append("allowed_dm")
            if settings.delete_enabled:
                credential_service.get_bot_creds()
                checks.append("bot_scope")
    except (ChatClearStoreError, ChatHistoryClientError, CredentialReadinessError) as error:
        return HistoryPreflightResult(
            ok=False,
            enabled=True,
            delete_enabled=settings.delete_enabled,
            checks=tuple(checks),
            error_code=_safe_error_code(error),
        )
    except Exception:  # noqa: BLE001
        return HistoryPreflightResult(
            ok=False,
            enabled=True,
            delete_enabled=settings.delete_enabled,
            checks=tuple(checks),
            error_code="preflight_failure",
        )

    return HistoryPreflightResult(
        ok=True,
        enabled=True,
        delete_enabled=settings.delete_enabled,
        checks=tuple(checks),
    )


def worker_lease_ready(
    store: ChatClearStore,
    worker: HistoryWorkerStatus | None,
    *,
    now: datetime | None = None,
    max_heartbeat_age_seconds: float = 20.0,
) -> bool:
    if worker is None or not worker.is_active or max_heartbeat_age_seconds <= 0:
        return False
    try:
        diagnostics = store.diagnostics(now=now)
        if (
            diagnostics.worker_owner != worker.owner_id
            or diagnostics.worker_heartbeat_at is None
        ):
            return False
        heartbeat = datetime.fromisoformat(
            diagnostics.worker_heartbeat_at.replace("Z", "+00:00")
        )
        current = now if now is not None else datetime.now(tz=_UTC)
        return 0 <= (current - heartbeat).total_seconds() <= max_heartbeat_age_seconds
    except (ChatClearStoreError, OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class HistoryBackupResult:
    path: Path
    checkpoint_busy: int
    checkpoint_log_frames: int
    checkpointed_frames: int


def backup_history_store(
    store: ChatClearStore,
    destination: Path | str,
) -> HistoryBackupResult:
    """Create a consistent online SQLite backup without copying a live WAL."""

    store.preflight()
    raw_target = Path(destination).expanduser()
    if raw_target.is_symlink():
        raise ValueError("backup destination must be a new regular file")
    target = raw_target.resolve(strict=False)
    source = store.database_path.resolve(strict=True)
    if target == source or target.exists() or target.is_symlink():
        raise ValueError("backup destination must be a new regular file")
    parent_existed = target.parent.exists()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(target.parent, 0o700)
    checkpoint = store.checkpoint("PASSIVE")

    source_connection = sqlite3.connect(source)
    target_connection: sqlite3.Connection | None = None
    try:
        target_connection = sqlite3.connect(target)
        source_connection.backup(target_connection)
        integrity = target_connection.execute("PRAGMA integrity_check").fetchone()
        version = int(target_connection.execute("PRAGMA user_version").fetchone()[0])
        if integrity is None or integrity[0] != "ok" or version != CHAT_HISTORY_SCHEMA_VERSION:
            raise RuntimeError("backup verification failed")
        target_connection.commit()
    except BaseException:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if target_connection is not None:
            target_connection.close()
        source_connection.close()
    os.chmod(target, 0o600, follow_symlinks=False)
    mode = stat.S_IMODE(target.stat(follow_symlinks=False).st_mode)
    if mode != 0o600:
        raise RuntimeError("backup permission verification failed")
    return HistoryBackupResult(target, *checkpoint)


@dataclass(frozen=True, slots=True)
class HistoryRestoreResult:
    safety_backup_path: Path


def restore_history_store(
    store: ChatClearStore,
    backup_path: Path | str,
    *,
    service_stopped: bool,
) -> HistoryRestoreResult:
    """Restore through SQLite while exclusively holding the worker lease.

    The explicit flag is an operator acknowledgement, not service discovery;
    the runbook requires stopping the unit first.  A verified safety backup is
    created before any page in the live database can be replaced.
    """

    if not service_stopped:
        raise ValueError("service-stopped acknowledgement is required")
    raw_source = Path(backup_path).expanduser()
    if raw_source.is_symlink():
        raise ValueError("restore source must be a regular non-symlink file")
    source_path = raw_source.resolve(strict=True)
    if source_path == store.database_path.resolve(strict=False):
        raise ValueError("restore source must differ from the live database")
    source_stat = source_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("restore source must be a regular non-symlink file")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_descriptor = os.open(store.worker_lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("history worker lease is still held") from None
        os.fchmod(lock_descriptor, 0o600)
        store.preflight()
        timestamp = datetime.now(tz=_UTC).strftime("%Y%m%dT%H%M%SZ")
        safety_path = store.state_dir / f"history.pre-restore-{timestamp}.sqlite3"
        backup_history_store(store, safety_path)

        source_connection = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
        target_connection: sqlite3.Connection | None = None
        try:
            integrity = source_connection.execute("PRAGMA integrity_check").fetchone()
            version = int(
                source_connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if (
                integrity is None
                or integrity[0] != "ok"
                or version != CHAT_HISTORY_SCHEMA_VERSION
            ):
                raise ValueError("restore source is incompatible")
            target_connection = sqlite3.connect(store.database_path)
            checkpoint = target_connection.execute(
                "PRAGMA wal_checkpoint(PASSIVE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise RuntimeError("live database checkpoint is busy")
            source_connection.backup(target_connection)
            target_connection.commit()
            restored = target_connection.execute("PRAGMA integrity_check").fetchone()
            if restored is None or restored[0] != "ok":
                raise RuntimeError("restored database verification failed")
        finally:
            if target_connection is not None:
                target_connection.close()
            source_connection.close()
        store.preflight()
        return HistoryRestoreResult(safety_path)
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


__all__ = [
    "HistoryBackupResult",
    "HistoryPreflightResult",
    "HistoryRestoreResult",
    "backup_history_store",
    "restore_history_store",
    "run_history_preflight",
    "worker_lease_ready",
]
