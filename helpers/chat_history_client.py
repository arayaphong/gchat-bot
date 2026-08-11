from __future__ import annotations

import random
import re
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from enum import Enum
from typing import Any

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from helpers.services import CredentialService

CHAT_READ_TIMEOUT_SECONDS = 15
CHAT_READ_MAX_ATTEMPTS = 5
CHAT_READ_MAX_ELAPSED_SECONDS = 300.0
CHAT_READ_BACKOFF_CAP_SECONDS = 30.0
CHAT_LIST_PAGE_SIZE = 1000
CHAT_LIST_MAX_PAGES = 1000
CHAT_SPACE_FIELDS = "name,spaceType,singleUserBotDm"
CHAT_LIST_FIELDS = "messages(name,createTime,sender(name,type)),nextPageToken"

_RESOURCE_SEGMENT = r"[^\s/\x00-\x1f\x7f]+"
_USER_NAME_RE = re.compile(rf"^users/{_RESOURCE_SEGMENT}$")
_SPACE_NAME_RE = re.compile(rf"^spaces/{_RESOURCE_SEGMENT}$")


class ChatHistoryClientErrorCode(str, Enum):
    UNAUTHORIZED_USER = "unauthorized_user"
    UNAUTHORIZED_SPACE = "unauthorized_space"
    INVALID_RESOURCE_NAME = "invalid_resource_name"
    INVALID_SPACE_RESPONSE = "invalid_space_response"
    NOT_DIRECT_MESSAGE = "not_direct_message"
    API_FAILURE = "api_failure"
    RETRY_EXHAUSTED = "retry_exhausted"
    INVALID_PAGE = "invalid_page"
    PAGINATION_CYCLE = "pagination_cycle"
    PAGE_LIMIT = "page_limit"


