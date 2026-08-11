from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from helpers.chat_history_time import (
    DISPLAY_TIMEZONE,
    MAX_DURATION_COMPONENTS,
    MAX_TIME_ARGUMENT_LENGTH,
    HistoryCommandError,
    HistoryCommandErrorCode,
    HistoryCommandKind,
    HistoryTimeArgumentKind,
    parse_history_command,
)

REFERENCE_EVENT_TIME = "2026-08-11T02:30:00.123456Z"


class HistoryTimeTestCase(unittest.TestCase):
    def assert_error(
        self,
        code: HistoryCommandErrorCode,
        raw_text: object,
        *,
        event_time: object | None = REFERENCE_EVENT_TIME,
        clock: Mock | None = None,
    ) -> HistoryCommandError:
        with self.assertRaises(HistoryCommandError) as caught:
            parse_history_command(
                raw_text,
                event_time=event_time,
                clock=clock,
            )
        self.assertIs(caught.exception.code, code)
        return caught.exception


class HistoryCommandParserTests(HistoryTimeTestCase):
    def test_stats_is_typed_and_never_captures_a_clock(self) -> None:
        clock = Mock(side_effect=AssertionError("stats must not read the clock"))

        parsed = parse_history_command(" \t/chat\t", clock=clock)

        self.assertIs(parsed.kind, HistoryCommandKind.STATS)
        self.assertIsNone(parsed.argument_kind)
        self.assertIsNone(parsed.normalized_argument)
        self.assertIsNone(parsed.reference_time_utc)
        self.assertIsNone(parsed.cutoff_utc)
        self.assertIsNone(parsed.reference_time_display)
        self.assertIsNone(parsed.cutoff_display)
        self.assertEqual(parsed.display_timezone, DISPLAY_TIMEZONE)
        clock.assert_not_called()

    def test_clear_accepts_ascii_outer_and_intertoken_whitespace(self) -> None:
        parsed = parse_history_command(
            " \t/chat\tclear  \t001d\t ",
            event_time=REFERENCE_EVENT_TIME,
        )

        self.assertIs(parsed.kind, HistoryCommandKind.CLEAR)
        self.assertIs(parsed.argument_kind, HistoryTimeArgumentKind.RELATIVE)
        self.assertEqual(parsed.normalized_argument, "1d")
        self.assertEqual(
            parsed.reference_time_utc,
            "2026-08-11T02:30:00.123456Z",
        )
        self.assertEqual(parsed.cutoff_utc, "2026-08-10T02:30:00.123456Z")
        self.assertEqual(
            parsed.reference_time_display,
            "2026-08-11 09:30:00.123456 Asia/Bangkok",
        )
        self.assertEqual(
            parsed.cutoff_display,
            "2026-08-10 09:30:00.123456 Asia/Bangkok",
        )

    def test_all_plan_level_invalid_command_examples_are_typed_usage_errors(
        self,
    ) -> None:
        invalid_commands = (
            "/chat clear",
            "/chat clear 1 w",
            "/chat clear 7 วัน",
            "/chat clear 2026-08-01 18:30",
            "/chat clear 1w extra",
            "/chat clear 1W",
            "/chat clear 0m",
            "/chat clear -1d",
            "/Chat",
            "/chat Clear 1w",
            "/chat!",
            "/chat-clear",
            "/chat\u00a0clear 1w",
            "/chat clear 1w\nextra",
        )
        for command in invalid_commands:
            with self.subTest(command=command):
                self.assert_error(HistoryCommandErrorCode.INVALID_SYNTAX, command)

    def test_ordinary_messages_are_not_accepted_by_the_command_parser(self) -> None:
        for text in ("hello", "/chatty", "/chat1", "/chat_foo", "please /chat"):
            with self.subTest(text=text):
                self.assert_error(HistoryCommandErrorCode.INVALID_SYNTAX, text)

    def test_non_string_command_is_a_sanitized_typed_error(self) -> None:
        error = self.assert_error(HistoryCommandErrorCode.INVALID_SYNTAX, None)

        self.assertEqual(error.field_path, "command")
        self.assertNotIn("None", str(error))

    def test_rejected_argument_is_never_echoed_by_an_error(self) -> None:
        sentinel = "SENTINEL-PRIVATE-ARGUMENT"

        error = self.assert_error(
            HistoryCommandErrorCode.INVALID_SYNTAX,
            f"/chat clear {sentinel}",
        )

        self.assertNotIn(sentinel, str(error))

    def test_invalid_argument_is_rejected_before_clock_capture(self) -> None:
        clock = Mock(side_effect=AssertionError("invalid syntax needs no clock"))

        self.assert_error(
            HistoryCommandErrorCode.INVALID_SYNTAX,
            "/chat clear not-a-time",
            event_time=None,
            clock=clock,
        )

        clock.assert_not_called()


