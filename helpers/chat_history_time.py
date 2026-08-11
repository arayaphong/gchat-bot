from __future__ import annotations

import re
import unicodedata
from enum import Enum


class HistoryCommandKind(str, Enum):
    """Route-level classification of the reserved ``/chat`` namespace."""

    NOT_HISTORY = "NOT_HISTORY"
    STATS = "STATS"
    CLEAR = "CLEAR"
    INVALID_HISTORY = "INVALID_HISTORY"


_STATS_RE = re.compile(r"^[ \t]*/chat[ \t]*$")
_CLEAR_ENVELOPE_RE = re.compile(r"^[ \t]*/chat[ \t]+clear[ \t]+[^ \t\r\n]+[ \t]*$")


def _trim_unicode_whitespace(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return text[start:end]


def _has_disallowed_command_character(text: str) -> bool:
    for character in text:
        if character.isspace() and character not in {" ", "\t"}:
            return True
        if unicodedata.category(character) == "Cc" and character != "\t":
            return True
    return False


def recognize_history_command(raw_text: str) -> HistoryCommandKind:
    """Classify route ownership without parsing a time argument.

    ``CLEAR`` means that the command has the strict, one-token clear envelope;
    Phase 2 remains responsible for deciding whether that token is a valid
    duration or timestamp.  Every reserved but malformed form returns
    ``INVALID_HISTORY`` so it cannot fall through to a provider.
    """

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")

    detected = _trim_unicode_whitespace(raw_text)
    if len(detected) < 5 or detected[:5].casefold() != "/chat":
        return HistoryCommandKind.NOT_HISTORY

    if len(detected) > 5:
        boundary = detected[5]
        if boundary.isalpha() or boundary.isdecimal() or boundary == "_":
            return HistoryCommandKind.NOT_HISTORY

    if _has_disallowed_command_character(raw_text):
        return HistoryCommandKind.INVALID_HISTORY
    if _STATS_RE.fullmatch(raw_text) is not None:
        return HistoryCommandKind.STATS
    if _CLEAR_ENVELOPE_RE.fullmatch(raw_text) is not None:
        return HistoryCommandKind.CLEAR
    return HistoryCommandKind.INVALID_HISTORY
