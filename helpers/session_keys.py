from __future__ import annotations

import re
from dataclasses import dataclass

SESSION_AGENT = "main"
SESSION_CHANNEL = "gchat"
SESSION_KEY_PREFIX = f"agent:{SESSION_AGENT}:{SESSION_CHANNEL}:"
# OpenClaw's chat-send/session-key schema caps keys at 512 characters. Encoded
# Chat identities are ASCII, so the character and UTF-8 byte lengths match.
MAX_SESSION_KEY_LENGTH = 512
# ``main`` was used by the retired Space-level identity. It remains reserved so
# a real Chat thread cannot collide with a legacy deterministic key.
_LEGACY_MAIN_CONTEXT = "main"

_RESOURCE_ID = r"[^/:?#\s]+"
_SPACE_NAME_RE = re.compile(rf"^spaces/(?P<space_id>{_RESOURCE_ID})$")
_THREAD_NAME_RE = re.compile(
    rf"^spaces/(?P<space_id>{_RESOURCE_ID})/threads/(?P<thread_id>{_RESOURCE_ID})$"
)
_SESSION_COMPONENT = r"[a-z0-9._%\-]+"
_SESSION_KEY_RE = re.compile(
    rf"^{re.escape(SESSION_KEY_PREFIX)}"
    rf"(?P<space_component>{_SESSION_COMPONENT}):"
    rf"(?P<thread_component>{_SESSION_COMPONENT})$"
)
_SESSION_COMPONENT_SAFE_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789._-")
_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")


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
    if thread_id == _LEGACY_MAIN_CONTEXT:
        raise ValueError(
            "Google Chat thread ID collides with the reserved main context"
        )
    return match.group("space_id"), thread_id


def _encode_session_component(resource_id: str) -> str:
    """Encode one case-sensitive Chat ID into an OpenClaw-stable component.

    OpenClaw canonicalizes ordinary session keys to lowercase. Percent-encoding
    every byte outside a lowercase-safe alphabet preserves the exact Google ID
    while making that canonicalization an idempotent operation.
    """

    encoded: list[str] = []
    for byte in resource_id.encode("utf-8"):
        if byte in _SESSION_COMPONENT_SAFE_BYTES:
            encoded.append(chr(byte))
        else:
            encoded.append(f"%{byte:02x}")
    return "".join(encoded)


def _decode_session_component(component: str) -> str:
    if not isinstance(component, str) or not component:
        raise ValueError("invalid encoded Google Chat resource ID")

    decoded = bytearray()
    index = 0
    while index < len(component):
        character = component[index]
        if character == "%":
            hex_pair = component[index + 1 : index + 3]
            if len(hex_pair) != 2 or any(
                digit not in _LOWER_HEX_DIGITS for digit in hex_pair
            ):
                raise ValueError("invalid encoded Google Chat resource ID")
            decoded.append(int(hex_pair, 16))
            index += 3
            continue
        if ord(character) not in _SESSION_COMPONENT_SAFE_BYTES:
            raise ValueError("invalid encoded Google Chat resource ID")
        decoded.append(ord(character))
        index += 1

    try:
        resource_id = decoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid encoded Google Chat resource ID") from error
    if _encode_session_component(resource_id) != component:
        raise ValueError("non-canonical encoded Google Chat resource ID")
    if re.fullmatch(_RESOURCE_ID, resource_id) is None:
        raise ValueError("invalid decoded Google Chat resource ID")
    return resource_id


def _session_key(space_id: str, thread_id: str) -> str:
    session_key = (
        f"{SESSION_KEY_PREFIX}{_encode_session_component(space_id)}:"
        f"{_encode_session_component(thread_id)}"
    )
    if len(session_key) > MAX_SESSION_KEY_LENGTH:
        raise ValueError("deterministic Google Chat session key is too long")
    return session_key


def normalize_space_name(space: str) -> str:
    """Return one validated canonical Google Chat Space resource name."""

    return f"spaces/{_space_id(space)}"


def normalize_thread_name(space: str, thread: str) -> str:
    """Return a canonical thread resource belonging to ``space``."""

    space_id = _space_id(space)
    thread_space_id, thread_id = _thread_ids(thread)
    if thread_space_id != space_id:
        raise ValueError("Google Chat thread does not belong to the event space")
    return f"spaces/{space_id}/threads/{thread_id}"


