from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

_RESOURCE_SEGMENT = r"[^\s/\x00-\x1f\x7f]+"
_USER_NAME_RE = re.compile(rf"^users/{_RESOURCE_SEGMENT}$")
_SPACE_NAME_RE = re.compile(rf"^spaces/{_RESOURCE_SEGMENT}$")
_MESSAGE_NAME_RE = re.compile(
    rf"^(?P<space>spaces/{_RESOURCE_SEGMENT})/messages/{_RESOURCE_SEGMENT}$"
)
_THREAD_NAME_RE = re.compile(
    rf"^(?P<space>spaces/{_RESOURCE_SEGMENT})/threads/{_RESOURCE_SEGMENT}$"
)
_HISTORY_ACTION_HANDLE = "historyActionHandle"


class ChatEventKind(str, Enum):
    """The event families understood by the webhook router."""

    MESSAGE = "MESSAGE"
    BUTTON_CLICK = "BUTTON_CLICK"
    UNKNOWN = "UNKNOWN"


class ChatEventValidationCode(str, Enum):
    """Safe, stable categories for malformed event diagnostics."""

    MALFORMED_EVENT = "malformed_event"
    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    INVALID_RESOURCE_NAME = "invalid_resource_name"
    SPACE_MISMATCH = "space_mismatch"
    AMBIGUOUS_PAYLOAD = "ambiguous_payload"


class ChatEventValidationError(ValueError):
    """Raised when a recognized event cannot be normalized safely.

    The error message deliberately contains only a category and a field path,
    never the rejected value.  Callers can therefore use it in sanitized logs.
    """

    def __init__(self, code: ChatEventValidationCode, field_path: str) -> None:
        self.code = code
        self.field_path = field_path
        super().__init__(f"{code.value}: {field_path}")


def _empty_parameters() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """Normalized input with no authorization fallback to display names.

    ``display_name`` exists only to preserve the legacy provider/presentation
    behavior.  Authorization must use ``actor_name`` exclusively.
    """

    kind: ChatEventKind
    actor_name: str | None = None
    display_name: str | None = None
    space_name: str | None = None
    message_name: str | None = None
    thread_name: str | None = None
    event_time: str | None = None
    text: str | None = None
    attachments: tuple[Mapping[str, Any], ...] = ()
    quoted_message: Mapping[str, Any] | None = None
    action_parameters: Mapping[str, str] = field(default_factory=_empty_parameters)
    platform: str | None = None
    is_legacy: bool = False


def _error(code: ChatEventValidationCode, field_path: str) -> None:
    raise ChatEventValidationError(code, field_path)


def _require_object(
    container: Mapping[str, Any], key: str, field_path: str
) -> Mapping[str, Any]:
    if key not in container:
        _error(ChatEventValidationCode.MISSING_FIELD, field_path)
    value = container[key]
    if not isinstance(value, Mapping):
        _error(ChatEventValidationCode.INVALID_FIELD, field_path)
    return value


def _optional_object(
    container: Mapping[str, Any], key: str, field_path: str
) -> Mapping[str, Any] | None:
    if key not in container:
        return None
    value = container[key]
    if not isinstance(value, Mapping):
        _error(ChatEventValidationCode.INVALID_FIELD, field_path)
    return value


def _require_string(container: Mapping[str, Any], key: str, field_path: str) -> str:
    if key not in container:
        _error(ChatEventValidationCode.MISSING_FIELD, field_path)
    value = container[key]
    if not isinstance(value, str) or not value:
        _error(ChatEventValidationCode.INVALID_FIELD, field_path)
    return value


def _optional_string(
    container: Mapping[str, Any], key: str, field_path: str
) -> str | None:
    if key not in container:
        return None
    value = container[key]
    if not isinstance(value, str) or not value:
        _error(ChatEventValidationCode.INVALID_FIELD, field_path)
    return value


def _optional_display_name(user: Mapping[str, Any] | None) -> str | None:
    if user is None:
        return None
    value = user.get("displayName")
    return value if isinstance(value, str) and value else None


def _require_resource_name(
    container: Mapping[str, Any],
    key: str,
    field_path: str,
    pattern: re.Pattern[str],
) -> str:
    value = _require_string(container, key, field_path)
    if pattern.fullmatch(value) is None:
        _error(ChatEventValidationCode.INVALID_RESOURCE_NAME, field_path)
    return value


def _validate_child_name(
    resource_name: str,
    space_name: str,
    field_path: str,
    pattern: re.Pattern[str],
) -> None:
    match = pattern.fullmatch(resource_name)
    if match is None:
        _error(ChatEventValidationCode.INVALID_RESOURCE_NAME, field_path)
    if match.group("space") != space_name:
        _error(ChatEventValidationCode.SPACE_MISMATCH, field_path)


