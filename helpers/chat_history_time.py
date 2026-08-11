from __future__ import annotations

import calendar
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from typing import NoReturn
from zoneinfo import ZoneInfo

MAX_TIME_ARGUMENT_LENGTH = 64
MAX_DURATION_COMPONENTS = 6
DISPLAY_TIMEZONE = "Asia/Bangkok"

_BANGKOK = ZoneInfo(DISPLAY_TIMEZONE)
_UTC = timezone.utc


class HistoryCommandKind(str, Enum):
    """The route-level history command families."""

    NOT_HISTORY = "NOT_HISTORY"
    STATS = "STATS"
    CLEAR = "CLEAR"
    INVALID_HISTORY = "INVALID_HISTORY"


class HistoryTimeArgumentKind(str, Enum):
    """The two cutoff argument grammars accepted by ``/chat clear``."""

    RELATIVE = "RELATIVE"
    ABSOLUTE = "ABSOLUTE"


class HistoryCommandErrorCode(str, Enum):
    """Stable error categories safe to expose in diagnostics."""

    INVALID_SYNTAX = "invalid_syntax"
    INVALID_DATETIME = "invalid_datetime"
    INVALID_EVENT_TIME = "invalid_event_time"
    INVALID_REFERENCE_TIME = "invalid_reference_time"
    FUTURE_CUTOFF = "future_cutoff"
    ARGUMENT_TOO_LONG = "argument_too_long"
    TOO_MANY_COMPONENTS = "too_many_components"
    TIME_OVERFLOW = "time_overflow"


class HistoryCommandError(ValueError):
    """A sanitized command/time error that never contains rejected input."""

    def __init__(self, code: HistoryCommandErrorCode, field_path: str) -> None:
        self.code = code
        self.field_path = field_path
        super().__init__(f"{code.value}: {field_path}")


@dataclass(frozen=True, slots=True)
class ParsedHistoryCommand:
    """A normalized command ready to persist without reparsing wall-clock time."""

    kind: HistoryCommandKind
    argument_kind: HistoryTimeArgumentKind | None
    normalized_argument: str | None
    reference_time_utc: str | None
    cutoff_utc: str | None
    reference_time_display: str | None
    cutoff_display: str | None
    display_timezone: str


@dataclass(frozen=True, slots=True)
class _DurationComponent:
    value: int
    unit: str


_STATS_RE = re.compile(r"^[ \t]*/chat[ \t]*$")
_CLEAR_COMMAND_RE = re.compile(
    r"^[ \t]*/chat[ \t]+clear[ \t]+(?P<argument>[^ \t\r\n]+)[ \t]*$"
)
_DURATION_COMPONENT_RE = re.compile(r"(?P<value>[0-9]+)(?P<unit>mo|y|w|d|h|m)")
_DURATION_UNIT_ORDER = {"y": 0, "mo": 1, "w": 2, "d": 3, "h": 4, "m": 5}
_ABSOLUTE_RE = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"(?:T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?"
    r"(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})?"
    r")?"
    r")?$"
)
_EVENT_TIME_RE = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?"
    r"(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})$"
)


def _raise(code: HistoryCommandErrorCode, field_path: str) -> NoReturn:
    raise HistoryCommandError(code, field_path)


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
        if character != "\t" and unicodedata.category(character).startswith("C"):
            return True
    return False


def recognize_history_command(raw_text: str) -> HistoryCommandKind:
    """Classify route ownership without parsing cutoff semantics."""

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
    if _CLEAR_COMMAND_RE.fullmatch(raw_text) is not None:
        return HistoryCommandKind.CLEAR
    return HistoryCommandKind.INVALID_HISTORY


def _parse_offset(raw_offset: str, *, error_code: HistoryCommandErrorCode) -> tzinfo:
    if raw_offset == "Z":
        return _UTC

    sign = raw_offset[0]
    hours = int(raw_offset[1:3])
    minutes = int(raw_offset[4:6])
    if minutes > 59 or hours > 14 or (hours == 14 and minutes != 0):
        _raise(error_code, "offset")
    if sign == "-" and hours == 0 and minutes == 0:
        _raise(error_code, "offset")

    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if sign == "+" else -delta)


def _to_utc(
    value: datetime, *, error_code: HistoryCommandErrorCode, field_path: str
) -> datetime:
    try:
        return value.astimezone(_UTC)
    except (OverflowError, ValueError):
        _raise(error_code, field_path)


