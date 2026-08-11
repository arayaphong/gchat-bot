from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
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


class _CursorStateError(RuntimeError):
    """Persisted watcher state exists but cannot be trusted."""


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
        self._state_file = (
            state_file.expanduser().resolve(strict=False)
            if state_file is not None
            else self._sessions_dir / DEFAULT_STATE_FILENAME
        )
        self._poll_seconds = poll_seconds
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursors: dict[str, _TrajectoryCursor] = {}
        self._last_error: Exception | None = None
        self._delivered_fingerprints: dict[tuple[str, str, tuple[str, ...]], float] = {}
        self._restore_state()

    @property
    def is_active(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def start(self) -> None:
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

        with self._state_lock:
            existing = self._cursors.get(normalized_key)
            if existing is not None:
                if existing.space != normalized_space:
                    raise ValueError(
                        "a watched session cannot be rebound to another Chat space"
                    )
                if existing.identity_thread != normalized_identity_thread:
                    raise ValueError(
                        "a watched session cannot be rebound to another identity thread"
                    )
                if existing.reply_thread != normalized_reply_thread:
                    raise ValueError(
                        "a watched session cannot be rebound to another reply thread"
                    )
                return

            context = ChatSessionContext.from_session_key(normalized_key)
            if (
                context.space != normalized_space
                or context.thread != normalized_identity_thread
            ):
                raise ValueError(
                    "watched identity does not match its deterministic key"
                )
            self._validate_reply_thread(normalized_space, normalized_reply_thread)

            trajectory_file = self._resolve_file(normalized_key)
            offset = self._file_size(trajectory_file) if trajectory_file else 0
            self._cursors[normalized_key] = _TrajectoryCursor(
                session_key=normalized_key,
                space=normalized_space,
                identity_thread=normalized_identity_thread,
                reply_thread=normalized_reply_thread,
                trajectory_file=trajectory_file,
                offset=offset,
            )
            try:
                self._persist_state_locked()
            except BaseException:
                del self._cursors[normalized_key]
                raise
            print(
                f"👁️ [session-watch] prepared session={normalized_key!r} "
                f"space={normalized_space!r} "
                f"identity_thread={normalized_identity_thread!r} "
                f"reply_thread={normalized_reply_thread!r} "
                f"file={str(trajectory_file) if trajectory_file else 'pending'!r} "
                f"offset={offset}"
            )

    @staticmethod
    def _validate_reply_thread(space: str, reply_thread: str) -> None:
        if not reply_thread:
            return
        reply_context = ChatSessionContext.for_thread(space, reply_thread)
        if reply_context.space != space or reply_context.thread != reply_thread:
            raise ValueError(
                "reply_thread must be a canonical thread in its Chat space"
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
                self._last_error = None
            except Exception as error:  # noqa: BLE001
                self._last_error = error
                print(
                    f"❌ [session-watch] poll failed: {type(error).__name__}: {error}"
                )
            self._stop_event.wait(self._poll_seconds)

    def _resolve_file(self, session_key: str) -> Path | None:
        try:
            payload: Any = json.loads(self._sessions_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
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
        with self._state_lock:
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
            trajectory_file = resolved_file
            offset = 0
            with self._state_lock:
                cursor = self._cursors.get(session_key)
                if cursor is None:
                    return
                cursor.trajectory_file = trajectory_file
                cursor.offset = 0
        elif trajectory_file is None:
            return

        # Re-check after restoration and immediately before opening. A valid
        # basename must not become a symlink out of the sessions directory.
        if not self._is_safe_trajectory_path(trajectory_file):
            return

        try:
            file_size = trajectory_file.stat().st_size
            if file_size < offset:
                offset = 0
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
                self._commit_offset(session_key, trajectory_file, next_offset)
                offset = next_offset
                continue

            extracted = extract_assistant_content(entry)
            if extracted is not None:
                timestamp, text, media_paths = extracted
                fingerprint = (text, media_paths)
                if self._is_duplicate_delivery(session_key, fingerprint):
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
                    self._delivered_fingerprints[(session_key, *fingerprint)] = (
                        time.monotonic()
                    )
                    print(
                        f"✅ [session-watch] delivered session={session_key!r} "
                        f"offset={line_offset}"
                    )

            self._commit_offset(session_key, trajectory_file, next_offset)
            offset = next_offset

    def _is_duplicate_delivery(
        self,
        session_key: str,
        fingerprint: tuple[str, tuple[str, ...]],
    ) -> bool:
        now = time.monotonic()
        stale = [
            key
            for key, delivered_at in self._delivered_fingerprints.items()
            if now - delivered_at >= DUPLICATE_DELIVERY_WINDOW_SECONDS
        ]
        for key in stale:
            del self._delivered_fingerprints[key]
        delivered_at = self._delivered_fingerprints.get((session_key, *fingerprint))
        return delivered_at is not None

    def _commit_offset(
        self,
        session_key: str,
        trajectory_file: Path,
        offset: int,
    ) -> None:
        with self._state_lock:
            cursor = self._cursors.get(session_key)
            if cursor is not None and cursor.trajectory_file == trajectory_file:
                previous_offset = cursor.offset
                cursor.offset = offset
                try:
                    self._persist_state_locked()
                except BaseException:
                    cursor.offset = previous_offset
                    raise

    def _restore_state(self) -> None:
        with self._state_lock:
            self._restore_state_locked()

    def _restore_state_locked(self) -> None:
        try:
            restored = self._read_state()
        except _CursorStateError as error:
            # A bad state file must neither prevent startup nor supply a route.
            # The next explicitly prepared session will replace it atomically.
            print(f"⚠️ [session-watch] ignored cursor state: {error}")
            return
        if restored is not None:
            self._cursors = restored

    def _read_state(self) -> dict[str, _TrajectoryCursor] | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._state_file, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _CursorStateError(
                f"cannot read {self._state_file.name}: {type(error).__name__}"
            ) from error

        try:
            with os.fdopen(descriptor, "rb") as state_handle:
                file_stat = os.fstat(state_handle.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise _CursorStateError("cursor state is not a regular file")
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
                self._validate_reply_thread(context.space, reply_thread)
            except (TypeError, ValueError) as error:
                raise _CursorStateError(
                    "cursor state reply route is invalid"
                ) from error

            offset = persisted.get("offset")
            if type(offset) is not int or offset < 0:
                raise _CursorStateError("cursor state offset is invalid")

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

    def _persist_state_locked(self) -> None:
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
            }
            for session_key, cursor in sorted(self._cursors.items())
        }
        encoded = json.dumps(
            {"version": CURSOR_STATE_VERSION, "cursors": cursors},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_CURSOR_STATE_BYTES:
            raise _CursorStateError("cursor state is too large to persist")
        self._write_state_atomic(encoded)

    def _write_state_atomic(self, encoded: bytes) -> None:
        parent = self._state_file.parent
        temporary = parent / f".{self._state_file.name}.{uuid.uuid4().hex}.tmp"
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as state_handle:
                    state_handle.write(encoded)
                    state_handle.flush()
                    os.fsync(state_handle.fileno())
                os.replace(temporary, self._state_file)
                os.chmod(self._state_file, 0o600, follow_symlinks=False)
                directory_descriptor = os.open(
                    parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except BaseException:
                with suppress(FileNotFoundError):
                    temporary.unlink()
                raise
        except OSError as error:
            raise _CursorStateError(
                f"cannot persist {self._state_file.name}: {type(error).__name__}"
            ) from error


__all__ = [
    "AssistantTrajectoryMessage",
    "SessionTrajectoryWatcher",
    "extract_assistant_content",
    "extract_assistant_text",
    "parse_media_directives",
]
