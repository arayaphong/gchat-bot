from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from helpers.session_trajectory_watcher import (
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
        self.session_key = "agent:main:gchat:c0ffee"
        self.session_id = "session-one"
        self.trajectory_file = self.sessions_dir / f"{self.session_id}.jsonl"
        self.delivery = Mock(return_value=True)
        self.watcher = SessionTrajectoryWatcher(
            self.delivery,
            sessions_dir=self.sessions_dir,
            poll_seconds=0.01,
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
        self.watcher.prepare_session(self.session_key)

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
        self.assertEqual(message.text, "new answer")
        self.assertEqual(message.timestamp, "2026-08-05T12:00:00Z")

    def test_session_created_after_prepare_starts_from_its_first_line(self) -> None:
        self.watcher.prepare_session(self.session_key)
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
        self.watcher.prepare_session(self.session_key)
        self.append_line(
            self.trajectory_file,
            trajectory_entry("assistant", "background answer"),
        )

        self.assertTrue(delivered.wait(timeout=1))
        self.assertEqual(self.delivery.call_args.args[0].text, "background answer")

    def test_failed_delivery_retries_the_same_message_and_delivery_id(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.watcher.prepare_session(self.session_key)
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

    def test_media_directives_are_stripped_deduplicated_and_delivered(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.watcher.prepare_session(self.session_key)
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
        self.watcher.prepare_session(self.session_key)
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

    def test_switching_sessions_stops_reading_the_previous_trajectory(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.watcher.prepare_session(self.session_key)

        second_key = "agent:main:gchat:decade"
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
        self.watcher.prepare_session(second_key)
        self.append_line(
            self.trajectory_file, trajectory_entry("assistant", "old late")
        )
        self.append_line(second_file, trajectory_entry("assistant", "new session"))

        self.watcher._poll_once()

        self.delivery.assert_called_once()
        self.assertEqual(self.delivery.call_args.args[0].text, "new session")

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
        self.watcher.prepare_session(self.session_key)

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
        self.watcher.prepare_session(self.session_key)

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
        self.assertEqual(self.delivery.call_args_list[0].args[0].text, "Let me search first.")
        self.assertEqual(self.delivery.call_args_list[1].args[0].text, "Here is the answer.")

    def test_same_text_is_delivered_again_after_the_window(self) -> None:
        self.write_index(self.session_key, self.session_id)
        self.trajectory_file.touch()
        self.watcher.prepare_session(self.session_key)

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
