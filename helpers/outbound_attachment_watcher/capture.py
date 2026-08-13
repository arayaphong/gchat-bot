from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .config import OutboundAttachmentConfig
from .models import _FileIdentity


class _StabilityTracker:
    """Shared "N identical readings in a row" bookkeeping used by both the
    auto-watch and explicit-submission polling loops, which otherwise differ
    in how they fetch each reading and how they wait between iterations.
    """

    def __init__(self, required_unchanged: int) -> None:
        self._required_unchanged = required_unchanged
        self._previous: _FileIdentity | None = None
        self._unchanged = 0
        self.last_identity: _FileIdentity | None = None

    def observe(self, current: _FileIdentity) -> _FileIdentity | None:
        """Record a reading; return it once it has repeated enough times."""
        if current == self._previous:
            self.last_identity = current
            self._unchanged += 1
            if self._unchanged >= self._required_unchanged:
                return current
        else:
            self._previous = current
            self.last_identity = current
            self._unchanged = 0
        return None

    def reset(self) -> None:
        self._previous = None
        self._unchanged = 0


def _unlink_quietly(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def identity_from_stat(file_stat: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        ctime_ns=file_stat.st_ctime_ns,
    )


def regular_identity_unrestricted(path: Path) -> _FileIdentity | None:
    try:
        file_stat = path.lstat()
    except (OSError, ValueError):
        return None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        return None
    return identity_from_stat(file_stat)


def ensure_real_directory(path: Path, *, label: str) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(f"{label} is unavailable") from error
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
        directory_stat.st_mode
    ):
        raise RuntimeError(f"{label} must be a real directory")
    if resolved != path:
        raise RuntimeError(f"{label} cannot traverse a symlink")
    path.chmod(0o700)


def _open_relative_nofollow(
    root_fd: int, relative_path: Path, final_flags: int
) -> int:
    """Open ``relative_path`` under ``root_fd``, rejecting a symlink at
    any component - including intermediate directories.

    ``O_NOFOLLOW`` alone only guards the final path component, so a
    single ``os.open(multi/component/path, dir_fd=root_fd)`` can still
    follow a symlink swapped into an intermediate directory between the
    one-time containment check and this open. Walking one component at
    a time with ``O_NOFOLLOW | O_DIRECTORY`` closes that window.
    """
    parts = relative_path.parts
    current_fd = root_fd
    owned_fd: int | None = None
    try:
        for part in parts[:-1]:
            dir_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
            dir_flags |= getattr(os, "O_NOFOLLOW", 0)
            next_fd = os.open(part, dir_flags, dir_fd=current_fd)
            if owned_fd is not None:
                os.close(owned_fd)
            owned_fd = next_fd
            current_fd = next_fd
        return os.open(parts[-1], final_flags, dir_fd=current_fd)
    finally:
        if owned_fd is not None:
            os.close(owned_fd)


class StagingCapture:
    """Symlink-safe, integrity-checked copy of one source file into staging.

    ``should_stop``/``wait`` mirror the owning service's activation lifecycle
    (``_should_stop_active`` and an interruptible ``threading.Event.wait``)
    without this class needing to know about that lifecycle directly.
    """

    def __init__(
        self,
        config: OutboundAttachmentConfig,
        should_stop: Callable[[], bool],
        wait: Callable[[float], bool],
    ) -> None:
        self._config = config
        self._should_stop = should_stop
        self._wait = wait

    def source_root_for(self, path: Path) -> Path | None:
        absolute = Path(os.path.abspath(path))
        matches = [
            source_root
            for source_root in self._config.source_dirs
            if absolute.parent.is_relative_to(source_root)
        ]
        if not matches:
            return None
        return max(matches, key=lambda root: len(root.parts))

    def regular_identity(self, path: Path) -> _FileIdentity | None:
        if self.source_root_for(path) is None:
            return None
        return regular_identity_unrestricted(path)

    def wait_for_stable_identity(self, path: Path) -> _FileIdentity | None:
        deadline = time.monotonic() + self._config.readiness_timeout_seconds
        tracker = _StabilityTracker(self._config.stability_checks)
        while not self._should_stop() and time.monotonic() < deadline:
            current = self.regular_identity(path)
            if current is None:
                return None
            stable = tracker.observe(current)
            if stable is not None:
                return stable
            if self._config.stability_interval_seconds and self._wait(
                self._config.stability_interval_seconds
            ):
                return None
        return None

    def wait_for_explicit_identity(
        self,
        path: Path,
        source_root: Path,
    ) -> tuple[_FileIdentity | None, _FileIdentity | None, str]:
        # source_root is loop-invariant for the duration of this wait (it is
        # never mutated after being resolved by the caller), so it only
        # needs validating once up front rather than on every iteration -
        # the authoritative re-check against symlink swaps happens later,
        # at capture time, via the O_NOFOLLOW dir_fd walk.
        try:
            root_stat = source_root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                return None, None, "source_root_unavailable"
            if source_root.resolve(strict=True) != source_root:
                return None, None, "source_root_unavailable"
        except (OSError, RuntimeError, ValueError):
            return None, None, "source_root_unavailable"

        deadline = time.monotonic() + self._config.readiness_timeout_seconds
        tracker = _StabilityTracker(self._config.stability_checks)
        while time.monotonic() < deadline:
            try:
                file_stat = path.lstat()
            except FileNotFoundError:
                current = None
            except (OSError, RuntimeError, ValueError):
                return None, tracker.last_identity, "invalid_path"
            else:
                if stat.S_ISLNK(file_stat.st_mode):
                    return None, tracker.last_identity, "symlink_not_allowed"
                if not stat.S_ISREG(file_stat.st_mode):
                    return None, tracker.last_identity, "not_regular_file"
                current = identity_from_stat(file_stat)

            if current is None:
                tracker.reset()
            else:
                stable = tracker.observe(current)
                if stable is not None:
                    return stable, stable, ""
            if self._config.stability_interval_seconds:
                time.sleep(self._config.stability_interval_seconds)
        return (
            None,
            tracker.last_identity,
            "file_unstable" if tracker.last_identity is not None else "source_unavailable",
        )

    def capture_to_staging(
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

    def capture_explicit_to_staging(
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
                source_fd = _open_relative_nofollow(
                    root_fd, path.relative_to(source_root), source_flags
                )
            except (FileNotFoundError, NotADirectoryError):
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
            before = identity_from_stat(before_stat)
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

            after = identity_from_stat(os.fstat(source_fd))
            if before != after or total != before.size:
                return None
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            temp_path.replace(final_path)
            directory_fd = os.open(staging_dir, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return final_path, digest.hexdigest()
        except BaseException:
            _unlink_quietly(final_path)
            raise
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            _unlink_quietly(temp_path)
