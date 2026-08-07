from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_SESSIONS_DIR = Path("~/.openclaw/agents/main/sessions").expanduser()
# Async tool runs (e.g. image_generate) end right after dispatching the tool,
# and the gateway persists the assistant's accompanying text twice: once with
# the toolCall block and once as the run's text-only final message. Suppress
# re-delivery of an identical (text, media) payload within this window.
DUPLICATE_DELIVERY_WINDOW_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class AssistantTrajectoryMessage:
    session_key: str
    timestamp: str | None
    text: str
    delivery_id: str
    media_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class _TrajectoryCursor:
    session_key: str
    trajectory_file: Path | None
    offset: int


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
    """Tail completed assistant messages from the active OpenClaw trajectory."""

    def __init__(
        self,
        delivery_callback: Callable[[AssistantTrajectoryMessage], bool],
        *,
        sessions_dir: Path = DEFAULT_SESSIONS_DIR,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._delivery_callback = delivery_callback
        self._sessions_dir = sessions_dir.expanduser()
        self._sessions_index = self._sessions_dir / "sessions.json"
        self._poll_seconds = poll_seconds
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursor: _TrajectoryCursor | None = None
        self._last_error: Exception | None = None
        self._delivered_fingerprints: dict[tuple[str, tuple[str, ...]], float] = {}

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

    def prepare_session(self, session_key: str) -> None:
        """Select a session and baseline its existing trajectory exactly once."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        normalized_key = session_key.strip()

        with self._state_lock:
            if self._cursor is not None and self._cursor.session_key == normalized_key:
                return

            trajectory_file = self._resolve_file(normalized_key)
            offset = self._file_size(trajectory_file) if trajectory_file else 0
            self._cursor = _TrajectoryCursor(
                session_key=normalized_key,
                trajectory_file=trajectory_file,
                offset=offset,
            )
            self._delivered_fingerprints.clear()
            print(
                f"👁️ [session-watch] prepared session={normalized_key!r} "
                f"file={str(trajectory_file) if trajectory_file else 'pending'!r} "
                f"offset={offset}"
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
        return self._sessions_dir / f"{session_id}.jsonl"

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
            cursor = self._cursor
            if cursor is None:
                return
            session_key = cursor.session_key
            trajectory_file = cursor.trajectory_file
            offset = cursor.offset

        resolved_file = self._resolve_file(session_key)
        if resolved_file is not None and resolved_file != trajectory_file:
            trajectory_file = resolved_file
            offset = 0
            with self._state_lock:
                if self._cursor is None or self._cursor.session_key != session_key:
                    return
                self._cursor.trajectory_file = trajectory_file
                self._cursor.offset = 0
        elif trajectory_file is None:
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
                if self._is_duplicate_delivery(fingerprint):
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
                        timestamp=timestamp,
                        text=text,
                        delivery_id=delivery_id,
                        media_paths=media_paths,
                    )
                    if not self._delivery_callback(event):
                        return
                    self._delivered_fingerprints[fingerprint] = time.monotonic()
                    print(
                        f"✅ [session-watch] delivered session={session_key!r} "
                        f"offset={line_offset}"
                    )

            self._commit_offset(session_key, trajectory_file, next_offset)
            offset = next_offset

    def _is_duplicate_delivery(self, fingerprint: tuple[str, tuple[str, ...]]) -> bool:
        now = time.monotonic()
        stale = [
            key
            for key, delivered_at in self._delivered_fingerprints.items()
            if now - delivered_at >= DUPLICATE_DELIVERY_WINDOW_SECONDS
        ]
        for key in stale:
            del self._delivered_fingerprints[key]
        delivered_at = self._delivered_fingerprints.get(fingerprint)
        return delivered_at is not None

    def _commit_offset(
        self,
        session_key: str,
        trajectory_file: Path,
        offset: int,
    ) -> None:
        with self._state_lock:
            cursor = self._cursor
            if (
                cursor is not None
                and cursor.session_key == session_key
                and cursor.trajectory_file == trajectory_file
            ):
                cursor.offset = offset


__all__ = [
    "AssistantTrajectoryMessage",
    "SessionTrajectoryWatcher",
    "extract_assistant_content",
    "extract_assistant_text",
    "parse_media_directives",
]
