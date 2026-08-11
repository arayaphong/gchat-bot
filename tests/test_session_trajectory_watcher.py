from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.session_keys import ChatSessionContext
from helpers.session_trajectory_watcher import (
    AssistantTrajectoryMessage,
    SessionTrajectoryWatcher,
    extract_assistant_text,
    parse_media_directives,
)


def trajectory_entry(
    role: str,
    content: object,
    *,
    timestamp: str = "2026-08-05T12:00:00Z",
) -> str:
    return json.dumps(
        {
            "type": "message",
            "timestamp": timestamp,
            "message": {"role": role, "content": content},
        },
        ensure_ascii=False,
    )


class SessionTrajectoryWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.sessions_dir = Path(temporary.name)
        self.session_key = "agent:main:gchat:one:root"
        self.session_id = "session-one"
        self.space = "spaces/one"
        self.identity_thread = "spaces/one/threads/root"
        self.reply_thread = self.identity_thread
        self.trajectory_file = self.sessions_dir / f"{self.session_id}.jsonl"
        self.state_file = self.sessions_dir / "watcher-state.json"
        self.delivery = Mock(return_value=True)
        self.watcher = SessionTrajectoryWatcher(
            self.delivery,
            sessions_dir=self.sessions_dir,
            poll_seconds=0.01,
            state_file=self.state_file,
        )

    def restarted_watcher(self, delivery: Mock) -> SessionTrajectoryWatcher:
        return SessionTrajectoryWatcher(
            delivery,
            sessions_dir=self.sessions_dir,
            poll_seconds=0.01,
            state_file=self.state_file,
        )

    def prepare_session(
        self,
        session_key: str | None = None,
        *,
        space: str | None = None,
        identity_thread: str | None = None,
        reply_thread: str | None = None,
    ) -> None:
        self.watcher.prepare_session(
            session_key or self.session_key,
            self.space if space is None else space,
            (self.identity_thread if identity_thread is None else identity_thread),
            self.reply_thread if reply_thread is None else reply_thread,
        )

    def write_index(self, session_key: str, session_id: str) -> None:
        (self.sessions_dir / "sessions.json").write_text(
            json.dumps({session_key: {"sessionId": session_id}}),
            encoding="utf-8",
        )

    @staticmethod
    def append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as file_handle:
            file_handle.write(line + "\n")

    def test_existing_history_is_skipped_and_new_assistant_text_is_delivered(
        self,
    ) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.write_text(
            trajectory_entry("assistant", "old answer") + "\n",
            encoding="utf-8",
        )
        self.prepare_session()

        self.append_line(self.trajectory_file, trajectory_entry("user", "question"))
        self.append_line(
            self.trajectory_file,
            trajectory_entry(
                "assistant",
                [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "new "},
                    {"type": "text", "text": "answer"},
                ],
            ),
        )
        self.watcher._poll_once()

        self.delivery.assert_called_once()
        message = self.delivery.call_args.args[0]
        self.assertEqual(message.session_key, self.session_key)
        self.assertEqual(message.space, self.space)
        self.assertEqual(message.reply_thread, self.reply_thread)
        self.assertEqual(message.text, "new answer")
        self.assertEqual(message.timestamp, "2026-08-05T12:00:00Z")

    def test_direct_message_accepts_empty_reply_with_real_identity_thread(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.watcher.prepare_session(
            self.session_key,
            self.space,
            self.identity_thread,
            "",
        )
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "root answer"),
        )

        restarted_delivery = Mock(return_value=True)
        restarted = self.restarted_watcher(restarted_delivery)
        restarted._poll_once()

        message = restarted_delivery.call_args.args[0]
        self.assertEqual(message.space, self.space)
        self.assertEqual(message.reply_thread, "")

    def test_mixed_case_google_route_survives_encoded_key_restart(self) -> None:
        space = "spaces/AAQAjEa3Dp8"
        identity_thread = "spaces/AAQAjEa3Dp8/threads/Zz9"
        context = ChatSessionContext.from_event(
            space,
            identity_thread,
            is_direct_message=True,
            thread_reply=False,
        )
        self.write_index(context.session_key, self.session_id)
        self.trajectory_file.touch()
        self.watcher.prepare_session(
            context.session_key,
            space,
            identity_thread,
            "",
        )
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "case-safe answer"),
        )

        restarted_delivery = Mock(return_value=True)
        restarted = self.restarted_watcher(restarted_delivery)
        restarted._poll_once()

        message = restarted_delivery.call_args.args[0]
        self.assertEqual(message.session_key, context.session_key)
        self.assertEqual(message.session_key, message.session_key.lower())
        self.assertEqual(message.space, space)
        self.assertEqual(message.reply_thread, "")

    def test_session_created_after_prepare_starts_from_its_first_line(self) -> None:
        self.prepare_session()
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.write_text(
            trajectory_entry("assistant", "first answer") + "\n",
            encoding="utf-8",
        )

        self.watcher._poll_once()

        self.assertEqual(self.delivery.call_args.args[0].text, "first answer")

    def test_background_thread_delivers_appended_messages(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        delivered = threading.Event()
        self.delivery.side_effect = lambda _message: delivered.set() or True

        self.watcher.start()
        self.addCleanup(self.watcher.stop)
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "background answer"),
        )

        self.assertTrue(delivered.wait(timeout=1))
        self.assertEqual(self.delivery.call_args.args[0].text, "background answer")

    def test_failed_delivery_retries_the_same_message_and_delivery_id(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "retry me"),
        )
        self.delivery.side_effect = [False, True]

        self.watcher._poll_once()
        self.watcher._poll_once()

        self.assertEqual(self.delivery.call_count, 2)
        first = self.delivery.call_args_list[0].args[0]
        second = self.delivery.call_args_list[1].args[0]
        self.assertEqual(first.text, "retry me")
        self.assertEqual(first.delivery_id, second.delivery_id)

    def test_failed_delivery_is_replayed_with_same_id_after_restart(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "retry after restart"),
        )
        self.delivery.return_value = False

        self.watcher._poll_once()

        first_attempt = self.delivery.call_args.args[0]
        restarted_delivery = Mock(return_value=True)
        restarted = self.restarted_watcher(restarted_delivery)
        restarted._poll_once()

        restarted_delivery.assert_called_once()
        retry = restarted_delivery.call_args.args[0]
        self.assertEqual(retry.text, "retry after restart")
        self.assertEqual(retry.delivery_id, first_attempt.delivery_id)
        self.assertEqual(
            (retry.space, retry.reply_thread),
            (self.space, self.reply_thread),
        )

    def test_committed_output_is_not_replayed_after_restart(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "already delivered"),
        )

        self.watcher._poll_once()

        restarted_delivery = Mock(return_value=True)
        restarted = self.restarted_watcher(restarted_delivery)
        restarted._poll_once()

        self.delivery.assert_called_once()
        restarted_delivery.assert_not_called()

    def test_pending_cursor_restores_without_baselining_new_output(self) -> None:
        self.prepare_session()
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.write_text(
            trajectory_entry("assistant", "created after prepare") + "\n",
            encoding="utf-8",
        )

        restarted_delivery = Mock(return_value=True)
        restarted = self.restarted_watcher(restarted_delivery)
        restarted._poll_once()

        restarted_delivery.assert_called_once()
        self.assertEqual(
            restarted_delivery.call_args.args[0].text,
            "created after prepare",
        )

    def test_start_refreshes_state_committed_after_construction(self) -> None:
        delivered = threading.Event()
        early_delivery = Mock(side_effect=lambda _message: delivered.set() or True)
        early_watcher = self.restarted_watcher(early_delivery)
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "state appeared later"),
        )

        early_watcher.start()
        self.addCleanup(early_watcher.stop)

        self.assertTrue(delivered.wait(timeout=1))
        self.assertEqual(
            early_delivery.call_args.args[0].text,
            "state appeared later",
        )

    def test_atomic_write_failure_preserves_last_committed_state(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        original_state = self.state_file.read_bytes()
        second_key = "agent:main:gchat:two:root"

        with (
            patch(
                "helpers.session_trajectory_watcher.os.replace",
                side_effect=OSError("disk unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "cannot persist"),
        ):
            self.watcher.prepare_session(
                second_key,
                "spaces/two",
                "spaces/two/threads/root",
                "spaces/two/threads/root",
            )

        self.assertEqual(self.state_file.read_bytes(), original_state)
        self.assertNotIn(second_key, self.watcher._cursors)
        self.assertEqual(
            list(self.sessions_dir.glob(f".{self.state_file.name}.*.tmp")),
            [],
        )

    def test_cursor_state_is_private_and_contains_canonical_route(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.write_text(
            trajectory_entry("assistant", "existing history") + "\n",
            encoding="utf-8",
        )

        self.prepare_session()

        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(
            payload["cursors"][self.session_key],
            {
                "space": self.space,
                "identity_thread": self.identity_thread,
                "reply_thread": self.reply_thread,
                "trajectory_file": self.trajectory_file.name,
                "offset": self.trajectory_file.stat().st_size,
            },
        )

    def test_omitted_state_file_uses_a_durable_default(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        default_watcher = SessionTrajectoryWatcher(
            Mock(return_value=True),
            sessions_dir=self.sessions_dir,
            poll_seconds=0.01,
        )

        default_watcher.prepare_session(
            self.session_key,
            self.space,
            self.identity_thread,
            self.reply_thread,
        )

        default_state = self.sessions_dir / ".jinx-gchat-trajectory-cursors.json"
        self.assertTrue(default_state.is_file())
        self.assertEqual(default_state.stat().st_mode & 0o777, 0o600)

    def test_corrupt_or_untrusted_state_is_ignored_fail_closed(self) -> None:
        outside_trajectory = self.sessions_dir.with_suffix(".outside.jsonl")
        outside_trajectory.write_text("outside\n", encoding="utf-8")
        self.addCleanup(outside_trajectory.unlink, missing_ok=True)
        self.trajectory_file.symlink_to(outside_trajectory)
        valid_cursor = {
            "space": self.space,
            "identity_thread": self.identity_thread,
            "reply_thread": self.reply_thread,
            "trajectory_file": self.trajectory_file.name,
            "offset": 0,
        }
        invalid_states: tuple[str, ...] = (
            "not-json",
            json.dumps(
                {
                    "version": 1,
                    "cursors": {self.session_key: valid_cursor},
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "cursors": {
                        self.session_key: {
                            **valid_cursor,
                            "space": "spaces/other",
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "cursors": {
                        self.session_key: {
                            **valid_cursor,
                            "identity_thread": "spaces/one/threads/other",
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "cursors": {
                        self.session_key: {
                            **valid_cursor,
                            "reply_thread": "spaces/two/threads/root",
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "cursors": {
                        self.session_key: {
                            **valid_cursor,
                            "trajectory_file": "../outside.jsonl",
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "cursors": {self.session_key: {**valid_cursor, "offset": True}},
                }
            ),
            json.dumps(
                {
                    "version": 2,
                    "cursors": {self.session_key: valid_cursor},
                }
            ),
        )

        for ordinal, state in enumerate(invalid_states):
            with self.subTest(ordinal=ordinal):
                self.state_file.write_text(state, encoding="utf-8")
                delivery = Mock(return_value=True)

                restarted = self.restarted_watcher(delivery)
                restarted._poll_once()

                delivery.assert_not_called()
                self.assertEqual(restarted._cursors, {})

    def test_restored_trajectory_rejects_a_later_symlink_escape(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        delivery = Mock(return_value=True)
        restarted = self.restarted_watcher(delivery)
        outside_trajectory = self.sessions_dir.with_suffix(".outside.jsonl")
        outside_trajectory.write_text(
            trajectory_entry("assistant", "must not escape") + "\n",
            encoding="utf-8",
        )
        self.addCleanup(outside_trajectory.unlink, missing_ok=True)
        self.trajectory_file.unlink()
        self.trajectory_file.symlink_to(outside_trajectory)

        restarted._poll_once()

        delivery.assert_not_called()

    def test_media_directives_are_stripped_deduplicated_and_delivered(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry(
                "assistant",
                "เสร็จแล้วครับ\r\n\r\n"
                "  MEDIA:/tmp/spider cat.png  \r\n"
                "MEDIA:/tmp/./spider cat.png\r\n"
                "inline MEDIA:/tmp/not-a-directive.png",
            ),
        )

        self.watcher._poll_once()

        message = self.delivery.call_args.args[0]
        self.assertEqual(
            message.text,
            "เสร็จแล้วครับ\n\ninline MEDIA:/tmp/not-a-directive.png",
        )
        self.assertEqual(message.media_paths, ("/tmp/spider cat.png",))

    def test_media_only_assistant_message_is_delivered(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        self.append_line(
            self.trajectory_file,
            trajectory_entry(
                "assistant",
                "MEDIA:/tmp/one.png\nMEDIA:/tmp/two.png",
            ),
        )

        self.watcher._poll_once()

        message = self.delivery.call_args.args[0]
        self.assertEqual(message.text, "")
        self.assertEqual(
            message.media_paths,
            ("/tmp/one.png", "/tmp/two.png"),
        )

    def test_registering_another_session_retains_the_previous_trajectory(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()

        second_key = "agent:main:gchat:two:root"
        second_id = "session-two"
        second_file = self.sessions_dir / f"{second_id}.jsonl"
        (self.sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    self.session_key: {"sessionId": self.session_id},
                    second_key: {"sessionId": second_id},
                }
            ),
            encoding="utf-8",
        )
        second_file.write_text(
            trajectory_entry("assistant", "existing second history") + "\n",
            encoding="utf-8",
        )
        second_space = "spaces/two"
        second_thread = "spaces/two/threads/root"
        self.prepare_session(
            second_key,
            space=second_space,
            identity_thread=second_thread,
            reply_thread=second_thread,
        )
        self.append_line(
            self.trajectory_file, trajectory_entry("assistant", "old late")
        )
        self.append_line(second_file, trajectory_entry("assistant", "new session"))

        self.watcher._poll_once()

        self.assertEqual(self.delivery.call_count, 2)
        old_message, new_message = [
            invocation.args[0] for invocation in self.delivery.call_args_list
        ]
        self.assertEqual(old_message.text, "old late")
        self.assertEqual(
            (old_message.space, old_message.reply_thread),
            (self.space, self.reply_thread),
        )
        self.assertEqual(new_message.text, "new session")
        self.assertEqual(
            (new_message.space, new_message.reply_thread),
            (second_space, second_thread),
        )

    def test_failed_delivery_in_one_session_does_not_block_another(self) -> None:
        second_key = "agent:main:gchat:two:root"
        second_id = "session-two"
        second_file = self.sessions_dir / f"{second_id}.jsonl"
        (self.sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    self.session_key: {"sessionId": self.session_id},
                    second_key: {"sessionId": second_id},
                }
            ),
            encoding="utf-8",
        )
        self.trajectory_file.touch()
        second_file.touch()
        self.prepare_session()
        self.prepare_session(
            second_key,
            space="spaces/two",
            identity_thread="spaces/two/threads/root",
            reply_thread="spaces/two/threads/root",
        )
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "retry first session"),
        )
        self.append_line(
            second_file,
            trajectory_entry("assistant", "deliver second session"),
        )

        failed_once = False

        def deliver(message: AssistantTrajectoryMessage) -> bool:
            nonlocal failed_once
            if message.session_key == self.session_key and not failed_once:
                failed_once = True
                return False
            return True

        self.delivery.side_effect = deliver

        self.watcher._poll_once()
        self.watcher._poll_once()

        delivered_messages = [
            invocation.args[0] for invocation in self.delivery.call_args_list
        ]
        self.assertEqual(
            [message.text for message in delivered_messages],
            [
                "retry first session",
                "deliver second session",
                "retry first session",
            ],
        )
        first_attempt, second_session, retry = delivered_messages
        self.assertEqual(first_attempt.delivery_id, retry.delivery_id)
        self.assertEqual(
            (second_session.space, second_session.reply_thread),
            ("spaces/two", "spaces/two/threads/root"),
        )

    def test_delivery_exception_in_one_session_isolated_until_poll_finishes(
        self,
    ) -> None:
        second_key = "agent:main:gchat:two:root"
        second_id = "session-two"
        second_file = self.sessions_dir / f"{second_id}.jsonl"
        (self.sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    self.session_key: {"sessionId": self.session_id},
                    second_key: {"sessionId": second_id},
                }
            ),
            encoding="utf-8",
        )
        self.trajectory_file.touch()
        second_file.touch()
        self.prepare_session()
        self.prepare_session(
            second_key,
            space="spaces/two",
            identity_thread="spaces/two/threads/root",
            reply_thread="spaces/two/threads/root",
        )
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "broken delivery"),
        )
        self.append_line(
            second_file,
            trajectory_entry("assistant", "healthy delivery"),
        )

        def deliver(message: AssistantTrajectoryMessage) -> bool:
            if message.session_key == self.session_key:
                raise RuntimeError("Chat unavailable")
            return True

        self.delivery.side_effect = deliver

        with self.assertRaisesRegex(RuntimeError, "Chat unavailable"):
            self.watcher._poll_once()

        self.assertEqual(self.delivery.call_count, 2)
        self.assertEqual(
            self.delivery.call_args_list[1].args[0].text,
            "healthy delivery",
        )

    def test_identical_payloads_in_different_sessions_are_both_delivered(
        self,
    ) -> None:
        second_key = "agent:main:gchat:two:root"
        second_id = "session-two"
        second_file = self.sessions_dir / f"{second_id}.jsonl"
        (self.sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    self.session_key: {"sessionId": self.session_id},
                    second_key: {"sessionId": second_id},
                }
            ),
            encoding="utf-8",
        )
        self.trajectory_file.touch()
        second_file.touch()
        self.prepare_session()
        self.prepare_session(
            second_key,
            space="spaces/two",
            identity_thread="spaces/two/threads/root",
            reply_thread="spaces/two/threads/root",
        )
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "same answer"),
        )
        self.append_line(
            second_file,
            trajectory_entry("assistant", "same answer"),
        )

        self.watcher._poll_once()

        self.assertEqual(self.delivery.call_count, 2)
        self.assertEqual(
            [
                invocation.args[0].session_key
                for invocation in self.delivery.call_args_list
            ],
            [self.session_key, second_key],
        )

    def test_registered_session_cannot_be_rebound_within_the_same_space(
        self,
    ) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()
        latest_thread = "spaces/one/threads/latest-root"
        with self.assertRaisesRegex(ValueError, "another reply thread"):
            self.prepare_session(reply_thread=latest_thread)

    def test_registered_session_cannot_be_rebound_to_another_space(self) -> None:
        self.prepare_session()

        with self.assertRaisesRegex(ValueError, "another Chat space"):
            self.prepare_session(
                space="spaces/other",
                identity_thread="spaces/other/threads/root",
                reply_thread="spaces/other/threads/root",
            )

    def test_session_key_cannot_claim_a_different_identity_thread(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            self.prepare_session(
                identity_thread="spaces/one/threads/different",
            )

    def test_lossy_legacy_lowercase_key_cannot_claim_mixed_case_route(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            self.watcher.prepare_session(
                "agent:main:gchat:aaqajea3dp8:zz9",
                "spaces/AAQAjEa3Dp8",
                "spaces/AAQAjEa3Dp8/threads/Zz9",
                "",
            )

    def test_reply_thread_must_be_canonical_and_belong_to_space(self) -> None:
        invalid_reply_threads = (
            "threads/root",
            "spaces/two/threads/root",
        )

        for reply_thread in invalid_reply_threads:
            with (
                self.subTest(reply_thread=reply_thread),
                self.assertRaises((TypeError, ValueError)),
            ):
                self.prepare_session(reply_thread=reply_thread)

    def test_extractor_ignores_non_assistant_and_empty_messages(self) -> None:
        self.assertIsNone(extract_assistant_text({"type": "event"}))
        self.assertIsNone(
            extract_assistant_text(
                {"type": "message", "message": {"role": "user", "content": "hi"}}
            )
        )
        self.assertIsNone(
            extract_assistant_text(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "name": "search"}],
                    },
                }
            )
        )

    def test_parser_strips_empty_and_invalid_full_line_directives(self) -> None:
        text, paths = parse_media_directives(
            "before\nMEDIA:\nMEDIA:relative.png\nafter"
        )

        self.assertEqual(text, "before\nafter")
        self.assertEqual(paths, ("relative.png",))

    def test_identical_text_from_async_tool_final_copy_is_not_redelivered(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()

        # async tool run: model text accompanies the toolCall ...
        self.append_line(
            self.trajectory_file,
            trajectory_entry(
                "assistant",
                [
                    {"type": "text", "text": "สร้างภาพให้นะ 🎲"},
                    {"type": "toolCall", "name": "image_generate"},
                ],
            ),
        )
        # ... and the gateway re-writes the same text as the run's final message
        self.append_line(
            self.trajectory_file,
            trajectory_entry(
                "assistant",
                [{"type": "text", "text": "สร้างภาพให้นะ 🎲"}],
            ),
        )
        self.watcher._poll_once()

        self.delivery.assert_called_once()
        self.assertEqual(self.delivery.call_args.args[0].text, "สร้างภาพให้นะ 🎲")

    def test_different_narration_texts_are_all_delivered(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()

        self.append_line(
            self.trajectory_file,
            trajectory_entry(
                "assistant",
                [
                    {"type": "text", "text": "Let me search first."},
                    {"type": "toolCall", "name": "web_search"},
                ],
            ),
        )
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "Here is the answer."),
        )
        self.watcher._poll_once()

        self.assertEqual(self.delivery.call_count, 2)
        self.assertEqual(
            self.delivery.call_args_list[0].args[0].text, "Let me search first."
        )
        self.assertEqual(
            self.delivery.call_args_list[1].args[0].text, "Here is the answer."
        )

    def test_same_text_is_delivered_again_after_the_window(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.prepare_session()

        self.append_line(self.trajectory_file, trajectory_entry("assistant", "same"))
        self.watcher._poll_once()
        self.delivery.assert_called_once()

        # simulate the dedupe window having elapsed
        for key in list(self.watcher._delivered_fingerprints):
            self.watcher._delivered_fingerprints[key] -= 1000.0

        self.append_line(self.trajectory_file, trajectory_entry("assistant", "same"))
        self.watcher._poll_once()

        self.assertEqual(self.delivery.call_count, 2)


if __name__ == "__main__":
    unittest.main()
