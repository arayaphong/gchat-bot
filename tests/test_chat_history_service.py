from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, ClassVar
from unittest.mock import Mock

from helpers.chat_history_client import (
    ChatHistoryClientError,
    ChatHistoryClientErrorCode,
)
from helpers.chat_history_service import (
    ChatHistoryPresenter,
    ChatHistoryService,
    ChatHistoryStatsError,
    ChatHistoryStatsErrorCode,
    ChatMessageStats,
    reduce_chat_message_stats,
)
from helpers.services import CredentialMissingGrantedScopesError

USER = "users/allowed-user"
SPACE = "spaces/allowed-dm"
THREAD = f"{SPACE}/threads/thread-one"
SOURCE = f"{SPACE}/messages/source-command"


def message(
    identifier: str,
    created: str,
    sender_type: str | None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": f"{SPACE}/messages/{identifier}",
        "createTime": created,
    }
    if sender_type is not None:
        value["sender"] = {"name": "users/sender", "type": sender_type}
    return value


class InlineThread:
    def __init__(self, *, target: Any, kwargs: dict[str, Any], **_options: Any) -> None:
        self.target = target
        self.kwargs = kwargs

    def start(self) -> None:
        self.target(**self.kwargs)


class DeferredThread(InlineThread):
    instances: ClassVar[list[DeferredThread]] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def run(self) -> None:
        self.target(**self.kwargs)


class ChatHistoryReducerTests(unittest.TestCase):
    def test_stream_reducer_excludes_source_and_does_not_assume_order(self) -> None:
        records = [
            message("latest", "2026-08-10T18:00:00.000000001Z", "BOT"),
            {"name": SOURCE},
            message("first", "2026-08-09T18:00:00Z", "HUMAN"),
            message("middle", "2026-08-10T00:00:00+00:00", None),
            message("unknown", "2026-08-10T01:00:00Z", "TYPE_UNSPECIFIED"),
        ]

        stats = reduce_chat_message_stats(
            iter(records), space_name=SPACE, source_message_name=SOURCE
        )

        self.assertEqual(
            (stats.total, stats.human, stats.bot, stats.unknown),
            (4, 1, 1, 2),
        )
        self.assertEqual(
            stats.first_create_time,
            datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            stats.latest_create_time,
            datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(stats.duration_seconds or 0, 86_400.000000001)

    def test_malformed_record_aborts_instead_of_returning_partial_stats(self) -> None:
        malformed_records = (
            {"createTime": "2026-08-10T00:00:00Z"},
            message("bad-time", "not-a-time", "HUMAN"),
            {
                "name": f"{SPACE}/messages/bad-sender",
                "createTime": "2026-08-10T00:00:00Z",
                "sender": "private-content",
            },
            message("wrong-space", "2026-08-10T00:00:00Z", "HUMAN")
            | {"name": "spaces/elsewhere/messages/wrong"},
        )
        for record in malformed_records:
            with self.subTest(record=record):
                with self.assertRaises(ChatHistoryStatsError) as raised:
                    reduce_chat_message_stats(
                        [record], space_name=SPACE, source_message_name=SOURCE
                    )
                self.assertEqual(
                    raised.exception.code,
                    ChatHistoryStatsErrorCode.MALFORMED_MESSAGE,
                )

    def test_source_must_be_an_exact_child_of_the_allowed_space(self) -> None:
        with self.assertRaises(ChatHistoryStatsError) as raised:
            reduce_chat_message_stats(
                [],
                space_name=SPACE,
                source_message_name="spaces/other/messages/source",
            )
        self.assertEqual(
            raised.exception.code,
            ChatHistoryStatsErrorCode.INVALID_SOURCE_MESSAGE,
        )


class ChatHistoryPresenterTests(unittest.TestCase):
    def paragraphs(self, card: dict[str, Any]) -> list[str]:
        widgets = card["cardsV2"][0]["card"]["sections"][0]["widgets"]
        return [widget["textParagraph"]["text"] for widget in widgets]

    def test_zero_state_has_counts_but_no_time_range(self) -> None:
        card = ChatHistoryPresenter().build_stats_message(
            ChatMessageStats(0, 0, 0, 0, None, None, None)
        )
        self.assertEqual(
            card["cardsV2"][0]["card"]["header"]["title"],
            "🛠️ ผู้ดูแลระบบ",
        )
        rendered = " ".join(self.paragraphs(card))
        self.assertIn("ข้อความทั้งหมด: 0", rendered)
        self.assertNotIn("ข้อความแรก", rendered)
        self.assertNotIn("ช่วงเวลา", rendered)

    def test_nonzero_state_renders_bangkok_times_and_range(self) -> None:
        card = ChatHistoryPresenter().build_stats_message(
            ChatMessageStats(
                2,
                1,
                1,
                0,
                datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 11, 19, 2, 3, tzinfo=timezone.utc),
                90_123,
            )
        )
        rendered = " ".join(self.paragraphs(card))
        self.assertIn("11/08/2026 01:00:00", rendered)
        self.assertIn("12/08/2026 02:02:03", rendered)
        self.assertIn("1 วัน 1 ชั่วโมง 2 นาที 3 วินาที", rendered)

    def test_failure_cards_do_not_render_exception_or_api_values(self) -> None:
        presenter = ChatHistoryPresenter()
        rendered = " ".join(
            self.paragraphs(presenter.build_failure_message("api_failure"))
        )
        self.assertNotIn("api_failure", rendered)
        self.assertNotIn("response", rendered.lower())


class ChatHistoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        DeferredThread.instances.clear()
        self.client = Mock()
        self.client.allowed_space = SPACE
        self.gateway = Mock()
        self.gateway.send_structured_card.return_value = {
            "name": f"{SPACE}/messages/stats"
        }

    def test_valid_stats_are_read_and_sent_in_background_with_stable_id(self) -> None:
        self.client.iter_messages.return_value = [
            {"name": SOURCE},
            message("one", "2026-08-10T00:00:00Z", "HUMAN"),
        ]
        service = ChatHistoryService(
            self.client,
            self.gateway,
            thread_factory=DeferredThread,
        )

        self.assertTrue(
            service.submit_stats(
                actor_name=USER,
                space_name=SPACE,
                thread_name=THREAD,
                source_message_name=SOURCE,
            )
        )
        self.client.iter_messages.assert_not_called()
        self.gateway.send_structured_card.assert_not_called()

        DeferredThread.instances[0].run()

        self.client.validate_authority.assert_called_once_with(USER, SPACE)
        self.client.iter_messages.assert_called_once_with(
            actor_name=USER, space_name=SPACE
        )
        call = self.gateway.send_structured_card.call_args
        self.assertEqual(call.args[:2], (SPACE, THREAD))
        self.assertRegex(call.kwargs["message_id"], r"^client-jinx-hs-[0-9a-f]{32}$")
        self.assertEqual(
            call.kwargs["message_id"],
            ChatHistoryService.deterministic_message_id(SOURCE),
        )

    def test_duplicate_source_has_same_custom_message_id(self) -> None:
        first = ChatHistoryService.deterministic_message_id(SOURCE)
        second = ChatHistoryService.deterministic_message_id(SOURCE)
        other = ChatHistoryService.deterministic_message_id(
            f"{SPACE}/messages/other-source"
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_wrong_user_or_space_never_lists_messages(self) -> None:
        for code in (
            ChatHistoryClientErrorCode.UNAUTHORIZED_USER,
            ChatHistoryClientErrorCode.UNAUTHORIZED_SPACE,
        ):
            with self.subTest(code=code):
                self.client.reset_mock()
                self.gateway.reset_mock()
                self.client.validate_authority.side_effect = ChatHistoryClientError(
                    code
                )
                service = ChatHistoryService(
                    self.client,
                    self.gateway,
                    thread_factory=InlineThread,
                )
                requested_space = (
                    "spaces/elsewhere"
                    if code is ChatHistoryClientErrorCode.UNAUTHORIZED_SPACE
                    else SPACE
                )
                service.submit_stats(
                    actor_name=USER,
                    space_name=requested_space,
                    thread_name=f"{requested_space}/threads/thread-one",
                    source_message_name=SOURCE,
                )
                self.client.iter_messages.assert_not_called()
                if code is ChatHistoryClientErrorCode.UNAUTHORIZED_USER:
                    self.gateway.send_structured_card.assert_called_once()
                else:
                    self.gateway.send_structured_card.assert_not_called()

    def test_missing_source_never_lists_messages(self) -> None:
        service = ChatHistoryService(
            self.client,
            self.gateway,
            thread_factory=InlineThread,
        )
        service.submit_stats(
            actor_name=USER,
            space_name=SPACE,
            thread_name=THREAD,
            source_message_name=None,
        )
        self.client.iter_messages.assert_not_called()
        rendered = repr(self.gateway.send_structured_card.call_args.args[2])
        self.assertIn("ไม่พบตัวตนข้อความคำสั่ง", rendered)

    def test_missing_scope_gets_explicit_reauthorization_card(self) -> None:
        self.client.iter_messages.side_effect = CredentialMissingGrantedScopesError(
            ("https://www.googleapis.com/auth/chat.messages",)
        )
        service = ChatHistoryService(
            self.client,
            self.gateway,
            thread_factory=InlineThread,
        )
        service.submit_stats(
            actor_name=USER,
            space_name=SPACE,
            thread_name=THREAD,
            source_message_name=SOURCE,
        )
        rendered = repr(self.gateway.send_structured_card.call_args.args[2])
        self.assertIn("อนุญาตบัญชีผู้ใช้อีกครั้ง", rendered)


if __name__ == "__main__":
    unittest.main()
