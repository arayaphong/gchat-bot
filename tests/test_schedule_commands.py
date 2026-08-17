from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import (
    SCHEDULE_LIST_EMPTY_TEXT,
    format_schedule_list,
)
from helpers.schedule_commands import (
    USAGE_TEXT,
    ParsedScheduleCommand,
    ScheduleCommandError,
    ScheduleSpec,
    build_cron_add_argv,
    list_session_jobs,
    parse_duration_seconds,
    parse_schedule_args,
    resolve_at_value,
    resolve_session_job,
)
from helpers.session_keys import ChatSessionContext

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"
CONTEXT = ChatSessionContext.for_thread(SPACE, THREAD)
SESSION_KEY = CONTEXT.session_key
NOW = datetime(2026, 8, 13, 15, 0, tzinfo=timezone(timedelta(hours=7)))


def _completed(stdout: str = "{}", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["openclaw"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class ParseDurationTests(unittest.TestCase):
    def test_single_unit(self) -> None:
        self.assertEqual(parse_duration_seconds("10m"), 600)
        self.assertEqual(parse_duration_seconds("2h"), 7200)

    def test_compound_units(self) -> None:
        self.assertEqual(parse_duration_seconds("1h30m"), 5400)

    def test_rejects_invalid_text(self) -> None:
        for invalid in ("", "10x", "m", "10m5", "0m", "soon"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ScheduleCommandError):
                    parse_duration_seconds(invalid)

    def test_rejects_excessive_lead_time(self) -> None:
        with self.assertRaises(ScheduleCommandError):
            parse_duration_seconds("400d")


class ResolveAtValueTests(unittest.TestCase):
    def test_relative_duration_becomes_seconds(self) -> None:
        self.assertEqual(resolve_at_value("+10m", NOW), "+600s")

    def test_clock_time_later_today(self) -> None:
        result = resolve_at_value("18:30", NOW)
        self.assertEqual(result, "2026-08-13T18:30:00+07:00")

    def test_clock_time_already_passed_rolls_to_tomorrow(self) -> None:
        result = resolve_at_value("09:00", NOW)
        self.assertEqual(result, "2026-08-14T09:00:00+07:00")

    def test_iso_datetime(self) -> None:
        result = resolve_at_value("2026-08-14 09:00", NOW)
        self.assertEqual(result, "2026-08-14T09:00:00+07:00")

    def test_rejects_past_iso_datetime(self) -> None:
        with self.assertRaises(ScheduleCommandError):
            resolve_at_value("2026-08-13 09:00", NOW)

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(ScheduleCommandError):
            resolve_at_value("next friday", NOW)


class ParseScheduleArgsTests(unittest.TestCase):
    def test_empty_is_usage(self) -> None:
        self.assertEqual(parse_schedule_args("", NOW).kind, "usage")

    def test_list(self) -> None:
        self.assertEqual(parse_schedule_args("list", NOW).kind, "list")

    def test_cancel(self) -> None:
        parsed = parse_schedule_args("cancel abcd1234", NOW)
        self.assertEqual(parsed.kind, "cancel")
        self.assertEqual(parsed.job_ref, "abcd1234")

    def test_cancel_without_id_is_rejected(self) -> None:
        with self.assertRaises(ScheduleCommandError):
            parse_schedule_args("cancel", NOW)

    def test_add_with_relative_duration(self) -> None:
        parsed = parse_schedule_args("+10m แจ้งเตือนประชุม", NOW)
        self.assertEqual(parsed.kind, "add")
        assert parsed.spec is not None
        self.assertEqual(parsed.spec.when, "+600s")
        self.assertEqual(parsed.spec.message, "แจ้งเตือนประชุม")

    def test_add_with_in_keyword(self) -> None:
        parsed = parse_schedule_args("in 1h standup", NOW)
        self.assertEqual(parsed.kind, "add")
        assert parsed.spec is not None
        self.assertEqual(parsed.spec.when, "+3600s")

    def test_add_with_at_clock_time(self) -> None:
        parsed = parse_schedule_args("at 18:30 โทรหาลูกค้า", NOW)
        self.assertEqual(parsed.kind, "add")
        assert parsed.spec is not None
        self.assertEqual(parsed.spec.when, "2026-08-13T18:30:00+07:00")

    def test_add_with_at_iso_date_and_time(self) -> None:
        parsed = parse_schedule_args("at 2026-08-14 09:00 สรุปข่าว", NOW)
        self.assertEqual(parsed.kind, "add")
        assert parsed.spec is not None
        self.assertEqual(parsed.spec.when, "2026-08-14T09:00:00+07:00")

    def test_add_without_message_is_usage(self) -> None:
        self.assertEqual(parse_schedule_args("+10m", NOW).kind, "usage")

    def test_unknown_shape_is_usage(self) -> None:
        self.assertEqual(parse_schedule_args("tomorrow 9am standup", NOW).kind, "usage")

    def test_message_with_shell_metacharacters_kept_verbatim(self) -> None:
        parsed = parse_schedule_args("+10m run $(rm -rf x) `now`", NOW)
        assert parsed.spec is not None
        self.assertEqual(parsed.spec.message, "run $(rm -rf x) `now`")


class BuildCronAddArgvTests(unittest.TestCase):
    def test_pins_job_to_event_session(self) -> None:
        argv = build_cron_add_argv(ScheduleSpec(when="+600s", message="ประชุม"), SESSION_KEY)
        self.assertIn(f"session:{SESSION_KEY}", argv)
        self.assertIn("--delete-after-run", argv)
        self.assertIn("--json", argv)
        tz_index = argv.index("--tz")
        self.assertEqual(argv[tz_index + 1], "Asia/Bangkok")
        message_index = argv.index("--message")
        self.assertIn("ประชุม", argv[message_index + 1])

    def test_message_with_metacharacters_is_one_plain_argv_element(self) -> None:
        payload = "run $(rm -rf x) `now`"
        argv = build_cron_add_argv(ScheduleSpec(when="+600s", message=payload), SESSION_KEY)
        message_index = argv.index("--message")
        self.assertIn(payload, argv[message_index + 1])

    def test_rejects_empty_session_key(self) -> None:
        with self.assertRaises(ValueError):
            build_cron_add_argv(ScheduleSpec(when="+600s", message="x"), " ")


class SessionJobOwnershipTests(unittest.TestCase):
    def _jobs_payload(self) -> str:
        return (
            '{"jobs": ['
            '{"id": "aaaa1111-0000", "sessionTarget": "session:' + SESSION_KEY + '"}, '
            '{"id": "bbbb2222-0000", "sessionTarget": "session:agent:main:gchat:other:other"}, '
            '{"id": "cccc3333-0000", "sessionKey": "' + SESSION_KEY + '"}'
            "]}"
        )

    def test_list_filters_other_sessions(self) -> None:
        with patch(
            "helpers.schedule_commands.cron_list",
            return_value=_completed(self._jobs_payload()),
        ):
            jobs = list_session_jobs(SESSION_KEY)
        self.assertEqual(
            [job["id"] for job in jobs],
            ["aaaa1111-0000", "cccc3333-0000"],
        )

    def test_resolve_accepts_unique_prefix(self) -> None:
        with patch(
            "helpers.schedule_commands.cron_list",
            return_value=_completed(self._jobs_payload()),
        ):
            job = resolve_session_job("aaaa", SESSION_KEY)
        self.assertEqual(job["id"], "aaaa1111-0000")

    def test_resolve_rejects_job_of_another_session(self) -> None:
        with patch(
            "helpers.schedule_commands.cron_list",
            return_value=_completed(self._jobs_payload()),
        ):
            with self.assertRaises(ScheduleCommandError):
                resolve_session_job("bbbb", SESSION_KEY)

    def test_resolve_rejects_unknown_id(self) -> None:
        with patch(
            "helpers.schedule_commands.cron_list",
            return_value=_completed(self._jobs_payload()),
        ):
            with self.assertRaises(ScheduleCommandError):
                resolve_session_job("zzzz", SESSION_KEY)

    def test_list_rejects_nonzero_exit(self) -> None:
        with patch(
            "helpers.schedule_commands.cron_list",
            return_value=_completed("boom", returncode=1, stderr="boom"),
        ):
            with self.assertRaises(RuntimeError):
                list_session_jobs(SESSION_KEY)


class ScheduleMessageFormatTests(unittest.TestCase):
    def test_empty_list_text(self) -> None:
        self.assertEqual(format_schedule_list([]), SCHEDULE_LIST_EMPTY_TEXT)

    def test_list_shows_id_time_and_label(self) -> None:
        job = {
            "id": "aaaa1111-bbbb",
            "displayName": "แจ้งเตือนประชุม",
            "nextRunAtMs": 1786603200000,
        }
        text = format_schedule_list([job])
        self.assertIn("aaaa1111", text)
        self.assertIn("แจ้งเตือนประชุม", text)


class PromptSessionContextTests(unittest.TestCase):
    def test_session_key_block_is_included_when_provided(self) -> None:
        from helpers.providers.openclaw_provider import build_openclaw_prompt

        prompt = build_openclaw_prompt("hello", "Alice", [], session_key=SESSION_KEY)
        self.assertIn("[SESSION_CONTEXT]", prompt)
        self.assertIn(f"sessionKey: {SESSION_KEY}", prompt)
        self.assertTrue(prompt.endswith("Alice: hello"))

    def test_session_key_block_is_omitted_without_key(self) -> None:
        from helpers.providers.openclaw_provider import build_openclaw_prompt

        prompt = build_openclaw_prompt("hello", "Alice", [])
        self.assertNotIn("SESSION_CONTEXT", prompt)


class OrchestratorScheduleCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = Mock()
        self.session_manager = Mock()
        self.attachment_service = Mock()
        self.openclaw_client = Mock()
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
        )

    def _sent_texts(self) -> list[str]:
        return [
            call.args[2]
            for call in self.gateway.send_followup.call_args_list
        ]

    def test_usage_is_answered_in_thread(self) -> None:
        self.orchestrator._handle_schedule(CONTEXT, "")
        self.assertIn(USAGE_TEXT, self._sent_texts())
        space, thread = self.gateway.send_followup.call_args.args[:2]
        self.assertEqual((space, thread), (SPACE, THREAD))

    def test_add_creates_job_for_event_session(self) -> None:
        job = {"id": "aaaa1111-bbbb", "nextRunAtMs": 1786603200000}
        with patch(
            "helpers.message_orchestrator.add_session_job",
            return_value=job,
        ) as add_job:
            self.orchestrator._handle_schedule(CONTEXT, "+10m แจ้งเตือนประชุม")
        spec, session_key = add_job.call_args.args
        self.assertEqual(session_key, SESSION_KEY)
        self.assertEqual(spec.message, "แจ้งเตือนประชุม")
        sent = "\n".join(self._sent_texts())
        self.assertIn("ตั้งเตือนแล้ว", sent)
        self.assertIn("aaaa1111", sent)

    def test_list_is_scoped_to_event_session(self) -> None:
        with patch(
            "helpers.message_orchestrator.list_session_jobs",
            return_value=[],
        ) as list_jobs:
            self.orchestrator._handle_schedule(CONTEXT, "list")
        self.assertEqual(list_jobs.call_args.args[0], SESSION_KEY)
        self.assertIn(SCHEDULE_LIST_EMPTY_TEXT, self._sent_texts())

    def test_cancel_resolves_ownership_before_removing(self) -> None:
        job = {"id": "aaaa1111-bbbb", "displayName": "แจ้งเตือนประชุม"}
        with (
            patch(
                "helpers.message_orchestrator.resolve_session_job",
                return_value=job,
            ) as resolve_job,
            patch(
                "helpers.message_orchestrator.remove_session_job"
            ) as remove_job,
        ):
            self.orchestrator._handle_schedule(CONTEXT, "cancel aaaa")
        self.assertEqual(
            resolve_job.call_args.args,
            ("aaaa", SESSION_KEY),
        )
        remove_job.assert_called_once_with("aaaa1111-bbbb")

    def test_cli_failure_is_reported_not_raised(self) -> None:
        with patch(
            "helpers.message_orchestrator.list_session_jobs",
            side_effect=FileNotFoundError("openclaw"),
        ):
            self.orchestrator._handle_schedule(CONTEXT, "list")
        sent = "\n".join(self._sent_texts())
        self.assertIn("ไม่พบคำสั่ง openclaw", sent)


if __name__ == "__main__":
    unittest.main()
