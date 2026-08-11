from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

HISTORY_ENABLED_ENV = "GCHAT_HISTORY_ENABLED"
HISTORY_DELETE_ENABLED_ENV = "GCHAT_HISTORY_DELETE_ENABLED"
HISTORY_ALLOWED_USER_ENV = "GCHAT_HISTORY_ALLOWED_USER"
HISTORY_ALLOWED_SPACE_ENV = "GCHAT_HISTORY_ALLOWED_SPACE"
OUTBOUND_SPACE_ENV = "GCHAT_OUTBOUND_SPACE"
CARD_ACTION_URL_ENV = "GCHAT_CARD_ACTION_URL"
HISTORY_STATE_DIR_ENV = "JINX_CHAT_HISTORY_STATE_DIR"

_RESOURCE_ID = r"[A-Za-z0-9][A-Za-z0-9._~-]{0,254}"
_USER_RESOURCE_RE = re.compile(rf"users/(?P<resource_id>{_RESOURCE_ID})\Z")
_SPACE_RESOURCE_RE = re.compile(rf"spaces/(?P<resource_id>{_RESOURCE_ID})\Z")
_UNSTABLE_USER_ALIASES = frozenset({"all", "app", "me"})


class ChatHistorySettingsError(ValueError):
    """Raised when history configuration cannot be used safely."""


def default_chat_history_state_dir() -> Path:
    return Path.home() / ".openclaw" / "state" / "jinx-gchat" / "chat-history"


def chat_history_state_dir_from_env(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the effective private history-state root.

    This helper deliberately does not depend on whether the history feature is
    enabled.  Outbound file policy must continue protecting an existing ledger
    while the feature is disabled or being rolled back.
    """

    values = os.environ if environ is None else environ
    raw = values.get(HISTORY_STATE_DIR_ENV)
    configured = raw.strip() if isinstance(raw, str) else ""
    candidate = (
        Path(configured).expanduser()
        if configured
        else default_chat_history_state_dir()
    )
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise ChatHistorySettingsError(
            f"{HISTORY_STATE_DIR_ENV} is not a usable filesystem path"
        ) from error


def _parse_flag(values: Mapping[str, str], name: str) -> bool:
    raw = values.get(name)
    if raw is None:
        return False
    if not isinstance(raw, str):
        raise ChatHistorySettingsError(f"{name} must be exactly 'true' or 'false'")
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ChatHistorySettingsError(f"{name} must be exactly 'true' or 'false'")


def _parse_resource(
    values: Mapping[str, str],
    name: str,
    pattern: re.Pattern[str],
    *,
    reject_user_aliases: bool = False,
) -> str | None:
    raw = values.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ChatHistorySettingsError(f"{name} must be one canonical resource name")
    if not raw:
        return None
    match = pattern.fullmatch(raw)
    if match is None or (
        reject_user_aliases and match.group("resource_id") in _UNSTABLE_USER_ALIASES
    ):
        raise ChatHistorySettingsError(f"{name} must be one canonical resource name")
    return raw


def _parse_card_action_url(values: Mapping[str, str], *, required: bool) -> str | None:
    raw = values.get(CARD_ACTION_URL_ENV)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ChatHistorySettingsError(
                f"{CARD_ACTION_URL_ENV} is required when history delete is enabled"
            )
        return None
    if not isinstance(raw, str):
        raise ChatHistorySettingsError(
            f"{CARD_ACTION_URL_ENV} must be an absolute HTTPS URL"
        )

    value = raw
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
        raise ChatHistorySettingsError(
            f"{CARD_ACTION_URL_ENV} must be an absolute HTTPS URL"
        )
    if "\\" in value:
        raise ChatHistorySettingsError(
            f"{CARD_ACTION_URL_ENV} must be an absolute HTTPS URL"
        )

    try:
        parsed = urlsplit(value)
        # Accessing port validates malformed and out-of-range port values.
        _ = parsed.port
    except ValueError as error:
        raise ChatHistorySettingsError(
            f"{CARD_ACTION_URL_ENV} must be an absolute HTTPS URL"
        ) from error

    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise ChatHistorySettingsError(
            f"{CARD_ACTION_URL_ENV} must be an absolute HTTPS URL without userinfo or fragment"
        )
    return value


@dataclass(frozen=True, slots=True)
class ChatHistorySettings:
    enabled: bool
    delete_enabled: bool
    allowed_user: str | None
    allowed_space: str | None
    card_action_url: str | None
    state_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ChatHistorySettings:
        values = os.environ if environ is None else environ
        enabled = _parse_flag(values, HISTORY_ENABLED_ENV)
        delete_enabled = _parse_flag(values, HISTORY_DELETE_ENABLED_ENV)
        if delete_enabled and not enabled:
            raise ChatHistorySettingsError(
                f"{HISTORY_DELETE_ENABLED_ENV} requires {HISTORY_ENABLED_ENV}=true"
            )

        allowed_user = _parse_resource(
            values,
            HISTORY_ALLOWED_USER_ENV,
            _USER_RESOURCE_RE,
            reject_user_aliases=True,
        )
        allowed_space = _parse_resource(
            values,
            HISTORY_ALLOWED_SPACE_ENV,
            _SPACE_RESOURCE_RE,
        )
        outbound_space = None
        if OUTBOUND_SPACE_ENV in values:
            outbound_space = _parse_resource(
                values,
                OUTBOUND_SPACE_ENV,
                _SPACE_RESOURCE_RE,
            )
        if allowed_space is None:
            allowed_space = outbound_space
        elif outbound_space is not None and outbound_space != allowed_space:
            raise ChatHistorySettingsError(
                f"{HISTORY_ALLOWED_SPACE_ENV} and {OUTBOUND_SPACE_ENV} must match"
            )

        if enabled and allowed_user is None:
            raise ChatHistorySettingsError(
                f"{HISTORY_ALLOWED_USER_ENV} is required when history is enabled"
            )
        if enabled and allowed_space is None:
            raise ChatHistorySettingsError(
                f"{HISTORY_ALLOWED_SPACE_ENV} is required when history is enabled"
            )

        return cls(
            enabled=enabled,
            delete_enabled=delete_enabled,
            allowed_user=allowed_user,
            allowed_space=allowed_space,
            card_action_url=_parse_card_action_url(
                values,
                required=delete_enabled,
            ),
            state_dir=chat_history_state_dir_from_env(values),
        )


__all__ = [
    "CARD_ACTION_URL_ENV",
    "HISTORY_ALLOWED_SPACE_ENV",
    "HISTORY_ALLOWED_USER_ENV",
    "HISTORY_DELETE_ENABLED_ENV",
    "HISTORY_ENABLED_ENV",
    "HISTORY_STATE_DIR_ENV",
    "OUTBOUND_SPACE_ENV",
    "ChatHistorySettings",
    "ChatHistorySettingsError",
    "chat_history_state_dir_from_env",
    "default_chat_history_state_dir",
]
