from __future__ import annotations

import fcntl
import os
import stat
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers.chat_clear_store import (
    ChatClearStore,
    ChatClearStoreError,
    ChatClearStoreUnavailableError,
    ClearJob,
)

_UTC = timezone.utc


class _SingletonWorkerLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def try_acquire(self) -> bool:
        if self._descriptor is not None:
            return True
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("worker lock is not regular")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except OSError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise ChatClearStoreUnavailableError("history worker lock failed") from None
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ChatHistoryWorkerSupervisor:
    """Singleton polling skeleton; deliberately contains no delete executor."""

    def __init__(
        self,
        store: ChatClearStore,
        *,
        preview_handler: Callable[[ClearJob], None] | None = None,
        confirmation_cleanup_handler: Callable[[ClearJob], None] | None = None,
        poll_interval_seconds: float = 5.0,
        lock_retry_seconds: float = 5.0,
        maintenance_interval_seconds: float = 3600.0,
        clock: Callable[[], datetime] | None = None,
        owner_id: str | None = None,
    ) -> None:
        if (
            poll_interval_seconds <= 0
            or lock_retry_seconds <= 0
            or maintenance_interval_seconds <= 0
        ):
            raise ValueError("worker intervals must be positive")
        self._store = store
        self._preview_handler = preview_handler
        self._confirmation_cleanup_handler = confirmation_cleanup_handler
        self._poll_interval_seconds = poll_interval_seconds
        self._lock_retry_seconds = lock_retry_seconds
        self._maintenance_interval_seconds = maintenance_interval_seconds
        self._clock = clock or (lambda: datetime.now(tz=_UTC))
        self.owner_id = owner_id or f"worker-{uuid.uuid4().hex}"
        self._process_lock = _SingletonWorkerLock(store.worker_lock_path)
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._active_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_maintenance: datetime | None = None
        self.last_error: ChatClearStoreError | None = None

    @property
    def is_active(self) -> bool:
        return self._active_event.is_set()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._supervise,
                name="jinx-chat-history-supervisor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = 10.0) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            self._wake_event.set()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("history worker did not stop in time")
        with self._lifecycle_lock:
            self._thread = None

    def wake(self) -> None:
        self._wake_event.set()

    def wait_until_active(self, timeout: float | None = None) -> bool:
        return self._active_event.wait(timeout)

    def _supervise(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self._store.preflight()
                    acquired = self._process_lock.try_acquire()
                except ChatClearStoreError as error:
                    self.last_error = error
                    self._stop_event.wait(self._lock_retry_seconds)
                    continue
                if not acquired:
                    self._stop_event.wait(self._lock_retry_seconds)
                    continue
                try:
                    self._activate()
                except ChatClearStoreError as error:
                    self.last_error = error
                finally:
                    self._active_event.clear()
                    try:
                        self._store.clear_worker_heartbeat(self.owner_id)
                    except ChatClearStoreError:
                        pass
                    self._process_lock.release()
                if not self._stop_event.is_set():
                    self._stop_event.wait(self._lock_retry_seconds)
        finally:
            self._active_event.clear()
            self._process_lock.release()

    def _activate(self) -> None:
        now = self._clock()
        self._store.recover_interrupted(now=now)
        self._last_maintenance = None
        self.last_error = None
        self._active_event.set()
        # Startup poll is mandatory; wakeups are only latency optimizations.
        self.poll_once(now=now)
        while not self._stop_event.is_set():
            self._wake_event.wait(self._poll_interval_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            self.poll_once(now=self._clock())

    def poll_once(self, *, now: datetime | None = None) -> None:
        current = now if now is not None else self._clock()
        self._store.record_worker_heartbeat(self.owner_id, now=current)
        self._store.expire_pending(now=current)

        if self._confirmation_cleanup_handler is not None:
            while not self._stop_event.is_set():
                cleanup_job = self._store.claim_confirmation_cleanup(now=current)
                if cleanup_job is None:
                    break
                try:
                    self._confirmation_cleanup_handler(cleanup_job)
                except Exception:  # noqa: BLE001
                    self._store.record_final_notification_failure(
                        cleanup_job.operation_id,
                        safe_error_category="confirmation_cleanup_failure",
                        retry_at=current + timedelta(seconds=30),
                    )
                    break

        if self._preview_handler is not None:
            while not self._stop_event.is_set():
                job = self._store.claim_preview(now=current)
                if job is None:
                    break
                try:
                    # No transaction is held while the injected handler runs.
                    self._preview_handler(job)
                except Exception:  # noqa: BLE001
                    retried = self._store.mark_preview_retry(
                        job.operation_id,
                        next_attempt_at=current + timedelta(seconds=5),
                        safe_error_category="preview_handler_failure",
                    )
                    # A frozen PREPARING snapshot is reclaimed on a later poll;
                    # do not spin on it inside this transaction-free loop.
                    if not retried:
                        break

        if (
            self._last_maintenance is None
            or (current - self._last_maintenance).total_seconds()
            >= self._maintenance_interval_seconds
        ):
            self._store.maintain(now=current)
            self._last_maintenance = current


__all__ = ["ChatHistoryWorkerSupervisor"]
