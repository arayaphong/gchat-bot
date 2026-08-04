from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path


class ProcessingGateError(RuntimeError):
    """Raised when the shared processing lease cannot be inspected safely."""


class ProcessingLease:
    """One exclusive lease returned by :class:`ProcessingGate`."""

    def __init__(self, gate: ProcessingGate, descriptor: int | None) -> None:
        self._gate = gate
        self._descriptor = descriptor
        self._release_lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            descriptor, self._descriptor = self._descriptor, None
            self._gate._release(descriptor)

    def __enter__(self) -> object:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


class ProcessingGate:
    """Non-blocking exclusive gate for provider and outbound Chat work.

    The local lock preserves the existing single-process busy behavior.  When
    ``lock_file`` is supplied, ``flock`` extends the same lease across WSGI
    workers without introducing a check-then-act window.
    """

    def __init__(self, lock_file: Path | None = None) -> None:
        self._lock_file = lock_file.expanduser() if lock_file is not None else None
        self._local_lock = threading.Lock()

    @property
    def local_lock(self) -> threading.Lock:
        return self._local_lock

    @property
    def is_locked(self) -> bool:
        return self._local_lock.locked()

    def try_acquire(self) -> ProcessingLease | None:
        if not self._local_lock.acquire(blocking=False):
            return None

        descriptor: int | None = None
        try:
            if self._lock_file is not None:
                self._lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(
                    self._lock_file,
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(descriptor)
                    descriptor = None
                    self._local_lock.release()
                    return None
            return ProcessingLease(self, descriptor)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            self._local_lock.release()
            raise ProcessingGateError(
                f"cannot acquire shared processing gate: {type(error).__name__}"
            ) from error

    def _release(self, descriptor: int | None) -> None:
        try:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            self._local_lock.release()


__all__ = ["ProcessingGate", "ProcessingGateError", "ProcessingLease"]
