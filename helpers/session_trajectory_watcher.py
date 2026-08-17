from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helpers.session_keys import ChatSessionContext

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_SESSIONS_DIR = Path("~/.openclaw/agents/main/sessions").expanduser()
DEFAULT_STATE_FILENAME = ".jinx-gchat-trajectory-cursors.json"
CURSOR_STATE_VERSION = 2
MAX_CURSOR_STATE_BYTES = 4 * 1024 * 1024
# Async tool runs (e.g. image_generate) end right after dispatching the tool,
# and the gateway persists the assistant's accompanying text twice: once with
# the toolCall block and once as the run's text-only final message. Suppress
# re-delivery of an identical (text, media) payload within this window.
DUPLICATE_DELIVERY_WINDOW_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class AssistantTrajectoryMessage:
    session_key: str
    space: str
    reply_thread: str
    timestamp: str | None
    text: str
    delivery_id: str
    media_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class _TrajectoryCursor:
    session_key: str
    space: str
    identity_thread: str
    reply_thread: str
    trajectory_file: Path | None
    offset: int
    last_fingerprint: str | None = None
    last_delivered_at: float | None = None


class _CursorStateError(RuntimeError):
    """Persisted watcher state exists but cannot be trusted."""


class _PollOwnership:
    """A process-scoped lease for the one trajectory delivery owner."""

    def __init__(self, watcher: SessionTrajectoryWatcher, name: str) -> None:
        self._watcher_ref = weakref.ref(watcher)
        self._name = name
        self._descriptor: int | None = None
        self._directory_descriptor: int | None = None
        self._pid = os.getpid()

    @property
    def is_held(self) -> bool:
        self._discard_inherited_descriptor()
        return self._descriptor is not None

    def try_acquire(self) -> bool:
        self._discard_inherited_descriptor()
        if self._descriptor is not None:
            return True
        watcher = self._watcher_ref()
        if watcher is None:
            raise _CursorStateError("trajectory watcher is no longer available")
        with watcher._open_state_directory() as directory_descriptor:
            descriptor = _open_lock_file(
                directory_descriptor,
                self._name,
                label="poll ownership",
            )
            retained_directory = os.dup(directory_descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            os.close(retained_directory)
            return False
        except OSError as error:
            os.close(descriptor)
            os.close(retained_directory)
            raise _CursorStateError(
                f"cannot acquire trajectory poll ownership: {type(error).__name__}"
            ) from error
        self._descriptor = descriptor
        self._directory_descriptor = retained_directory
        return True

    def verify_directory_identity(self) -> None:
        """Fail closed if the configured state directory was replaced."""
        self._discard_inherited_descriptor()
        retained = self._directory_descriptor
        watcher = self._watcher_ref()
        if retained is None or watcher is None:
            raise _CursorStateError("trajectory poll ownership is not held")
        with watcher._open_state_directory() as current:
            retained_stat = os.fstat(retained)
            current_stat = os.fstat(current)
            if (retained_stat.st_dev, retained_stat.st_ino) != (
                current_stat.st_dev,
                current_stat.st_ino,
            ):
                raise _CursorStateError(
                    "trajectory state directory changed while poll ownership was held"
                )

    def release(self) -> None:
        self._discard_inherited_descriptor()
        descriptor, self._descriptor = self._descriptor, None
        directory_descriptor, self._directory_descriptor = (
            self._directory_descriptor,
            None,
        )
        if descriptor is None:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)

    def after_fork_in_child(self) -> None:
        """Drop a copied fd without unlocking the parent's shared lease."""
        descriptor, self._descriptor = self._descriptor, None
        directory_descriptor, self._directory_descriptor = (
            self._directory_descriptor,
            None,
        )
        self._pid = os.getpid()
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if directory_descriptor is not None:
            with suppress(OSError):
                os.close(directory_descriptor)

    def _discard_inherited_descriptor(self) -> None:
        if self._pid == os.getpid():
            return
        self.after_fork_in_child()


