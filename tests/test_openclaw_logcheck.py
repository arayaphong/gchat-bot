from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from helpers.providers.openclaw_logcheck import (
    JOURNAL_TIMEOUT_SECONDS,
    check_run_errors,
)


class OpenClawLogcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.since = datetime(2026, 8, 5, 21, 30, 45, tzinfo=timezone.utc)

    def test_empty_run_id_does_not_read_the_journal(self) -> None:
        with patch("helpers.providers.openclaw_logcheck.subprocess.run") as run:
            self.assertEqual(check_run_errors("", self.since), [])

        run.assert_not_called()

    def test_only_run_specific_error_patterns_are_returned(self) -> None:
        run_id = "chatcmpl_target"
        matching_lines = [
            f"gateway runId={run_id} isError=true",
            f"gateway runId={run_id} [model-fetch] error AbortError",
        ]
        stdout = "\n".join(
            [
                f"gateway runId={run_id} isError=false",
                f"gateway runId={run_id} ordinary error wording",
                *matching_lines,
                "gateway runId=chatcmpl_other timedOut=true",
            ]
        )
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=stdout,
            stderr="",
        )

        with patch(
            "helpers.providers.openclaw_logcheck.subprocess.run",
            return_value=completed,
        ) as run:
            result = check_run_errors(run_id, self.since)

        self.assertEqual(result, matching_lines)
        run.assert_called_once_with(
            [
                "journalctl",
                "--user",
                "-u",
                "openclaw-gateway",
                "--since",
                "2026-08-05 21:30:45",
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=JOURNAL_TIMEOUT_SECONDS,
            check=False,
        )

    def test_unavailable_or_timed_out_journal_is_best_effort(self) -> None:
        failures = [
            OSError("journalctl missing"),
            subprocess.TimeoutExpired(["journalctl"], JOURNAL_TIMEOUT_SECONDS),
        ]

        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                patch(
                    "helpers.providers.openclaw_logcheck.subprocess.run",
                    side_effect=failure,
                ),
            ):
                self.assertEqual(
                    check_run_errors("chatcmpl_target", self.since),
                    [],
                )

    def test_nonzero_journal_exit_is_best_effort(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            1,
            stdout="chatcmpl_target isError=true",
            stderr="journal unavailable",
        )

        with patch(
            "helpers.providers.openclaw_logcheck.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(
                check_run_errors("chatcmpl_target", self.since),
                [],
            )


if __name__ == "__main__":
    unittest.main()
