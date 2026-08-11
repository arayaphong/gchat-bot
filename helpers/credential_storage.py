"""Small primitives for securely coordinating credential-file access."""

from __future__ import annotations

import fcntl
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = path.resolve(strict=False)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def credential_lock_path(path: Path | str) -> Path:
    target = Path(path)
    return target.with_name(f".{target.name}.lock")


@contextmanager
def credential_file_lock(path: Path | str) -> Iterator[None]:
    """Serialize access to a credential across threads and local processes."""

    target = Path(path)
    lock_path = credential_lock_path(target)
    thread_lock = _thread_lock_for(lock_path)

    with thread_lock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        locked = False
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def atomic_write_secret(path: Path | str, content: str) -> None:
    """Atomically replace a UTF-8 secret file with owner-only permissions."""

    target = Path(path)
    directory = target.parent
    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600, follow_symlinks=False)

        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def locked_atomic_write_secret(path: Path | str, content: str) -> None:
    with credential_file_lock(path):
        atomic_write_secret(path, content)
