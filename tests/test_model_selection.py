from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from helpers.providers.model_selection import (
    ModelSelection,
    find_session_model,
    get_model_selection,
    load_cli_json,
)


def completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


class ModelSelectionTests(unittest.TestCase):
    def test_exact_session_model_overrides_default(self) -> None:
        sessions = {
            "sessions": [
                {
                    "key": "agent:main:other",
                    "modelProvider": "minimax",
                    "model": "MiniMax-M3",
                },
                {
                    "key": "agent:main:gchat:c0ffee",
                    "modelProvider": " moonshot ",
                    "model": " kimi-k2.6 ",
                },
            ]
        }

        with (
            patch(
                "helpers.providers.model_selection.get_default_model",
                return_value=completed("kimi-coding/kimi-for-coding"),
            ),
            patch(
                "helpers.providers.model_selection.list_sessions",
                return_value=completed(sessions),
            ),
        ):
            selection = get_model_selection("agent:main:gchat:c0ffee")

        self.assertEqual(
            selection,
            ModelSelection(
                default_model="kimi-coding/kimi-for-coding",
                session_model="moonshot/kimi-k2.6",
            ),
        )
        self.assertEqual(selection.effective_model, "moonshot/kimi-k2.6")

    def test_missing_session_falls_back_to_default(self) -> None:
        with (
            patch(
                "helpers.providers.model_selection.get_default_model",
                return_value=completed("kimi-coding/kimi-for-coding"),
            ),
            patch(
                "helpers.providers.model_selection.list_sessions",
                return_value=completed({"sessions": []}),
            ),
        ):
            selection = get_model_selection("agent:main:gchat:decade")

        self.assertIsNone(selection.session_model)
        self.assertEqual(selection.effective_model, "kimi-coding/kimi-for-coding")

    def test_malformed_matching_session_fails_closed(self) -> None:
        with self.assertRaisesRegex(TypeError, "model"):
            find_session_model(
                {
                    "sessions": [
                        {
                            "key": "agent:main:gchat:c0ffee",
                            "modelProvider": "moonshot",
                        }
                    ]
                },
                "agent:main:gchat:c0ffee",
            )

    def test_cli_failure_and_invalid_json_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "bad command"):
            load_cli_json(
                subprocess.CompletedProcess([], 2, stdout="", stderr="bad command"),
                "openclaw command",
            )

        with self.assertRaises(json.JSONDecodeError):
            load_cli_json(
                subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
                "openclaw command",
            )


if __name__ == "__main__":
    unittest.main()