def _open_lock_file(
    directory_descriptor: int,
    name: str,
    *,
    label: str,
) -> int:
    """Open a private regular lock file without following a final symlink."""
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise _CursorStateError(
            f"cannot open trajectory {label} lock: {type(error).__name__}"
        ) from error

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _CursorStateError(f"trajectory {label} lock is not a regular file")
        if file_stat.st_nlink != 1:
            raise _CursorStateError(f"trajectory {label} lock has unsafe links")
        if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
            raise _CursorStateError(f"trajectory {label} lock has a foreign owner")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


_MEDIA_DIRECTIVE = re.compile(r"^[ \t]*MEDIA:[ \t]*(.*?)[ \t]*$")


def parse_media_directives(text: str) -> tuple[str, tuple[str, ...]]:
    """Remove full-line MEDIA directives and return their paths in order."""
    retained_lines: list[str] = []
    media_paths: list[str] = []
    seen_paths: set[str] = set()

    for line in text.splitlines():
        match = _MEDIA_DIRECTIVE.fullmatch(line)
        if match is None:
            retained_lines.append(line)
            continue

        media_path = match.group(1).strip()
        dedupe_key = os.path.normpath(media_path)
        if media_path and dedupe_key not in seen_paths:
            seen_paths.add(dedupe_key)
            media_paths.append(media_path)

    return "\n".join(retained_lines).strip(), tuple(media_paths)


def extract_assistant_content(
    entry: Any,
) -> tuple[str | None, str, tuple[str, ...]] | None:
    if not isinstance(entry, dict) or entry.get("type") != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None

    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    else:
        text = ""

    normalized_text, media_paths = parse_media_directives(text)
    if not normalized_text and not media_paths:
        return None
    timestamp = entry.get("timestamp")
    return (
        timestamp if isinstance(timestamp, str) else None,
        normalized_text,
        media_paths,
    )


def extract_assistant_text(entry: Any) -> tuple[str | None, str] | None:
    """Compatibility extractor for callers interested only in visible text."""
    extracted = extract_assistant_content(entry)
    if extracted is None or not extracted[1]:
        return None
    timestamp, text, _media_paths = extracted
    return timestamp, text