class RelativeDurationTests(HistoryTimeTestCase):
    def test_each_supported_unit_and_mixed_examples(self) -> None:
        cases = (
            ("30m", "2026-08-11T02:00:00.123456Z"),
            ("12h", "2026-08-10T14:30:00.123456Z"),
            ("7d", "2026-08-04T02:30:00.123456Z"),
            ("2w", "2026-07-28T02:30:00.123456Z"),
            ("3mo", "2026-05-11T02:30:00.123456Z"),
            ("1y", "2025-08-11T02:30:00.123456Z"),
            ("1w2d", "2026-08-02T02:30:00.123456Z"),
            ("2h30m", "2026-08-11T00:00:00.123456Z"),
            ("1y2mo3d", "2025-06-08T02:30:00.123456Z"),
        )
        for argument, expected_cutoff in cases:
            with self.subTest(argument=argument):
                parsed = parse_history_command(
                    f"/chat clear {argument}",
                    event_time=REFERENCE_EVENT_TIME,
                )
                self.assertIs(
                    parsed.argument_kind,
                    HistoryTimeArgumentKind.RELATIVE,
                )
                self.assertEqual(parsed.cutoff_utc, expected_cutoff)

    def test_all_six_components_are_accepted_in_descending_order(self) -> None:
        parsed = parse_history_command(
            "/chat clear 1y2mo3w4d5h6m",
            event_time=REFERENCE_EVENT_TIME,
        )

        self.assertEqual(MAX_DURATION_COMPONENTS, 6)
        self.assertEqual(parsed.normalized_argument, "1y2mo3w4d5h6m")
        self.assertEqual(parsed.cutoff_utc, "2025-05-16T21:24:00.123456Z")

    def test_leading_zeroes_are_removed_during_normalization(self) -> None:
        parsed = parse_history_command(
            "/chat clear 0001y002mo03d04h005m",
            event_time=REFERENCE_EVENT_TIME,
        )

        self.assertEqual(parsed.normalized_argument, "1y2mo3d4h5m")

    def test_calendar_month_subtraction_clamps_month_end(self) -> None:
        cases = (
            (
                "2024-03-31T05:00:00Z",
                "1mo",
                "2024-02-29T05:00:00.000000Z",
            ),
            (
                "2023-03-31T05:00:00Z",
                "1mo",
                "2023-02-28T05:00:00.000000Z",
            ),
        )
        for event_time, argument, expected in cases:
            with self.subTest(event_time=event_time):
                parsed = parse_history_command(
                    f"/chat clear {argument}",
                    event_time=event_time,
                )
                self.assertEqual(parsed.cutoff_utc, expected)

    def test_years_and_months_are_combined_then_clamped_once(self) -> None:
        parsed = parse_history_command(
            "/chat clear 1y1mo",
            event_time="2024-02-29T05:00:00Z",
        )

        # A single 13-month subtraction preserves day 29 in January. Applying
        # one year and one month sequentially would incorrectly produce day 28.
        self.assertEqual(parsed.cutoff_utc, "2023-01-29T05:00:00.000000Z")

    def test_fixed_units_apply_after_calendar_subtraction(self) -> None:
        parsed = parse_history_command(
            "/chat clear 1mo1d",
            event_time="2024-03-31T05:00:00Z",
        )

        self.assertEqual(parsed.cutoff_utc, "2024-02-28T05:00:00.000000Z")

    def test_duplicate_reversed_zero_and_malformed_durations_are_rejected(
        self,
    ) -> None:
        invalid_arguments = (
            "1y2y",
            "1d1w",
            "1m1h",
            "0m",
            "00d",
            "-1d",
            "+1d",
            "1.5h",
            "1W",
            "1M",
            "1mom",
            "1h!",
            "１h",
            "m",
        )
        for argument in invalid_arguments:
            with self.subTest(argument=argument):
                self.assert_error(
                    HistoryCommandErrorCode.INVALID_SYNTAX,
                    f"/chat clear {argument}",
                )

    def test_component_and_argument_limits_have_distinct_errors(self) -> None:
        self.assert_error(
            HistoryCommandErrorCode.TOO_MANY_COMPONENTS,
            "/chat clear 1y1mo1w1d1h1m1m",
        )

        oversized = "1" * MAX_TIME_ARGUMENT_LENGTH + "m"
        self.assertGreater(len(oversized), MAX_TIME_ARGUMENT_LENGTH)
        self.assert_error(
            HistoryCommandErrorCode.ARGUMENT_TOO_LONG,
            f"/chat clear {oversized}",
        )

    def test_large_calendar_and_fixed_values_fail_as_time_overflow(self) -> None:
        for unit in ("y", "w"):
            with self.subTest(unit=unit):
                argument = f"{'9' * 60}{unit}"
                self.assertLessEqual(len(argument), MAX_TIME_ARGUMENT_LENGTH)
                self.assert_error(
                    HistoryCommandErrorCode.TIME_OVERFLOW,
                    f"/chat clear {argument}",
                )


