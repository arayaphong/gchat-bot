from __future__ import annotations

import unittest

from helpers.chat_history_time import (
    HistoryCommandKind,
    recognize_history_command,
)


class HistoryCommandRecognizerTests(unittest.TestCase):
    def test_phase_zero_truth_table(self) -> None:
        cases = (
            ("/chat", HistoryCommandKind.STATS),
            ("  /chat\t", HistoryCommandKind.STATS),
            ("/chat clear 1w", HistoryCommandKind.CLEAR),
            ("/chat\tclear\t1w", HistoryCommandKind.CLEAR),
            ("/chatty", HistoryCommandKind.NOT_HISTORY),
            ("/chat1", HistoryCommandKind.NOT_HISTORY),
            ("/chat_foo", HistoryCommandKind.NOT_HISTORY),
            ("/Chat", HistoryCommandKind.INVALID_HISTORY),
            ("/chat Clear 1w", HistoryCommandKind.INVALID_HISTORY),
            ("/chat!", HistoryCommandKind.INVALID_HISTORY),
            ("/chat-clear", HistoryCommandKind.INVALID_HISTORY),
            ("/chat\u00a0clear 1w", HistoryCommandKind.INVALID_HISTORY),
            ("\u00a0/chat\u00a0", HistoryCommandKind.INVALID_HISTORY),
            ("/chat clear 1 w", HistoryCommandKind.INVALID_HISTORY),
            ("/chat clear 1w\nextra", HistoryCommandKind.INVALID_HISTORY),
            ("please /chat", HistoryCommandKind.NOT_HISTORY),
        )

        for raw_text, expected in cases:
            with self.subTest(raw_text=raw_text):
                self.assertIs(recognize_history_command(raw_text), expected)

    def test_unicode_identifier_continuations_are_ordinary_messages(self) -> None:
        for raw_text in ("/chatก", "/chat中", "/chat٣"):
            with self.subTest(raw_text=raw_text):
                self.assertIs(
                    recognize_history_command(raw_text),
                    HistoryCommandKind.NOT_HISTORY,
                )

    def test_reserved_namespace_is_case_insensitive_but_valid_grammar_is_not(
        self,
    ) -> None:
        for raw_text in ("/CHAT", "/cHaT clear 1w", "/CHAT CLEAR 1w"):
            with self.subTest(raw_text=raw_text):
                self.assertIs(
                    recognize_history_command(raw_text),
                    HistoryCommandKind.INVALID_HISTORY,
                )

    def test_clear_classification_only_recognizes_one_token_envelope(self) -> None:
        accepted_envelopes = (
            " /chat clear 1w ",
            "/chat\tclear 2026-08-11T02:30:00Z\t",
            # Argument semantics deliberately belong to the Phase 2 parser.
            "/chat clear not-a-time",
        )
        for raw_text in accepted_envelopes:
            with self.subTest(raw_text=raw_text):
                self.assertIs(
                    recognize_history_command(raw_text), HistoryCommandKind.CLEAR
                )

        for raw_text in (
            "/chat clear",
            "/chat clear 1w extra",
            "/chat clear\t",
            "/chat  clear 1w extra",
        ):
            with self.subTest(raw_text=raw_text):
                self.assertIs(
                    recognize_history_command(raw_text),
                    HistoryCommandKind.INVALID_HISTORY,
                )

    def test_control_and_non_ascii_whitespace_never_form_valid_commands(self) -> None:
        for raw_text in (
            "/chat\r",
            "/chat\x00",
            "/chat clear 1w\x7f",
            "/chat\u2003clear 1w",
            "\u2028/chat",
        ):
            with self.subTest(raw_text=raw_text):
                self.assertIs(
                    recognize_history_command(raw_text),
                    HistoryCommandKind.INVALID_HISTORY,
                )

    def test_empty_and_unrelated_text_are_not_history(self) -> None:
        for raw_text in ("", "   ", "/cha", "hello", " /models"):
            with self.subTest(raw_text=raw_text):
                self.assertIs(
                    recognize_history_command(raw_text),
                    HistoryCommandKind.NOT_HISTORY,
                )

    def test_non_string_input_is_a_programming_error(self) -> None:
        with self.assertRaisesRegex(TypeError, "raw_text must be a string"):
            recognize_history_command(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