def _parse_absolute_argument(argument: str) -> datetime | None:
    match = _ABSOLUTE_RE.fullmatch(argument)
    if match is None:
        return None

    second_text = match.group("second")
    fraction = match.group("fraction") or ""
    offset_text = match.group("offset")
    if second_text is None and (fraction or offset_text):
        _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "argument")

    zone: tzinfo = _BANGKOK
    if offset_text is not None:
        zone = _parse_offset(
            offset_text,
            error_code=HistoryCommandErrorCode.INVALID_DATETIME,
        )

    try:
        value = datetime(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
            hour=int(match.group("hour") or 0),
            minute=int(match.group("minute") or 0),
            second=int(second_text or 0),
            microsecond=int(fraction.ljust(6, "0")) if fraction else 0,
            tzinfo=zone,
        )
    except ValueError:
        _raise(HistoryCommandErrorCode.INVALID_DATETIME, "argument")

    return _to_utc(
        value,
        error_code=HistoryCommandErrorCode.TIME_OVERFLOW,
        field_path="argument",
    )


def _parse_duration_argument(
    argument: str,
) -> tuple[tuple[_DurationComponent, ...], str]:
    components: list[_DurationComponent] = []
    position = 0
    previous_order = -1

    while position < len(argument):
        match = _DURATION_COMPONENT_RE.match(argument, position)
        if match is None:
            _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "argument")
        position = match.end()
        value = int(match.group("value"))
        unit = match.group("unit")
        components.append(_DurationComponent(value=value, unit=unit))

        if len(components) > MAX_DURATION_COMPONENTS:
            _raise(HistoryCommandErrorCode.TOO_MANY_COMPONENTS, "argument")
        unit_order = _DURATION_UNIT_ORDER[unit]
        if value <= 0 or unit_order <= previous_order:
            _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "argument")
        previous_order = unit_order

    if not components:
        _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "argument")

    normalized = "".join(
        f"{component.value}{component.unit}" for component in components
    )
    return tuple(components), normalized


def _parse_event_time(event_time: object) -> datetime:
    if not isinstance(event_time, str) or len(event_time) > MAX_TIME_ARGUMENT_LENGTH:
        _raise(HistoryCommandErrorCode.INVALID_EVENT_TIME, "event_time")
    match = _EVENT_TIME_RE.fullmatch(event_time)
    if match is None:
        _raise(HistoryCommandErrorCode.INVALID_EVENT_TIME, "event_time")

    fraction = match.group("fraction") or ""
    try:
        zone = _parse_offset(
            match.group("offset"),
            error_code=HistoryCommandErrorCode.INVALID_EVENT_TIME,
        )
        value = datetime(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=int(match.group("second")),
            # Protobuf timestamps may contain nanoseconds. Truncating the last
            # three digits moves toward the earlier representable instant.
            microsecond=int(fraction[:6].ljust(6, "0")) if fraction else 0,
            tzinfo=zone,
        )
    except HistoryCommandError:
        raise
    except (OverflowError, ValueError):
        _raise(HistoryCommandErrorCode.INVALID_EVENT_TIME, "event_time")

    return _to_utc(
        value,
        error_code=HistoryCommandErrorCode.INVALID_EVENT_TIME,
        field_path="event_time",
    )


def _resolve_reference_time(
    event_time: object | None,
    clock: Callable[[], datetime] | None,
) -> datetime:
    if event_time is not None:
        return _parse_event_time(event_time)

    clock_function = clock if clock is not None else lambda: datetime.now(_UTC)
    try:
        captured = clock_function()
        if not isinstance(captured, datetime):
            _raise(HistoryCommandErrorCode.INVALID_REFERENCE_TIME, "clock")
        if captured.tzinfo is None or captured.utcoffset() is None:
            _raise(HistoryCommandErrorCode.INVALID_REFERENCE_TIME, "clock")
        return _to_utc(
            captured,
            error_code=HistoryCommandErrorCode.INVALID_REFERENCE_TIME,
            field_path="clock",
        )
    except HistoryCommandError:
        raise
    except Exception:  # noqa: BLE001 - external clock failures become safe errors
        _raise(HistoryCommandErrorCode.INVALID_REFERENCE_TIME, "clock")


