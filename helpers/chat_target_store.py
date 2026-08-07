from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPACE_NAME_RE = re.compile(r"^spaces/[^/]+$")
THREAD_NAME_RE = re.compile(r"^(spaces/[^/]+)/threads/[^/]+$")


class ChatTargetError(RuntimeError):
    """Base error for the fixed outbound Chat destination."""


class ChatTargetConflictError(ChatTargetError):
    def __init__(self, existing: ChatTarget, observed: ChatTarget) -> None:
        self.existing = existing
        self.observed = observed
        super().__init__(
            "outbound Chat target is fixed to space "
            f"{existing.space!r}; refusing {observed.space!r}"
        )


class ChatTargetStateError(ChatTargetError):
    """The persisted target exists but cannot be trusted."""


@dataclass(frozen=True)
class ChatTarget:
    space: str
    thread: str

    @staticmethod
    def from_names(space: str, thread: str) -> ChatTarget:
        if not isinstance(space, str) or not isinstance(thread, str):
            raise TypeError("Chat space and thread must be strings")
        normalized_space = space.strip()
        normalized_thread = thread.strip()
        thread_match = THREAD_NAME_RE.fullmatch(normalized_thread)
        if not normalized_space and thread_match:
            normalized_space = thread_match.group(1)
        if not normalized_space or not normalized_thread:
            raise ValueError("both Chat space and thread are required")
        if not SPACE_NAME_RE.fullmatch(normalized_space) or not thread_match:
            raise ValueError("invalid Google Chat space or thread resource name")
        if thread_match.group(1) != normalized_space:
            raise ValueError("Chat thread does not belong to the configured space")
        return ChatTarget(space=normalized_space, thread=normalized_thread)


class FixedChatTargetStore:
    """
    Persists the first authenticated Chat thread as the outbound destination.

    A fully configured target takes precedence over the state file.  This gives
    deployments a deterministic override while retaining zero-configuration
    learning. Incoming messages from other threads in the same space are
    accepted without changing the fixed outbound thread.
    """

    def __init__(
        self,
        state_file: Path,
        configured_space: str = "",
        configured_thread: str = "",
    ) -> None:
        if bool(configured_space.strip()) != bool(configured_thread.strip()):
            raise ValueError(
                "GCHAT_OUTBOUND_SPACE and GCHAT_OUTBOUND_THREAD must be set together"
            )
        self._state_file = state_file.expanduser()
        self._configured = (
            ChatTarget.from_names(configured_space, configured_thread)
            if configured_space.strip()
            else None
        )
        self._cached: ChatTarget | None = self._configured
        self._lock = threading.RLock()

    @property
    def state_file(self) -> Path:
        return self._state_file

    def get(self) -> ChatTarget | None:
        with self._lock:
            return self._get_locked()

    def _get_locked(self) -> ChatTarget | None:
        if self._cached is not None:
            return self._cached
        try:
            raw = self._state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ChatTargetStateError(
                f"cannot read outbound Chat target: {type(error).__name__}"
            ) from error

        try:
            payload: Any = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("target payload is not an object")
            persisted_space = payload.get("space")
            persisted_thread = payload.get("thread")
            if not isinstance(persisted_space, str) or not isinstance(
                persisted_thread, str
            ):
                raise TypeError("persisted target names are not strings")
            target = ChatTarget.from_names(
                persisted_space,
                persisted_thread,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ChatTargetStateError(
                "persisted outbound Chat target is invalid"
            ) from error

        self._cached = target
        return target

    def remember(self, space: str, thread: str) -> ChatTarget:
        observed = ChatTarget.from_names(space, thread)
        with self._lock:
            if self._configured is not None:
                if self._configured.space != observed.space:
                    raise ChatTargetConflictError(self._configured, observed)
                return self._configured

            with self._interprocess_lock():
                # Another WSGI worker may have fixed the destination since this
                # instance last looked.  Re-read while holding the process lock.
                cached_before_lock = self._cached
                self._cached = None
                existing = self._get_locked() or cached_before_lock
                if existing is not None:
                    self._cached = existing
                    if existing.space != observed.space:
                        raise ChatTargetConflictError(existing, observed)
                    return existing

                self._write_atomic(observed)
                self._cached = observed
                return observed

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        parent = self._state_file.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = parent / f".{self._state_file.name}.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise ChatTargetStateError(
                f"cannot lock outbound Chat target: {type(error).__name__}"
            ) from error

        try:
            lock_file = os.fdopen(descriptor, "rb", closefd=True)
        except OSError as error:
            os.close(descriptor)
            raise ChatTargetStateError(
                f"cannot lock outbound Chat target: {type(error).__name__}"
            ) from error

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            lock_file.close()
            raise ChatTargetStateError(
                f"cannot lock outbound Chat target: {type(error).__name__}"
            ) from error

        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _write_atomic(self, target: ChatTarget) -> None:
        parent = self._state_file.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = parent / f".{self._state_file.name}.{uuid.uuid4().hex}.tmp"
            encoded = json.dumps(
                {"space": target.space, "thread": target.thread},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(fd, "wb") as file_handle:
                    file_handle.write(encoded)
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                temporary.replace(self._state_file)
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                with suppress(FileNotFoundError):
                    temporary.unlink()
                raise
        except OSError as error:
            raise ChatTargetStateError(
                f"cannot persist outbound Chat target: {type(error).__name__}"
            ) from error
