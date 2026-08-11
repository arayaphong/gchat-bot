"""Canonical Google OAuth scopes used by the application.

Keep scope declarations in this module so interactive token generation and
runtime credential loading cannot silently drift apart.
"""

from __future__ import annotations

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CHAT_MESSAGES_SCOPE = "https://www.googleapis.com/auth/chat.messages"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"

DRIVE_SCOPES = (DRIVE_READONLY_SCOPE, DRIVE_FILE_SCOPE)
USER_SCOPES = (*DRIVE_SCOPES, CHAT_MESSAGES_SCOPE)
BOT_SCOPES = (CHAT_BOT_SCOPE,)

__all__ = [
    "BOT_SCOPES",
    "CHAT_BOT_SCOPE",
    "CHAT_MESSAGES_SCOPE",
    "DRIVE_FILE_SCOPE",
    "DRIVE_READONLY_SCOPE",
    "DRIVE_SCOPES",
    "USER_SCOPES",
]
