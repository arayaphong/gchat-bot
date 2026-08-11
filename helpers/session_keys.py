from __future__ import annotations

import re
from dataclasses import dataclass

SESSION_AGENT = "main"
SESSION_CHANNEL = "gchat"
SESSION_MAIN_CONTEXT = "main"
SESSION_KEY_PREFIX = f"agent:{SESSION_AGENT}:{SESSION_CHANNEL}:"

_RESOURCE_ID = r"[^/:?#\s]+"
_SPACE_NAME_RE = re.compile(rf"^spaces/(?P<space_id>{_RESOURCE_ID})$")
_THREAD_NAME_RE = re.compile(
    rf"^spaces/(?P<space_id>{_RESOURCE_ID})/threads/(?P<thread_id>{_RESOURCE_ID})$"
)
_SESSION_KEY_RE = re.compile(
    rf"^{re.escape(SESSION_KEY_PREFIX)}"
    rf"(?P<space_id>{_RESOURCE_ID}):(?P<context_id>{_RESOURCE_ID})$"
)


def _space_id(space: str) -> str:
    if not isinstance(space, str):
        raise TypeError("Google Chat space must be a string")
    match = _SPACE_NAME_RE.fullmatch(space.strip())
    if match is None:
        raise ValueError("invalid Google Chat space resource name")
    return match.group("space_id")


def _thread_ids(thread: str) -> tuple[str, str]:
    if not isinstance(thread, str):
        raise TypeError("Google Chat thread must be a string")
    match = _THREAD_NAME_RE.fullmatch(thread.strip())
    if match is None:
        raise ValueError("invalid Google Chat thread resource name")
    thread_id = match.group("thread_id")
    if thread_id == SESSION_MAIN_CONTEXT:
        raise ValueError("Google Chat thread ID collides with the reserved main context")
    return match.group("space_id"), thread_id


@dataclass(frozen=True, slots=True)
class ChatSessionContext:
    """Deterministic OpenClaw identity and reply route for one Chat context."""

    space: str
    reply_thread: str
    session_key: str
    is_direct_message: bool = False

    @classmethod
    def from_event(
        cls,
        space: str,
        thread: str,
        *,
        is_direct_message: bool,
        thread_reply: bool | None,
    ) -> ChatSessionContext:
        """Build the context for an inbound Google Chat message event.

        Direct messages and top-level messages share the Space's ``main``
        session. Only an explicit threaded reply in a non-DM Space selects a
        thread-specific session. Missing ``threadReply`` is therefore safely
        treated as a top-level message.
        """

        if not isinstance(is_direct_message, bool):
            raise TypeError("is_direct_message must be a boolean")
        if thread_reply is not None and not isinstance(thread_reply, bool):
            raise TypeError("thread_reply must be a boolean or None")
        space_id = _space_id(space)
        normalized_space = f"spaces/{space_id}"
        if not isinstance(thread, str):
            raise TypeError("Google Chat thread must be a string")
        normalized_thread = thread.strip()
        thread_ids = _thread_ids(normalized_thread) if normalized_thread else None
        if thread_ids is not None and thread_ids[0] != space_id:
            raise ValueError("Google Chat thread does not belong to the event space")
        if is_direct_message or thread_reply is not True:
            return cls(
                space=normalized_space,
                reply_thread="",
                session_key=(
                    f"{SESSION_KEY_PREFIX}{space_id}:{SESSION_MAIN_CONTEXT}"
                ),
                is_direct_message=is_direct_message,
            )

        if thread_ids is None:
            raise ValueError("invalid Google Chat thread resource name")
        _, thread_id = thread_ids
        return cls(
            space=normalized_space,
            reply_thread=f"spaces/{space_id}/threads/{thread_id}",
            session_key=f"{SESSION_KEY_PREFIX}{space_id}:{thread_id}",
            is_direct_message=False,
        )

    @classmethod
    def for_thread(cls, space: str, thread: str) -> ChatSessionContext:
        """Build a thread context explicitly, including a new `/new` target."""

        space_id = _space_id(space)
        thread_space_id, thread_id = _thread_ids(thread)
        if thread_space_id != space_id:
            raise ValueError("Google Chat thread does not belong to the event space")
        return cls(
            space=f"spaces/{space_id}",
            reply_thread=f"spaces/{space_id}/threads/{thread_id}",
            session_key=f"{SESSION_KEY_PREFIX}{space_id}:{thread_id}",
            is_direct_message=False,
        )

    @classmethod
    def from_session_key(cls, session_key: str) -> ChatSessionContext:
        """Recover the deterministic Chat route embedded in a session key."""

        if not isinstance(session_key, str):
            raise TypeError("session_key must be a string")
        normalized_key = session_key.strip()
        match = _SESSION_KEY_RE.fullmatch(normalized_key)
        if match is None:
            raise ValueError("invalid deterministic Google Chat session key")

        space_id = match.group("space_id")
        context_id = match.group("context_id")
        space = f"spaces/{space_id}"
        reply_thread = (
            ""
            if context_id == SESSION_MAIN_CONTEXT
            else f"{space}/threads/{context_id}"
        )
        return cls(
            space=space,
            reply_thread=reply_thread,
            session_key=normalized_key,
            is_direct_message=False,
        )

    @property
    def is_thread(self) -> bool:
        return bool(self.reply_thread)


def derive_session_key(
    space: str,
    thread: str,
    *,
    is_direct_message: bool,
    thread_reply: bool | None,
) -> str:
    """Compatibility helper for callers that only need the exact key."""

    return ChatSessionContext.from_event(
        space,
        thread,
        is_direct_message=is_direct_message,
        thread_reply=thread_reply,
    ).session_key


def parse_session_key(session_key: str) -> ChatSessionContext:
    return ChatSessionContext.from_session_key(session_key)


__all__ = [
    "SESSION_AGENT",
    "SESSION_CHANNEL",
    "SESSION_KEY_PREFIX",
    "SESSION_MAIN_CONTEXT",
    "ChatSessionContext",
    "derive_session_key",
    "parse_session_key",
]
