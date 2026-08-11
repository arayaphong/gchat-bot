from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

from helpers.chat_history_client import (
    CHAT_LIST_FIELDS,
    CHAT_SPACE_FIELDS,
    ChatHistoryClient,
    ChatHistoryClientError,
    ChatHistoryClientErrorCode,
)

USER = "users/allowed-user"
SPACE = "spaces/allowed-dm"


class FakeRequest:
    def __init__(self, action: Callable[[], Any]) -> None:
        self._action = action
        self.execute_calls: list[int] = []

    def execute(self, *, num_retries: int) -> Any:
        self.execute_calls.append(num_retries)
        return self._action()


class FakeMessages:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = list(pages)
        self.list_params: list[dict[str, Any]] = []
        self.delete_calls = 0

    def list(self, **params: Any) -> FakeRequest:
        self.list_params.append(dict(params))
        return FakeRequest(lambda: self.pages.pop(0))

    def delete(self, **_params: Any) -> FakeRequest:
        self.delete_calls += 1
        return FakeRequest(dict)


class FakeSpaces:
    def __init__(self, space_response: Any, pages: list[Any]) -> None:
        self.space_response = space_response
        self.get_params: list[dict[str, Any]] = []
        self.messages_api = FakeMessages(pages)

    def get(self, **params: Any) -> FakeRequest:
        self.get_params.append(dict(params))
        return FakeRequest(lambda: self.space_response)

    def messages(self) -> FakeMessages:
        return self.messages_api


class FakeApi:
    def __init__(self, space_response: Any, pages: list[Any]) -> None:
        self.spaces_api = FakeSpaces(space_response, pages)

    def spaces(self) -> FakeSpaces:
        return self.spaces_api


def direct_message() -> dict[str, Any]:
    return {
        "name": SPACE,
        "spaceType": "DIRECT_MESSAGE",
        "singleUserBotDm": True,
    }