class AbsoluteTimeTests(HistoryTimeTestCase):
    def test_every_supported_absolute_shape_normalizes_to_canonical_utc(self) -> None:
        cases = (
            ("2026-08-01", "2026-07-31T17:00:00.000000Z"),
            ("2026-08-01T18:30", "2026-08-01T11:30:00.000000Z"),
            ("2026-08-01T18:30:45", "2026-08-01T11:30:45.000000Z"),
            ("2026-08-01T18:30:45.1", "2026-08-01T11:30:45.100000Z"),
            (
                "2026-08-01T18:30:45.123456",
                "2026-08-01T11:30:45.123456Z",
            ),
            (
                "2026-08-01T18:30:00+07:00",
                "2026-08-01T11:30:00.000000Z",
            ),
            (
                "2026-08-01T18:30:00.25+07:00",
                "2026-08-01T11:30:00.250000Z",
            ),
            (
                "2026-08-01T18:30:00-04:30",
                "2026-08-01T23:00:00.000000Z",
            ),
            ("2026-08-01T11:30:00Z", "2026-08-01T11:30:00.000000Z"),
            (
                "2026-08-01T11:30:00.123456Z",
                "2026-08-01T11:30:00.123456Z",
            ),
        )
        for argument, expected_cutoff in cases:
            with self.subTest(argument=argument):
                parsed = parse_history_command(
                    f"/chat clear {argument}",
                    event_time=REFERENCE_EVENT_TIME,
                )
                self.assertIs(
                    parsed.argument_kind,
                    HistoryTimeArgumentKind.ABSOLUTE,
                )
                self.assertEqual(parsed.normalized_argument, argument)
                self.assertEqual(parsed.cutoff_utc, expected_cutoff)

    def test_naive_bangkok_offset_and_z_forms_are_equivalent(self) -> None:
        arguments = (
            "2026-08-01T18:30:00",
            "2026-08-01T18:30:00+07:00",
            "2026-08-01T11:30:00Z",
        )

        cutoffs = {
            parse_history_command(
                f"/chat clear {argument}",
                event_time=REFERENCE_EVENT_TIME,
            ).cutoff_utc
            for argument in arguments
        }

        self.assertEqual(cutoffs, {"2026-08-01T11:30:00.000000Z"})

    def test_offset_boundary_accepts_both_known_fourteen_hour_offsets(self) -> None:
        cases = (
            ("+14:00", "2026-08-01T04:30:00.000000Z"),
            ("-14:00", "2026-08-02T08:30:00.000000Z"),
        )
        for offset, expected in cases:
            with self.subTest(offset=offset):
                parsed = parse_history_command(
                    f"/chat clear 2026-08-01T18:30:00{offset}",
                    event_time=REFERENCE_EVENT_TIME,
                )
                self.assertEqual(parsed.cutoff_utc, expected)

    def test_known_positive_zero_offset_is_accepted(self) -> None:
        parsed = parse_history_command(
            "/chat clear 2026-08-01T11:30:00+00:00",
            event_time=REFERENCE_EVENT_TIME,
        )

        self.assertEqual(parsed.cutoff_utc, "2026-08-01T11:30:00.000000Z")

    def test_supported_gregorian_edge_years_are_zero_padded(self) -> None:
        earliest = parse_history_command(
            "/chat clear 0001-01-01T00:00:00Z",
            event_time=REFERENCE_EVENT_TIME,
        )
        latest = parse_history_command(
            "/chat clear 9999-12-31T09:00:00Z",
            event_time="9999-12-31T09:00:00Z",
        )

        self.assertEqual(earliest.cutoff_utc, "0001-01-01T00:00:00.000000Z")
        self.assertEqual(latest.cutoff_utc, "9999-12-31T09:00:00.000000Z")

    def test_absolute_syntax_outside_the_contract_is_rejected(self) -> None:
        invalid_arguments = (
            "2026-8-01",
            "2026-08-1",
            "2026-08-01t18:30",
            "2026-08-01 18:30",
            "2026-08-01T18:30Z",
            "2026-08-01T18:30.5",
            "2026-08-01T18:30:00.1234567Z",
            "2026-08-01T18:30:00z",
            "2026-W31-6",
            "2026-213",
            "2026-08-01T18:30:00Asia/Bangkok",
        )
        for argument in invalid_arguments:
            with self.subTest(argument=argument):
                self.assert_error(
                    HistoryCommandErrorCode.INVALID_SYNTAX,
                    f"/chat clear {argument}",
                )

    def test_invalid_calendar_clock_and_offset_values_are_rejected(self) -> None:
        invalid_arguments = (
            "0000-01-01",
            "2026-02-30",
            "2026-08-01T24:00",
            "2026-08-01T18:60",
            "2026-08-01T18:30:60Z",
            "2026-08-01T18:30:00+15:00",
            "2026-08-01T18:30:00+14:01",
            "2026-08-01T18:30:00-14:01",
            "2026-08-01T18:30:00+07:60",
            "2026-08-01T18:30:00-00:00",
        )
        for argument in invalid_arguments:
            with self.subTest(argument=argument):
                self.assert_error(
                    HistoryCommandErrorCode.INVALID_DATETIME,
                    f"/chat clear {argument}",
                )

    def test_equal_cutoff_is_allowed_but_future_cutoff_is_rejected(self) -> None:
        equal = parse_history_command(
            "/chat clear 2026-08-01T18:30:00+07:00",
            event_time="2026-08-01T11:30:00Z",
        )

        self.assertEqual(equal.cutoff_utc, equal.reference_time_utc)
        self.assert_error(
            HistoryCommandErrorCode.FUTURE_CUTOFF,
            "/chat clear 2026-08-01T11:30:00.000001Z",
            event_time="2026-08-01T11:30:00Z",
        )

    def test_utc_normalization_overflow_has_a_distinct_error(self) -> None:
        self.assert_error(
            HistoryCommandErrorCode.TIME_OVERFLOW,
            "/chat clear 0001-01-01",
        )

    def test_bangkok_display_overflow_is_a_typed_time_overflow(self) -> None:
        self.assert_error(
            HistoryCommandErrorCode.TIME_OVERFLOW,
            "/chat clear 9999-12-31T23:59:59Z",
            event_time="9999-12-31T23:59:59Z",
        )