def _extract_attachment_list(
    message: Mapping[str, Any], key: str, field_path: str
) -> list[Mapping[str, Any]]:
    attachments = message.get(key)
    if attachments is None:
        return []
    if not isinstance(attachments, list):
        _error(ChatEventValidationCode.INVALID_FIELD, field_path)
    normalized: list[Mapping[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            _error(ChatEventValidationCode.INVALID_FIELD, field_path)
        # Incoming Flask JSON consists of ordinary dict/list/scalar values.  A
        # shallow copy prevents later mutation of the top-level attachment.
        normalized.append(MappingProxyType(dict(attachment)))
    return normalized


def _extract_attachments(
    message: Mapping[str, Any], prefix: str
) -> tuple[Mapping[str, Any], ...]:
    attachments = _extract_attachment_list(
        message, "attachment", f"{prefix}.attachment"
    )
    gifs = _extract_attachment_list(message, "attachedGifs", f"{prefix}.attachedGifs")
    for gif in gifs:
        sticker = dict(gif)
        sticker["isSticker"] = True
        attachments.append(MappingProxyType(sticker))
    return tuple(attachments)


def _extract_message_text(message: Mapping[str, Any], prefix: str) -> str:
    if "argumentText" in message:
        argument_text = message["argumentText"]
        if not isinstance(argument_text, str):
            _error(ChatEventValidationCode.INVALID_FIELD, f"{prefix}.argumentText")
        if argument_text:
            return argument_text

    text = message.get("text", "")
    if not isinstance(text, str):
        _error(ChatEventValidationCode.INVALID_FIELD, f"{prefix}.text")
    return text


def _extract_quoted_message(
    message: Mapping[str, Any], prefix: str
) -> Mapping[str, Any] | None:
    metadata = message.get("quotedMessageMetadata")
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        _error(
            ChatEventValidationCode.INVALID_FIELD,
            f"{prefix}.quotedMessageMetadata",
        )

    snapshot = metadata.get("quotedMessageSnapshot")
    if snapshot is None:
        return None
    if not isinstance(snapshot, Mapping):
        _error(
            ChatEventValidationCode.INVALID_FIELD,
            f"{prefix}.quotedMessageMetadata.quotedMessageSnapshot",
        )
    text = snapshot.get("text", "")
    if not isinstance(text, str):
        _error(
            ChatEventValidationCode.INVALID_FIELD,
            f"{prefix}.quotedMessageMetadata.quotedMessageSnapshot.text",
        )
    if not text:
        return None
    return MappingProxyType({"sender": snapshot.get("sender", ""), "text": text})


def _extract_thread_name(
    message: Mapping[str, Any], space_name: str, prefix: str
) -> str | None:
    thread = _optional_object(message, "thread", f"{prefix}.thread")
    if thread is None:
        return None
    thread_name = _require_string(thread, "name", f"{prefix}.thread.name")
    _validate_child_name(
        thread_name,
        space_name,
        f"{prefix}.thread.name",
        _THREAD_NAME_RE,
    )
    return thread_name


def _extract_addon_context(
    root: Mapping[str, Any], chat: Mapping[str, Any]
) -> tuple[str, str | None, str | None, str | None, Mapping[str, Any]]:
    user = _require_object(chat, "user", "chat.user")
    actor_name = _require_resource_name(user, "name", "chat.user.name", _USER_NAME_RE)
    event_time = _optional_string(chat, "eventTime", "chat.eventTime")

    common = _optional_object(
        root, "commonEventObject", "commonEventObject"
    ) or MappingProxyType({})
    platform = _optional_string(common, "platform", "commonEventObject.platform")
    return actor_name, _optional_display_name(user), event_time, platform, common


def _extract_addon_space(
    chat: Mapping[str, Any], payload: Mapping[str, Any], payload_path: str
) -> str:
    payload_space = _require_object(payload, "space", f"{payload_path}.space")
    space_name = _require_resource_name(
        payload_space,
        "name",
        f"{payload_path}.space.name",
        _SPACE_NAME_RE,
    )

    chat_space = _optional_object(chat, "space", "chat.space")
    if chat_space is not None:
        chat_space_name = _require_resource_name(
            chat_space, "name", "chat.space.name", _SPACE_NAME_RE
        )
        if chat_space_name != space_name:
            _error(ChatEventValidationCode.SPACE_MISMATCH, "chat.space.name")
    return space_name


def _normalize_addon_message(
    root: Mapping[str, Any],
    chat: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> ChatEvent:
    actor_name, display_name, event_time, platform, _ = _extract_addon_context(
        root, chat
    )
    payload_path = "chat.messagePayload"
    space_name = _extract_addon_space(chat, payload, payload_path)
    message = _require_object(payload, "message", f"{payload_path}.message")
    message_name = _require_string(message, "name", f"{payload_path}.message.name")
    _validate_child_name(
        message_name,
        space_name,
        f"{payload_path}.message.name",
        _MESSAGE_NAME_RE,
    )

    message_path = f"{payload_path}.message"

    return ChatEvent(
        kind=ChatEventKind.MESSAGE,
        actor_name=actor_name,
        display_name=display_name,
        space_name=space_name,
        message_name=message_name,
        thread_name=_extract_thread_name(message, space_name, message_path),
        event_time=event_time,
        text=_extract_message_text(message, message_path),
        attachments=_extract_attachments(message, message_path),
        quoted_message=_extract_quoted_message(message, message_path),
        platform=platform,
    )


def _extract_action_parameters(common: Mapping[str, Any]) -> Mapping[str, str]:
    parameters = _require_object(common, "parameters", "commonEventObject.parameters")
    handle = _require_string(
        parameters,
        _HISTORY_ACTION_HANDLE,
        f"commonEventObject.parameters.{_HISTORY_ACTION_HANDLE}",
    )
    return MappingProxyType({_HISTORY_ACTION_HANDLE: handle})


def _normalize_addon_button(
    root: Mapping[str, Any],
    chat: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> ChatEvent:
    actor_name, display_name, event_time, platform, common = _extract_addon_context(
        root, chat
    )
    payload_path = "chat.buttonClickedPayload"
    space_name = _extract_addon_space(chat, payload, payload_path)
    message = _require_object(payload, "message", f"{payload_path}.message")
    message_name = _require_string(message, "name", f"{payload_path}.message.name")
    _validate_child_name(
        message_name,
        space_name,
        f"{payload_path}.message.name",
        _MESSAGE_NAME_RE,
    )

    return ChatEvent(
        kind=ChatEventKind.BUTTON_CLICK,
        actor_name=actor_name,
        display_name=display_name,
        space_name=space_name,
        message_name=message_name,
        event_time=event_time,
        action_parameters=_extract_action_parameters(common),
        platform=platform,
    )


def _normalize_legacy_message(
    root: Mapping[str, Any], message: Mapping[str, Any]
) -> ChatEvent:
    root_space = _optional_object(root, "space", "space")
    message_space = _optional_object(message, "space", "message.space")
    if root_space:
        space_name = _require_resource_name(
            root_space, "name", "space.name", _SPACE_NAME_RE
        )
    elif message_space:
        space_name = _require_resource_name(
            message_space, "name", "message.space.name", _SPACE_NAME_RE
        )
    else:
        _error(ChatEventValidationCode.MISSING_FIELD, "message.space.name")

    if root_space and message_space:
        message_space_name = _require_resource_name(
            message_space, "name", "message.space.name", _SPACE_NAME_RE
        )
        if message_space_name != space_name:
            _error(ChatEventValidationCode.SPACE_MISMATCH, "message.space.name")

    actor_name: str | None = None
    user = _optional_object(root, "user", "user")
    sender = _optional_object(message, "sender", "message.sender")
    if user is not None and "name" in user:
        actor_name = _require_resource_name(user, "name", "user.name", _USER_NAME_RE)
    elif sender is not None and "name" in sender:
        actor_name = _require_resource_name(
            sender, "name", "message.sender.name", _USER_NAME_RE
        )

    presentation_user = user if user else sender

    message_name = _optional_string(message, "name", "message.name")
    if message_name is not None:
        _validate_child_name(message_name, space_name, "message.name", _MESSAGE_NAME_RE)

    return ChatEvent(
        kind=ChatEventKind.MESSAGE,
        actor_name=actor_name,
        display_name=_optional_display_name(presentation_user),
        space_name=space_name,
        message_name=message_name,
        thread_name=_extract_thread_name(message, space_name, "message"),
        event_time=_optional_string(root, "eventTime", "eventTime"),
        text=_extract_message_text(message, "message"),
        attachments=_extract_attachments(message, "message"),
        quoted_message=_extract_quoted_message(message, "message"),
        is_legacy=True,
    )


def normalize_chat_event(data: Mapping[str, Any]) -> ChatEvent:
    """Normalize an authenticated Google Chat webhook JSON object.

    Workspace Add-on payloads take precedence over the compatibility fallback
    for legacy Chat message events.  Once an Add-on family is recognized,
    malformed required fields fail closed and never downgrade to legacy data.
    """

    if not isinstance(data, Mapping):
        _error(ChatEventValidationCode.MALFORMED_EVENT, "$event")

    chat = _optional_object(data, "chat", "chat")
    if chat is not None:
        has_message = "messagePayload" in chat
        has_button = "buttonClickedPayload" in chat
        if has_message and has_button:
            _error(ChatEventValidationCode.AMBIGUOUS_PAYLOAD, "chat")
        if has_message:
            payload = _require_object(chat, "messagePayload", "chat.messagePayload")
            return _normalize_addon_message(data, chat, payload)
        if has_button:
            payload = _require_object(
                chat, "buttonClickedPayload", "chat.buttonClickedPayload"
            )
            return _normalize_addon_button(data, chat, payload)

    legacy_message = _optional_object(data, "message", "message")
    if legacy_message is not None:
        return _normalize_legacy_message(data, legacy_message)

    return ChatEvent(kind=ChatEventKind.UNKNOWN)