class ChatHistoryClientTests(unittest.TestCase):
    def make_client(
        self,
        api: FakeApi,
        **kwargs: Any,
    ) -> ChatHistoryClient:
        return ChatHistoryClient(
            Mock(),
            allowed_user=USER,
            allowed_space=SPACE,
            api_factory=lambda: api,
            **kwargs,
        )

    def test_allowlist_is_checked_before_api_factory_or_credentials(self) -> None:
        factory = Mock()
        credentials = Mock()
        client = ChatHistoryClient(
            credentials,
            allowed_user=USER,
            allowed_space=SPACE,
            api_factory=factory,
        )

        for actor, space, expected in (
            ("users/intruder", SPACE, ChatHistoryClientErrorCode.UNAUTHORIZED_USER),
            (USER, "spaces/elsewhere", ChatHistoryClientErrorCode.UNAUTHORIZED_SPACE),
            (None, SPACE, ChatHistoryClientErrorCode.INVALID_RESOURCE_NAME),
        ):
            with self.subTest(actor=actor, space=space):
                with self.assertRaises(ChatHistoryClientError) as raised:
                    list(client.iter_messages(actor_name=actor, space_name=space))
                self.assertEqual(raised.exception.code, expected)

        factory.assert_not_called()
        credentials.get_user_creds.assert_not_called()

    def test_get_validates_canonical_single_user_direct_message_before_list(
        self,
    ) -> None:
        invalid_responses = (
            {},
            {
                "name": "spaces/other",
                "spaceType": "DIRECT_MESSAGE",
                "singleUserBotDm": True,
            },
            {"name": SPACE, "spaceType": "SPACE", "singleUserBotDm": True},
            {"name": SPACE, "spaceType": "DIRECT_MESSAGE", "singleUserBotDm": False},
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                api = FakeApi(response, [{"messages": []}])
                with self.assertRaises(ChatHistoryClientError):
                    list(
                        self.make_client(api).iter_messages(
                            actor_name=USER, space_name=SPACE
                        )
                    )
                self.assertEqual(
                    api.spaces_api.get_params,
                    [{"name": SPACE, "fields": CHAT_SPACE_FIELDS}],
                )
                self.assertEqual(api.spaces_api.messages_api.list_params, [])
                self.assertEqual(api.spaces_api.messages_api.delete_calls, 0)

    def test_empty_and_missing_messages_are_valid_pages(self) -> None:
        for page in ({}, {"messages": []}):
            with self.subTest(page=page):
                api = FakeApi(direct_message(), [page])
                self.assertEqual(
                    list(
                        self.make_client(api).iter_messages(
                            actor_name=USER, space_name=SPACE
                        )
                    ),
                    [],
                )

    def test_pagination_changes_only_page_token_and_handles_over_1000(self) -> None:
        first_page = [{"name": f"message-{index}"} for index in range(1000)]
        last = {"name": "message-1000"}
        api = FakeApi(
            direct_message(),
            [
                {"messages": first_page, "nextPageToken": "page-two"},
                {"messages": [last]},
            ],
        )

        messages = list(
            self.make_client(api).iter_messages(actor_name=USER, space_name=SPACE)
        )

        self.assertEqual(len(messages), 1001)
        base = {
            "parent": SPACE,
            "pageSize": 1000,
            "showDeleted": False,
            "fields": CHAT_LIST_FIELDS,
        }
        self.assertEqual(
            api.spaces_api.messages_api.list_params,
            [base, {**base, "pageToken": "page-two"}],
        )
        self.assertNotIn("text", CHAT_LIST_FIELDS)
        self.assertNotIn("cards", CHAT_LIST_FIELDS)
        self.assertNotIn("attachment", CHAT_LIST_FIELDS)
        self.assertEqual(api.spaces_api.messages_api.delete_calls, 0)

    def test_clear_preview_uses_exact_strict_cutoff_filter_on_every_page(
        self,
    ) -> None:
        api = FakeApi(
            direct_message(),
            [
                {"messages": [], "nextPageToken": "two"},
                {"messages": []},
            ],
        )
        cutoff = "2026-08-10T00:00:00.000000Z"

        self.assertEqual(
            list(
                self.make_client(api).iter_clear_candidates(
                    actor_name=USER,
                    space_name=SPACE,
                    cutoff_utc=cutoff,
                )
            ),
            [],
        )

        base = {
            "parent": SPACE,
            "pageSize": 1000,
            "showDeleted": False,
            "fields": CHAT_LIST_FIELDS,
            "filter": f'createTime < "{cutoff}"',
        }
        self.assertEqual(
            api.spaces_api.messages_api.list_params,
            [base, {**base, "pageToken": "two"}],
        )
        self.assertEqual(api.spaces_api.messages_api.delete_calls, 0)

    def test_repeated_page_token_aborts(self) -> None:
        api = FakeApi(
            direct_message(),
            [
                {"nextPageToken": "same"},
                {"nextPageToken": "same"},
            ],
        )
        with self.assertRaises(ChatHistoryClientError) as raised:
            list(self.make_client(api).iter_messages(actor_name=USER, space_name=SPACE))
        self.assertEqual(
            raised.exception.code, ChatHistoryClientErrorCode.PAGINATION_CYCLE
        )
        self.assertEqual(api.spaces_api.messages_api.delete_calls, 0)

    def test_page_limit_is_fail_closed(self) -> None:
        api = FakeApi(direct_message(), [{"nextPageToken": "more"}])
        with self.assertRaises(ChatHistoryClientError) as raised:
            list(
                self.make_client(api, max_pages=1).iter_messages(
                    actor_name=USER, space_name=SPACE
                )
            )
        self.assertEqual(raised.exception.code, ChatHistoryClientErrorCode.PAGE_LIMIT)

    def test_transient_timeout_retries_with_no_library_retries(self) -> None:
        attempts = 0
        sleeps: list[float] = []
        api = FakeApi(direct_message(), [{}])

        def flaky_get(**params: Any) -> FakeRequest:
            api.spaces_api.get_params.append(dict(params))

            def action() -> dict[str, Any]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TimeoutError("private transport detail")
                return direct_message()

            return FakeRequest(action)

        api.spaces_api.get = flaky_get  # type: ignore[method-assign]
        client = self.make_client(
            api,
            sleeper=sleeps.append,
            jitter=lambda _low, high: high,
        )

        self.assertEqual(
            list(client.iter_messages(actor_name=USER, space_name=SPACE)), []
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(api.spaces_api.messages_api.delete_calls, 0)

    def test_invalid_page_shape_aborts_without_delete(self) -> None:
        api = FakeApi(direct_message(), [{"messages": "not-a-list"}])
        with self.assertRaises(ChatHistoryClientError) as raised:
            list(self.make_client(api).iter_messages(actor_name=USER, space_name=SPACE))
        self.assertEqual(raised.exception.code, ChatHistoryClientErrorCode.INVALID_PAGE)
        self.assertEqual(api.spaces_api.messages_api.delete_calls, 0)


if __name__ == "__main__":
    unittest.main()