class SessionTrajectoryWatcher:
    """Tail completed assistant messages from registered OpenClaw trajectories."""

    def __init__(
        self,
        delivery_callback: Callable[[AssistantTrajectoryMessage], bool],
        *,
        sessions_dir: Path = DEFAULT_SESSIONS_DIR,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        state_file: Path | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._delivery_callback = delivery_callback
        self._sessions_dir = sessions_dir.expanduser().resolve(strict=False)
        self._sessions_index = self._sessions_dir / "sessions.json"
        raw_state_file = (
            state_file.expanduser()
            if state_file is not None
            else self._sessions_dir / DEFAULT_STATE_FILENAME
        )
        self._state_file = Path(os.path.abspath(os.fspath(raw_state_file)))
        self._state_transaction_name = f".{self._state_file.name}.state.lock"
        self._poll_ownership = _PollOwnership(
            self,
            f".{self._state_file.name}.poll.lock",
        )
        self._poll_seconds = poll_seconds
        self._pid = os.getpid()
        self._state_lock = threading.RLock()
        self._poll_execution_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursors: dict[str, _TrajectoryCursor] = {}
        self._last_error: Exception | None = None
        # sessions.json grows with every OpenClaw session (it is already ~1 MB
        # for a busy bot). _resolve_file runs once per watched session per poll
        # second, so re-reading and re-parsing the whole index each time burns
        # a CPU core for nothing — cache it by mtime instead.
        self._sessions_index_cache: tuple[int, dict[str, Any]] | None = None
        if hasattr(os, "register_at_fork"):
            watcher_ref = weakref.ref(self)

            def reset_watcher_after_fork() -> None:
                watcher = watcher_ref()
                if watcher is not None:
                    watcher._after_fork_in_child()

            os.register_at_fork(after_in_child=reset_watcher_after_fork)
        # Construction can happen while a WSGI app is preloaded. Avoid opening
        # lock descriptors or creating state directories until a worker starts
        # or handles a request; atomic replacement still makes this read safe.
        self._restore_initial_state()

    @property
    def is_active(self) -> bool:
        self._ensure_current_process()
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def start(self) -> None:
        self._ensure_current_process()
        with self._state_lock:
            if self.is_active:
                return
            # Re-read immediately before polling so an instance constructed
            # early in process startup still sees a state committed meanwhile.
            self._restore_state_locked()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="openclaw-session-trajectory-watcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._ensure_current_process()
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def prepare_session(
        self,
        session_key: str,
        space: str,
        identity_thread: str,
        reply_thread: str,
    ) -> None:
        """Register a routed session and baseline its existing trajectory once.

        A previously registered session retains its cursor and immutable route.
        A key cannot be rebound to another Space or thread because delayed
        output would otherwise leak into the wrong conversation.
        """
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        if not isinstance(space, str) or not space.strip():
            raise ValueError("space must be a non-empty string")
        if not isinstance(identity_thread, str) or not identity_thread.strip():
            raise ValueError("identity_thread must be a non-empty string")
        if not isinstance(reply_thread, str):
            raise TypeError("reply_thread must be a string")
        normalized_key = session_key.strip()
        normalized_space = space.strip()
        normalized_identity_thread = identity_thread.strip()
        normalized_reply_thread = reply_thread.strip()

        self._ensure_current_process()
        with self._state_lock, self._state_transaction() as state_directory:
            shared = self._read_state_for_update_locked(state_directory)
            existing = shared.get(normalized_key)
            if existing is not None:
                self._validate_existing_route(
                    existing,
                    space=normalized_space,
                    identity_thread=normalized_identity_thread,
                    reply_thread=normalized_reply_thread,
                )
                self._cursors = shared
                return

            context = ChatSessionContext.from_session_key(normalized_key)
            if (
                context.space != normalized_space
                or context.thread != normalized_identity_thread
            ):
                raise ValueError(
                    "watched identity does not match its deterministic key"
                )
            self._validate_reply_thread(
                normalized_space,
                normalized_identity_thread,
                normalized_reply_thread,
            )
            trajectory_file = self._resolve_file(normalized_key)
            offset = self._file_size(trajectory_file) if trajectory_file else 0
            shared[normalized_key] = _TrajectoryCursor(
                session_key=normalized_key,
                space=normalized_space,
                identity_thread=normalized_identity_thread,
                reply_thread=normalized_reply_thread,
                trajectory_file=trajectory_file,
                offset=offset,
            )
            self._persist_cursors_locked(shared, state_directory)
            self._cursors = shared
        print(
            f"👁️ [session-watch] prepared session={normalized_key!r} "
            f"space={normalized_space!r} "
            f"identity_thread={normalized_identity_thread!r} "
            f"reply_thread={normalized_reply_thread!r} "
            f"file={str(trajectory_file) if trajectory_file else 'pending'!r} "
            f"offset={offset}"
        )

    @staticmethod
    def _validate_existing_route(
        cursor: _TrajectoryCursor,
        *,
        space: str,
        identity_thread: str,
        reply_thread: str,
    ) -> None:
        if cursor.space != space:
            raise ValueError("a watched session cannot be rebound to another Chat space")
        if cursor.identity_thread != identity_thread:
            raise ValueError(
                "a watched session cannot be rebound to another identity thread"
            )
        if cursor.reply_thread != reply_thread:
            raise ValueError(
                "a watched session cannot be rebound to another reply thread"
            )

    @staticmethod
    def _validate_reply_thread(
        space: str,
        identity_thread: str,
        reply_thread: str,
    ) -> None:
        reply_context = ChatSessionContext.for_thread(space, reply_thread)
        if (
            reply_context.space != space
            or reply_context.thread != identity_thread
            or reply_thread != identity_thread
        ):
            raise ValueError(
                "reply_thread must equal the session's canonical identity thread"
            )

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    with self._poll_execution_lock:
                        if (
                            self._poll_ownership.is_held
                            or self._poll_ownership.try_acquire()
                        ):
                            self._poll_owned_once()
                            self._last_error = None
                except Exception as error:  # noqa: BLE001
                    self._last_error = error
                    print(
                        "❌ [session-watch] poll failed: "
                        f"{type(error).__name__}: {error}"
                    )
                self._stop_event.wait(self._poll_seconds)
        finally:
            self._poll_ownership.release()

    def _load_sessions_index(self) -> dict[str, Any] | None:
        try:
            mtime_ns = self._sessions_index.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._sessions_index_cache
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
        try:
            payload: Any = json.loads(
                self._sessions_index.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        self._sessions_index_cache = (mtime_ns, payload)
        return payload

    def _resolve_file(self, session_key: str) -> Path | None:
        payload = self._load_sessions_index()
        if payload is None:
            return None
        session = payload.get(session_key)
        if not isinstance(session, dict):
            return None
        session_id = session.get("sessionId")
        if (
            not isinstance(session_id, str)
            or not session_id.strip()
            or Path(session_id).name != session_id
        ):
            return None
        candidate = self._sessions_dir / f"{session_id}.jsonl"
        return candidate if self._is_safe_trajectory_path(candidate) else None

    @staticmethod
    def _file_size(trajectory_file: Path) -> int:
        try:
            return trajectory_file.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _complete_lines(chunk: bytes) -> list[bytes]:
        lines = chunk.splitlines(keepends=True)
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            lines.pop()
        return lines

    def _poll_once(self) -> None:
        """Run one owned poll, or return when another process is the owner."""
        self._ensure_current_process()
        with self._poll_execution_lock:
            already_owned = self._poll_ownership.is_held
            if not already_owned and not self._poll_ownership.try_acquire():
                return
            try:
                self._poll_owned_once()
            finally:
                if not already_owned:
                    self._poll_ownership.release()

    def _poll_owned_once(self) -> None:
        # Registrations may have been committed by any WSGI worker since this
        # process last polled. Refresh only while holding poll ownership.
        self._poll_ownership.verify_directory_identity()
        with self._state_lock:
            self._restore_state_locked()
            session_keys = tuple(self._cursors)

        first_error: Exception | None = None
        for session_key in session_keys:
            try:
                self._poll_session(session_key)
            except Exception as error:  # noqa: BLE001
                # One corrupt trajectory or failing delivery must not prevent
                # other registered sessions from advancing in this poll cycle.
                if first_error is None:
                    first_error = error

        if first_error is not None:
            raise first_error

    def _poll_session(self, session_key: str) -> None:
        with self._state_lock:
            cursor = self._cursors.get(session_key)
            if cursor is None:
                return
            session_key = cursor.session_key
            space = cursor.space
            reply_thread = cursor.reply_thread
            trajectory_file = cursor.trajectory_file
            offset = cursor.offset

        resolved_file = self._resolve_file(session_key)
        if resolved_file is not None and resolved_file != trajectory_file:
            if not self._switch_trajectory_file(session_key, resolved_file):
                return
            trajectory_file = resolved_file
            offset = 0
        elif trajectory_file is None:
            return

        # Re-check after restoration and immediately before opening. A valid
        # basename must not become a symlink out of the sessions directory.
        if not self._is_safe_trajectory_path(trajectory_file):
            return

        try:
            file_size = trajectory_file.stat().st_size
            if file_size < offset:
                reset_offset = self._reset_truncated_cursor(
                    session_key,
                    trajectory_file,
                    file_size,
                )
                if reset_offset is None:
                    return
                offset = reset_offset
            with trajectory_file.open("rb") as file_handle:
                file_handle.seek(offset)
                lines = self._complete_lines(file_handle.read())
        except FileNotFoundError:
            return

        for raw_line in lines:
            line_offset = offset
            next_offset = offset + len(raw_line)
            try:
                entry = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if not self._commit_offset(
                    session_key,
                    trajectory_file,
                    line_offset,
                    next_offset,
                ):
                    return
                offset = next_offset
                continue

            extracted = extract_assistant_content(entry)
            delivered_fingerprint: str | None = None
            delivered_at: float | None = None
            if extracted is not None:
                timestamp, text, media_paths = extracted
                fingerprint = (text, media_paths)
                fingerprint_digest = self._fingerprint_digest(fingerprint)
                if self._is_duplicate_delivery(session_key, fingerprint_digest):
                    delivered_fingerprint = fingerprint_digest
                    with self._state_lock:
                        cursor = self._cursors.get(session_key)
                        # Duplicate delivery implies a matching, still-fresh
                        # cursor fingerprint; keep its original timestamp.
                        delivered_at = (
                            cursor.last_delivered_at
                            if cursor is not None
                            else time.time()
                        )
                    print(
                        f"⏭️ [session-watch] skipped duplicate session={session_key!r} "
                        f"offset={line_offset}"
                    )
                else:
                    delivery_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"jinx-session-message:{session_key}:{trajectory_file.name}:{line_offset}",
                        )
                    )
                    event = AssistantTrajectoryMessage(
                        session_key=session_key,
                        space=space,
                        reply_thread=reply_thread,
                        timestamp=timestamp,
                        text=text,
                        delivery_id=delivery_id,
                        media_paths=media_paths,
                    )
                    if not self._delivery_callback(event):
                        return
                    delivered_at = time.time()
                    delivered_fingerprint = fingerprint_digest
                    print(
                        f"✅ [session-watch] delivered session={session_key!r} "
                        f"offset={line_offset}"
                    )

            if not self._commit_offset(
                session_key,
                trajectory_file,
                line_offset,
                next_offset,
                delivered_fingerprint=delivered_fingerprint,
                delivered_at=delivered_at,
            ):
                return
            offset = next_offset

    def _is_duplicate_delivery(
        self,
        session_key: str,
        fingerprint_digest: str,
    ) -> bool:
        # Only the immediately preceding delivery can be the async-tool text
        # persisted twice; a match against older history would also catch a
        # legitimate identical answer from an unrelated, later turn.
        with self._state_lock:
            cursor = self._cursors.get(session_key)
            return bool(
                cursor is not None
                and cursor.last_fingerprint == fingerprint_digest
                and cursor.last_delivered_at is not None
                and time.time() - cursor.last_delivered_at
                < DUPLICATE_DELIVERY_WINDOW_SECONDS
            )

    @staticmethod
    def _fingerprint_digest(
        fingerprint: tuple[str, tuple[str, ...]],
    ) -> str:
        text, media_paths = fingerprint
        encoded = json.dumps(
            [text, media_paths],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _switch_trajectory_file(
        self,
        session_key: str,
        trajectory_file: Path,
    ) -> bool:
        """Persist a session-file transition before reading the new file."""
        with self._state_lock, self._state_transaction() as state_directory:
            shared = self._read_state_for_update_locked(state_directory)
            cursor = shared.get(session_key)
            if cursor is None:
                self._cursors = shared
                return False
            if cursor.trajectory_file != trajectory_file:
                cursor.trajectory_file = trajectory_file
                cursor.offset = 0
                self._persist_cursors_locked(shared, state_directory)
            self._cursors = shared
            return True

    def _reset_truncated_cursor(
        self,
        session_key: str,
        trajectory_file: Path,
        file_size: int,
    ) -> int | None:
        """Durably reset an offset when the active trajectory was truncated."""
        with self._state_lock, self._state_transaction() as state_directory:
            shared = self._read_state_for_update_locked(state_directory)
            cursor = shared.get(session_key)
            if cursor is None or cursor.trajectory_file != trajectory_file:
                self._cursors = shared
                return None
            if cursor.offset > file_size:
                cursor.offset = 0
                self._persist_cursors_locked(shared, state_directory)
            self._cursors = shared
            return cursor.offset

    def _commit_offset(
        self,
        session_key: str,
        trajectory_file: Path,
        expected_offset: int,
        next_offset: int,
        *,
        delivered_fingerprint: str | None = None,
        delivered_at: float | None = None,
    ) -> bool:
        """Advance one cursor using a cross-process compare-and-swap update."""
        if next_offset < expected_offset:
            raise ValueError("next trajectory offset cannot move backwards")
        if (delivered_fingerprint is None) != (delivered_at is None):
            raise ValueError("trajectory delivery metadata must be complete")
        with self._state_lock, self._state_transaction() as state_directory:
            shared = self._read_state_for_update_locked(state_directory)
            cursor = shared.get(session_key)
            if cursor is None or cursor.trajectory_file != trajectory_file:
                self._cursors = shared
                return False
            if cursor.offset != expected_offset:
                # Only the poll owner may advance offsets. A mismatch means
                # this snapshot lost ownership or durable state changed;
                # reload instead of overwriting a newer cursor.
                self._cursors = shared
                return False
            cursor.offset = next_offset
            if delivered_fingerprint is not None:
                cursor.last_fingerprint = delivered_fingerprint
                cursor.last_delivered_at = delivered_at
            self._persist_cursors_locked(shared, state_directory)
            self._cursors = shared
            return True

    def _restore_state(self) -> None:
        self._ensure_current_process()
        with self._state_lock:
            self._restore_state_locked()

    def _restore_state_locked(self) -> None:
        with self._state_transaction() as state_directory:
            self._load_state_locked(state_directory)

    def _restore_initial_state(self) -> None:
        with self._state_lock:
            try:
                self._state_file.lstat()
            except FileNotFoundError:
                self._cursors = {}
                return
            except OSError as error:
                self._cursors = {}
                print(
                    "⚠️ [session-watch] ignored cursor state: "
                    f"cannot inspect state: {type(error).__name__}"
                )
                return
            self._load_state_locked()

    def _load_state_locked(self, directory_descriptor: int | None = None) -> None:
        try:
            restored = self._read_state(directory_descriptor)
        except _CursorStateError as error:
            # Never retain a process-local route after durable state becomes
            # untrusted; doing so could leak output to a stale destination.
            self._cursors = {}
            print(f"⚠️ [session-watch] ignored cursor state: {error}")
            return
        self._cursors = restored or {}

    def _read_state_for_update_locked(
        self,
        directory_descriptor: int | None = None,
    ) -> dict[str, _TrajectoryCursor]:
        restored = self._read_state(directory_descriptor)
        return restored or {}

    @contextmanager
    def _state_transaction(self) -> Iterator[int]:
        with self._open_state_directory() as directory_descriptor:
            descriptor = _open_lock_file(
                directory_descriptor,
                self._state_transaction_name,
                label="cursor state transaction",
            )
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except OSError as error:
                    raise _CursorStateError(
                        "cannot lock trajectory cursor state: "
                        f"{type(error).__name__}"
                    ) from error
                try:
                    yield directory_descriptor
                finally:
                    with suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def _open_state_directory(self) -> Iterator[int]:
        parent = self._state_file.parent
        self._reject_existing_symlink_components(parent)
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise _CursorStateError(
                f"cannot open cursor state directory: {type(error).__name__}"
            ) from error
        try:
            directory_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise _CursorStateError("cursor state parent is not a directory")
            if hasattr(os, "geteuid") and directory_stat.st_uid != os.geteuid():
                raise _CursorStateError("cursor state parent has a foreign owner")
            os.fchmod(descriptor, 0o700)
            # Detect a parent-path replacement between validation and open.
            if parent.resolve(strict=True) != parent:
                raise _CursorStateError("cursor state path traverses a symlink")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _reject_existing_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                component_stat = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _CursorStateError(
                    "cannot inspect cursor state directory"
                ) from error
            if stat.S_ISLNK(component_stat.st_mode):
                raise _CursorStateError("cursor state path cannot traverse a symlink")
            if current != path and not stat.S_ISDIR(component_stat.st_mode):
                raise _CursorStateError("cursor state parent must be a directory")

    def _ensure_current_process(self) -> None:
        if self._pid != os.getpid():
            self._after_fork_in_child()

    def _after_fork_in_child(self) -> None:
        self._pid = os.getpid()
        self._poll_ownership.after_fork_in_child()
        self._state_lock = threading.RLock()
        self._poll_execution_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._cursors = {}
        self._last_error = None

    def _read_state(
        self,
        directory_descriptor: int | None = None,
    ) -> dict[str, _TrajectoryCursor] | None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            if directory_descriptor is None:
                with self._open_state_directory() as opened_directory:
                    return self._read_state(opened_directory)
            else:
                try:
                    descriptor = os.open(
                        self._state_file.name,
                        flags,
                        dir_fd=directory_descriptor,
                    )
                except FileNotFoundError:
                    return None
        except _CursorStateError:
            raise
        except OSError as error:
            raise _CursorStateError(
                f"cannot read {self._state_file.name}: {type(error).__name__}"
            ) from error

        try:
            with os.fdopen(descriptor, "rb") as state_handle:
                file_stat = os.fstat(state_handle.fileno())
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                    raise _CursorStateError(
                        "cursor state is not a private regular file"
                    )
                if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
                    raise _CursorStateError("cursor state has a foreign owner")
                if stat.S_IMODE(file_stat.st_mode) != 0o600:
                    raise _CursorStateError("cursor state permissions are not private")
                encoded = state_handle.read(MAX_CURSOR_STATE_BYTES + 1)
        except OSError as error:
            raise _CursorStateError(
                f"cannot read {self._state_file.name}: {type(error).__name__}"
            ) from error

        if len(encoded) > MAX_CURSOR_STATE_BYTES:
            raise _CursorStateError("cursor state is too large")
        try:
            payload: Any = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _CursorStateError("cursor state is not valid JSON") from error
        return self._validate_state_payload(payload)

    def _validate_state_payload(
        self,
        payload: Any,
    ) -> dict[str, _TrajectoryCursor]:
        if not isinstance(payload, dict):
            raise _CursorStateError("cursor state is not an object")
        version = payload.get("version")
        if type(version) is not int or version != CURSOR_STATE_VERSION:
            raise _CursorStateError("unsupported cursor state version")
        persisted_cursors = payload.get("cursors")
        if not isinstance(persisted_cursors, dict):
            raise _CursorStateError("cursor state has no cursor map")

        restored: dict[str, _TrajectoryCursor] = {}
        for session_key, persisted in persisted_cursors.items():
            if not isinstance(session_key, str) or not isinstance(persisted, dict):
                raise _CursorStateError("cursor state contains an invalid entry")
            try:
                context = ChatSessionContext.from_session_key(session_key)
            except (TypeError, ValueError) as error:
                raise _CursorStateError(
                    "cursor state contains an invalid deterministic key"
                ) from error
            if context.session_key != session_key:
                raise _CursorStateError("cursor state key is not canonical")

            space = persisted.get("space")
            identity_thread = persisted.get("identity_thread")
            reply_thread = persisted.get("reply_thread")
            if space != context.space or identity_thread != context.thread:
                raise _CursorStateError(
                    "cursor state identity does not match its deterministic key"
                )
            if not isinstance(reply_thread, str):
                raise _CursorStateError("cursor state reply route is invalid")
            try:
                self._validate_reply_thread(
                    context.space,
                    context.thread,
                    reply_thread,
                )
            except (TypeError, ValueError) as error:
                raise _CursorStateError(
                    "cursor state reply route is invalid"
                ) from error

            offset = persisted.get("offset")
            if type(offset) is not int or offset < 0:
                raise _CursorStateError("cursor state offset is invalid")

            last_fingerprint = persisted.get("last_fingerprint")
            last_delivered_at = persisted.get("last_delivered_at")
            if last_fingerprint is not None and (
                not isinstance(last_fingerprint, str)
                or len(last_fingerprint) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in last_fingerprint
                )
            ):
                raise _CursorStateError("cursor delivery fingerprint is invalid")
            if last_delivered_at is not None and (
                type(last_delivered_at) not in (int, float)
                or last_delivered_at < 0
                or not math.isfinite(last_delivered_at)
            ):
                raise _CursorStateError("cursor delivery timestamp is invalid")
            if (last_fingerprint is None) != (last_delivered_at is None):
                raise _CursorStateError("cursor delivery fingerprint is incomplete")

            trajectory_name = persisted.get("trajectory_file")
            if trajectory_name is None:
                if offset != 0:
                    raise _CursorStateError(
                        "a pending cursor cannot have a committed offset"
                    )
                trajectory_file = None
            elif self._is_valid_trajectory_name(trajectory_name):
                trajectory_file = self._sessions_dir / trajectory_name
                if not self._is_safe_trajectory_path(trajectory_file):
                    raise _CursorStateError("cursor trajectory escapes sessions dir")
            else:
                raise _CursorStateError("cursor trajectory name is invalid")

            restored[session_key] = _TrajectoryCursor(
                session_key=session_key,
                space=context.space,
                identity_thread=context.thread,
                reply_thread=reply_thread,
                trajectory_file=trajectory_file,
                offset=offset,
                last_fingerprint=last_fingerprint,
                last_delivered_at=(
                    float(last_delivered_at)
                    if last_delivered_at is not None
                    else None
                ),
            )
        return restored

    @staticmethod
    def _is_valid_trajectory_name(value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and Path(value).name == value
            and Path(value).suffix == ".jsonl"
        )

    def _is_safe_trajectory_path(self, trajectory_file: Path) -> bool:
        try:
            resolved = trajectory_file.resolve(strict=False)
        except OSError:
            return False
        return resolved.parent == self._sessions_dir

    def _persist_cursors_locked(
        self,
        cursor_state: dict[str, _TrajectoryCursor],
        directory_descriptor: int | None = None,
    ) -> None:
        cursors = {
            session_key: {
                "space": cursor.space,
                "identity_thread": cursor.identity_thread,
                "reply_thread": cursor.reply_thread,
                "trajectory_file": (
                    cursor.trajectory_file.name
                    if cursor.trajectory_file is not None
                    else None
                ),
                "offset": cursor.offset,
                "last_fingerprint": cursor.last_fingerprint,
                "last_delivered_at": cursor.last_delivered_at,
            }
            for session_key, cursor in sorted(cursor_state.items())
        }
        encoded = json.dumps(
            {"version": CURSOR_STATE_VERSION, "cursors": cursors},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_CURSOR_STATE_BYTES:
            raise _CursorStateError("cursor state is too large to persist")
        self._write_state_atomic(encoded, directory_descriptor)

    def _write_state_atomic(
        self,
        encoded: bytes,
        directory_descriptor: int | None = None,
    ) -> None:
        if directory_descriptor is None:
            with self._open_state_directory() as opened_directory:
                self._write_state_atomic(encoded, opened_directory)
            return
        temporary = f".{self._state_file.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            file_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or (
                    hasattr(os, "geteuid")
                    and file_stat.st_uid != os.geteuid()
                )
            ):
                raise _CursorStateError(
                    "temporary cursor state is not a private regular file"
                )
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short cursor-state write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                self._state_file.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        except OSError as error:
            raise _CursorStateError(
                f"cannot persist {self._state_file.name}: {type(error).__name__}"
            ) from error
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(FileNotFoundError, OSError):
                os.unlink(temporary, dir_fd=directory_descriptor)


__all__ = [
    "AssistantTrajectoryMessage",
    "SessionTrajectoryWatcher",
    "extract_assistant_content",
    "extract_assistant_text",
    "parse_media_directives",
]