@dataclass(frozen=True, slots=True)
class ChatSessionContext:
    """Deterministic OpenClaw identity and reply route for one Chat context."""

    space: str
    thread: str
    reply_thread: str
    session_key: str
    is_direct_message: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.is_direct_message, bool):
            raise TypeError("is_direct_message must be a boolean")
        normalized_space = normalize_space_name(self.space)
        normalized_thread = normalize_thread_name(self.space, self.thread)
        if self.space != normalized_space:
            raise ValueError("Google Chat space must be canonical")
        if self.thread != normalized_thread:
            raise ValueError("Google Chat thread must be canonical")
        if not isinstance(self.reply_thread, str):
            raise TypeError("reply_thread must be a string")
        expected_reply_thread = "" if self.is_direct_message else normalized_thread
        if self.reply_thread != expected_reply_thread:
            raise ValueError("reply_thread does not match the Chat context route")
        if not isinstance(self.session_key, str):
            raise TypeError("session_key must be a string")
        space_id = _space_id(normalized_space)
        _, thread_id = _thread_ids(normalized_thread)
        expected_session_key = _session_key(space_id, thread_id)
        if self.session_key != expected_session_key:
            raise ValueError("session_key does not match the Chat thread identity")

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

        Google Chat's full ``message.thread.name`` is the identity for direct
        messages, top-level Space messages, and explicit thread replies alike.
        ``threadReply`` is validated as event metadata but never changes the
        identity. Direct messages omit the reply target because Chat DMs are
        flat; every non-DM message replies to its canonical thread resource.
        """

        if not isinstance(is_direct_message, bool):
            raise TypeError("is_direct_message must be a boolean")
        if thread_reply is not None and not isinstance(thread_reply, bool):
            raise TypeError("thread_reply must be a boolean or None")
        normalized_space = normalize_space_name(space)
        normalized_thread = normalize_thread_name(normalized_space, thread)
        space_id = _space_id(normalized_space)
        _, thread_id = _thread_ids(normalized_thread)
        return cls(
            space=normalized_space,
            thread=normalized_thread,
            reply_thread="" if is_direct_message else normalized_thread,
            session_key=_session_key(space_id, thread_id),
            is_direct_message=is_direct_message,
        )

    @classmethod
    def for_thread(cls, space: str, thread: str) -> ChatSessionContext:
        """Build a thread context explicitly, including a new `/new` target."""

        normalized_space = normalize_space_name(space)
        normalized_thread = normalize_thread_name(normalized_space, thread)
        space_id = _space_id(normalized_space)
        _, thread_id = _thread_ids(normalized_thread)
        return cls(
            space=normalized_space,
            thread=normalized_thread,
            reply_thread=normalized_thread,
            session_key=_session_key(space_id, thread_id),
            is_direct_message=False,
        )

    @classmethod
    def from_session_key(cls, session_key: str) -> ChatSessionContext:
        """Recover the deterministic Chat route embedded in a session key."""

        if not isinstance(session_key, str):
            raise TypeError("session_key must be a string")
        normalized_key = session_key.strip()
        if len(normalized_key) > MAX_SESSION_KEY_LENGTH:
            raise ValueError("deterministic Google Chat session key is too long")
        match = _SESSION_KEY_RE.fullmatch(normalized_key)
        if match is None:
            raise ValueError("invalid deterministic Google Chat session key")

        space_id = _decode_session_component(match.group("space_component"))
        thread_id = _decode_session_component(match.group("thread_component"))
        if thread_id == _LEGACY_MAIN_CONTEXT:
            raise ValueError(
                "legacy main session context is not a Chat thread identity"
            )
        space = f"spaces/{space_id}"
        thread = f"{space}/threads/{thread_id}"
        return cls(
            space=space,
            thread=thread,
            reply_thread=thread,
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
    "MAX_SESSION_KEY_LENGTH",
    "SESSION_AGENT",
    "SESSION_CHANNEL",
    "SESSION_KEY_PREFIX",
    "ChatSessionContext",
    "derive_session_key",
    "normalize_space_name",
    "normalize_thread_name",
    "parse_session_key",
]