class ReferenceTimeTests(HistoryTimeTestCase):
    def test_protobuf_nanoseconds_are_truncated_to_the_earlier_microsecond(
        self,
    ) -> None:
        parsed = parse_history_command(
            "/chat clear 1m",
            event_time="2026-08-11T02:30:00.123456789Z",
        )

        self.assertEqual(parsed.reference_time_utc, REFERENCE_EVENT_TIME)
        self.assertEqual(parsed.cutoff_utc, "2026-08-11T02:29:00.123456Z")

    def test_event_time_with_an_offset_normalizes_to_utc(self) -> None:
        parsed = parse_history_command(
            "/chat clear 1m",
            event_time="2026-08-11T09:30:00.5+07:00",
        )

        self.assertEqual(parsed.reference_time_utc, "2026-08-11T02:30:00.500000Z")

    def test_malformed_present_event_time_fails_without_clock_fallback(self) -> None:
        malformed_values: tuple[object, ...] = (
            "",
            "2026-08-11T02:30:00",
            "2026-08-11T02:30:00z",
            "2026-08-11T02:30:00.1234567890Z",
            "2026-02-30T02:30:00Z",
            "2026-08-11T02:30:00-00:00",
            123,
        )
        for event_time in malformed_values:
            with self.subTest(event_time=event_time):
                clock = Mock(side_effect=AssertionError("must not fall back"))
                self.assert_error(
                    HistoryCommandErrorCode.INVALID_EVENT_TIME,
                    "/chat clear 1m",
                    event_time=event_time,
                    clock=clock,
                )
                clock.assert_not_called()

    def test_absent_event_time_captures_an_aware_clock_exactly_once(self) -> None:
        clock = Mock(
            return_value=datetime(
                2026,
                8,
                11,
                9,
                30,
                0,
                654321,
                tzinfo=timezone(timedelta(hours=7)),
            )
        )

        parsed = parse_history_command(
            "/chat clear 30m",
            event_time=None,
            clock=clock,
        )

        clock.assert_called_once_with()
        self.assertEqual(parsed.reference_time_utc, "2026-08-11T02:30:00.654321Z")
        self.assertEqual(parsed.cutoff_utc, "2026-08-11T02:00:00.654321Z")

    def test_same_event_time_is_retry_deterministic_and_never_reads_clock(
        self,
    ) -> None:
        first_clock = Mock(return_value=datetime(2030, 1, 1, tzinfo=timezone.utc))
        second_clock = Mock(return_value=datetime(2040, 1, 1, tzinfo=timezone.utc))

        first = parse_history_command(
            "/chat clear 1y2mo3d",
            event_time=REFERENCE_EVENT_TIME,
            clock=first_clock,
        )
        second = parse_history_command(
            "/chat clear 1y2mo3d",
            event_time=REFERENCE_EVENT_TIME,
            clock=second_clock,
        )

        self.assertEqual(first, second)
        first_clock.assert_not_called()
        second_clock.assert_not_called()

    def test_invalid_or_failing_clock_returns_only_safe_typed_errors(self) -> None:
        clocks = (
            Mock(
                return_value=datetime(2026, 8, 11, 2, 30, tzinfo=timezone.utc).replace(
                    tzinfo=None
                )
            ),
            Mock(return_value="not-a-datetime"),
            Mock(side_effect=RuntimeError("SENTINEL-PRIVATE-CLOCK")),
        )
        for clock in clocks:
            with self.subTest(clock=clock):
                error = self.assert_error(
                    HistoryCommandErrorCode.INVALID_REFERENCE_TIME,
                    "/chat clear 1m",
                    event_time=None,
                    clock=clock,
                )
                clock.assert_called_once_with()
                self.assertNotIn("SENTINEL-PRIVATE-CLOCK", str(error))


if __name__ == "__main__":
    unittest.main()