class ChatHistoryClientError(RuntimeError):
    """A safe read-side failure which never embeds an API response body."""

    def __init__(self, code: ChatHistoryClientErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class ChatHistoryClient:
    """Read-only Google Chat metadata client for one allowlisted DM."""

    def __init__(
        self,
        credential_service: CredentialService,
        *,
        allowed_user: str,
        allowed_space: str,
        api_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = CHAT_READ_MAX_ATTEMPTS,
        max_elapsed_seconds: float = CHAT_READ_MAX_ELAPSED_SECONDS,
        max_pages: int = CHAT_LIST_MAX_PAGES,
    ) -> None:
        self._credential_service = credential_service
        self.allowed_user = allowed_user
        self.allowed_space = allowed_space
        self._api_factory = api_factory
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._max_attempts = max_attempts
        self._max_elapsed_seconds = max_elapsed_seconds
        self._max_pages = max_pages

        if (
            _USER_NAME_RE.fullmatch(allowed_user) is None
            or _SPACE_NAME_RE.fullmatch(allowed_space) is None
            or max_attempts < 1
            or max_elapsed_seconds <= 0
            or max_pages < 1
        ):
            raise ValueError("invalid Chat history client configuration")

    def validate_authority(
        self, actor_name: str | None, space_name: str | None
    ) -> None:
        """Fail closed before credentials, discovery, or any remote API call."""

        if (
            not isinstance(actor_name, str)
            or _USER_NAME_RE.fullmatch(actor_name) is None
        ):
            raise ChatHistoryClientError(
                ChatHistoryClientErrorCode.INVALID_RESOURCE_NAME
            )
        if actor_name != self.allowed_user:
            raise ChatHistoryClientError(ChatHistoryClientErrorCode.UNAUTHORIZED_USER)
        if (
            not isinstance(space_name, str)
            or _SPACE_NAME_RE.fullmatch(space_name) is None
        ):
            raise ChatHistoryClientError(
                ChatHistoryClientErrorCode.INVALID_RESOURCE_NAME
            )
        if space_name != self.allowed_space:
            raise ChatHistoryClientError(ChatHistoryClientErrorCode.UNAUTHORIZED_SPACE)

    def _build_api(self) -> Any:
        if self._api_factory is not None:
            return self._api_factory()
        authed_http = google_auth_httplib2.AuthorizedHttp(
            self._credential_service.get_user_creds(),
            http=httplib2.Http(timeout=CHAT_READ_TIMEOUT_SECONDS),
        )
        return build("chat", "v1", http=authed_http, cache_discovery=False)

    @staticmethod
    def _is_transient(error: BaseException) -> bool:
        if isinstance(error, HttpError):
            status = getattr(error.resp, "status", None)
            return status == 429 or (isinstance(status, int) and status >= 500)
        return isinstance(
            error,
            (TimeoutError, ConnectionError, socket.timeout, OSError),
        )

    def _execute(self, request_factory: Callable[[], Any]) -> Any:
        started = self._clock()
        for attempt in range(1, self._max_attempts + 1):
            try:
                return request_factory().execute(num_retries=0)
            except Exception as error:  # noqa: BLE001
                if not self._is_transient(error):
                    raise ChatHistoryClientError(
                        ChatHistoryClientErrorCode.API_FAILURE
                    ) from None
                elapsed = self._clock() - started
                if (
                    attempt >= self._max_attempts
                    or elapsed >= self._max_elapsed_seconds
                ):
                    raise ChatHistoryClientError(
                        ChatHistoryClientErrorCode.RETRY_EXHAUSTED
                    ) from None
                ceiling = min(
                    CHAT_READ_BACKOFF_CAP_SECONDS,
                    float(2 ** (attempt - 1)),
                )
                delay = max(0.0, self._jitter(0.0, ceiling))
                if elapsed + delay > self._max_elapsed_seconds:
                    raise ChatHistoryClientError(
                        ChatHistoryClientErrorCode.RETRY_EXHAUSTED
                    ) from None
                self._sleeper(delay)

        raise ChatHistoryClientError(ChatHistoryClientErrorCode.RETRY_EXHAUSTED)

    def iter_messages(
        self,
        *,
        actor_name: str | None,
        space_name: str | None,
    ) -> Iterator[Mapping[str, Any]]:
        yield from self._iter_messages(
            actor_name=actor_name,
            space_name=space_name,
            create_time_filter=None,
        )

    def iter_clear_candidates(
        self,
        *,
        actor_name: str | None,
        space_name: str | None,
        cutoff_utc: str,
    ) -> Iterator[Mapping[str, Any]]:
        if (
            not isinstance(cutoff_utc, str)
            or len(cutoff_utc) > 64
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
                cutoff_utc,
            )
            is None
        ):
            raise ChatHistoryClientError(
                ChatHistoryClientErrorCode.INVALID_RESOURCE_NAME
            )
        yield from self._iter_messages(
            actor_name=actor_name,
            space_name=space_name,
            create_time_filter=f'createTime < "{cutoff_utc}"',
        )

    def _iter_messages(
        self,
        *,
        actor_name: str | None,
        space_name: str | None,
        create_time_filter: str | None,
    ) -> Iterator[Mapping[str, Any]]:
        self.validate_authority(actor_name, space_name)
        if not isinstance(space_name, str):
            raise ChatHistoryClientError(
                ChatHistoryClientErrorCode.INVALID_RESOURCE_NAME
            )
        api = self._build_api()

        space = self._execute(
            lambda: api.spaces().get(name=space_name, fields=CHAT_SPACE_FIELDS)
        )
        if not isinstance(space, Mapping) or space.get("name") != space_name:
            raise ChatHistoryClientError(
                ChatHistoryClientErrorCode.INVALID_SPACE_RESPONSE
            )
        if (
            space.get("spaceType") != "DIRECT_MESSAGE"
            or space.get("singleUserBotDm") is not True
        ):
            raise ChatHistoryClientError(ChatHistoryClientErrorCode.NOT_DIRECT_MESSAGE)

        base_params: dict[str, Any] = {
            "parent": space_name,
            "pageSize": CHAT_LIST_PAGE_SIZE,
            "showDeleted": False,
            "fields": CHAT_LIST_FIELDS,
        }
        if create_time_filter is not None:
            base_params["filter"] = create_time_filter
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for page_number in range(self._max_pages):
            params = dict(base_params)
            if page_token is not None:
                params["pageToken"] = page_token

            page = self._execute(
                lambda params=params: api.spaces().messages().list(**params)
            )
            if not isinstance(page, Mapping):
                raise ChatHistoryClientError(ChatHistoryClientErrorCode.INVALID_PAGE)

            messages = page.get("messages", [])
            if not isinstance(messages, list):
                raise ChatHistoryClientError(ChatHistoryClientErrorCode.INVALID_PAGE)
            for message in messages:
                if not isinstance(message, Mapping):
                    raise ChatHistoryClientError(
                        ChatHistoryClientErrorCode.INVALID_PAGE
                    )
                yield message

            next_token = page.get("nextPageToken")
            if next_token in (None, ""):
                return
            if not isinstance(next_token, str):
                raise ChatHistoryClientError(ChatHistoryClientErrorCode.INVALID_PAGE)
            if next_token in seen_tokens:
                raise ChatHistoryClientError(
                    ChatHistoryClientErrorCode.PAGINATION_CYCLE
                )
            seen_tokens.add(next_token)
            page_token = next_token

            if page_number + 1 >= self._max_pages:
                raise ChatHistoryClientError(ChatHistoryClientErrorCode.PAGE_LIMIT)


__all__ = [
    "CHAT_LIST_FIELDS",
    "CHAT_LIST_MAX_PAGES",
    "CHAT_LIST_PAGE_SIZE",
    "CHAT_READ_MAX_ATTEMPTS",
    "CHAT_READ_MAX_ELAPSED_SECONDS",
    "CHAT_READ_TIMEOUT_SECONDS",
    "CHAT_SPACE_FIELDS",
    "ChatHistoryClient",
    "ChatHistoryClientError",
    "ChatHistoryClientErrorCode",
]