def _subtract_duration(
    reference_utc: datetime, components: tuple[_DurationComponent, ...]
) -> datetime:
    values = {component.unit: component.value for component in components}
    reference_local = reference_utc.astimezone(_BANGKOK)

    try:
        total_months = values.get("y", 0) * 12 + values.get("mo", 0)
        if total_months:
            source_month_index = (reference_local.year - 1) * 12 + (
                reference_local.month - 1
            )
            target_month_index = source_month_index - total_months
            if target_month_index < 0:
                _raise(HistoryCommandErrorCode.TIME_OVERFLOW, "argument")
            target_year_offset, target_month_offset = divmod(target_month_index, 12)
            target_year = target_year_offset + 1
            target_month = target_month_offset + 1
            target_day = min(
                reference_local.day,
                calendar.monthrange(target_year, target_month)[1],
            )
            reference_local = reference_local.replace(
                year=target_year,
                month=target_month,
                day=target_day,
            )

        fixed_delta = timedelta(
            weeks=values.get("w", 0),
            days=values.get("d", 0),
            hours=values.get("h", 0),
            minutes=values.get("m", 0),
        )
        return (reference_local - fixed_delta).astimezone(_UTC)
    except HistoryCommandError:
        raise
    except (OverflowError, ValueError):
        _raise(HistoryCommandErrorCode.TIME_OVERFLOW, "argument")


def _format_utc(value: datetime) -> str:
    utc = value.astimezone(_UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )


def _format_bangkok(value: datetime, *, field_path: str) -> str:
    try:
        local = value.astimezone(_BANGKOK)
    except (OverflowError, ValueError):
        _raise(HistoryCommandErrorCode.TIME_OVERFLOW, field_path)
    return (
        f"{local.year:04d}-{local.month:02d}-{local.day:02d} "
        f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}."
        f"{local.microsecond:06d} {DISPLAY_TIMEZONE}"
    )


def parse_history_command(
    raw_text: object,
    *,
    event_time: object | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ParsedHistoryCommand:
    """Parse one history command and resolve a deterministic clear cutoff.

    The server clock is captured exactly once, and only for a valid clear
    command whose event does not include ``chat.eventTime``.
    """

    if not isinstance(raw_text, str):
        _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "command")

    kind = recognize_history_command(raw_text)
    if kind is HistoryCommandKind.STATS:
        return ParsedHistoryCommand(
            kind=kind,
            argument_kind=None,
            normalized_argument=None,
            reference_time_utc=None,
            cutoff_utc=None,
            reference_time_display=None,
            cutoff_display=None,
            display_timezone=DISPLAY_TIMEZONE,
        )
    if kind is not HistoryCommandKind.CLEAR:
        _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "command")

    command_match = _CLEAR_COMMAND_RE.fullmatch(raw_text)
    if command_match is None:  # Defensive: recognizer and parser share the regex.
        _raise(HistoryCommandErrorCode.INVALID_SYNTAX, "command")
    argument = command_match.group("argument")
    if len(argument) > MAX_TIME_ARGUMENT_LENGTH:
        _raise(HistoryCommandErrorCode.ARGUMENT_TOO_LONG, "argument")

    absolute_cutoff = _parse_absolute_argument(argument)
    duration_components: tuple[_DurationComponent, ...] = ()
    if absolute_cutoff is not None:
        argument_kind = HistoryTimeArgumentKind.ABSOLUTE
        normalized_argument = argument
    else:
        duration_components, normalized_argument = _parse_duration_argument(argument)
        argument_kind = HistoryTimeArgumentKind.RELATIVE

    reference_utc = _resolve_reference_time(event_time, clock)
    cutoff_utc = (
        absolute_cutoff
        if absolute_cutoff is not None
        else _subtract_duration(reference_utc, duration_components)
    )
    if cutoff_utc > reference_utc:
        _raise(HistoryCommandErrorCode.FUTURE_CUTOFF, "argument")

    return ParsedHistoryCommand(
        kind=kind,
        argument_kind=argument_kind,
        normalized_argument=normalized_argument,
        reference_time_utc=_format_utc(reference_utc),
        cutoff_utc=_format_utc(cutoff_utc),
        reference_time_display=_format_bangkok(
            reference_utc,
            field_path="reference_time",
        ),
        cutoff_display=_format_bangkok(cutoff_utc, field_path="argument"),
        display_timezone=DISPLAY_TIMEZONE,
    )


__all__ = [
    "DISPLAY_TIMEZONE",
    "MAX_DURATION_COMPONENTS",
    "MAX_TIME_ARGUMENT_LENGTH",
    "HistoryCommandError",
    "HistoryCommandErrorCode",
    "HistoryCommandKind",
    "HistoryTimeArgumentKind",
    "ParsedHistoryCommand",
    "parse_history_command",
    "recognize_history_command",
]
