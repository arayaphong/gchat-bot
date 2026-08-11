from __future__ import annotations

import hashlib
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

import helpers.chat_history_delete as delete_module
from helpers.chat_clear_store import (
    ActionKind,
    ChatClearStore,
    ChatWritePacer,
    ClearJobRequest,
    CredentialPartition,
    ItemStatus,
    JobStatus,
    NotificationStatus,
    SnapshotItem,
)
from helpers.chat_gateway import ChatMessageNotFoundError
from helpers.chat_history_delete import (
    BotChatDeleteClient,
    ChatDeleteApiError,
    ChatDeleteValidationError,
    ChatHistoryDeleteExecutor,
    UserChatDeleteClient,
)

UTC = timezone.utc
USER = "users/allowed-user"
SPACE = "spaces/allowed-dm"


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 11, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeDeleteClient:
    def __init__(
        self,
        partition: CredentialPartition,
        events: list[tuple[str, str]],
    ) -> None:
        self.partition = partition
        self.events = events
        self.outcomes: dict[str, list[BaseException | None]] = {}
        self.refresh_calls = 0
        self.refresh_error: BaseException | None = None

    def delete(self, *, name: str, force: bool = False) -> None:
        self.events.append((self.partition.value, name))
        if force is not False:
            raise AssertionError("force must remain false")
        outcomes = self.outcomes.get(name, [])
        outcome = outcomes.pop(0) if outcomes else None
        if outcome is not None:
            raise outcome

    def refresh_credentials(self) -> None:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error


class FakePacer:
    def __init__(self) -> None:
        self.spaces: list[str] = []

    def wait_for_turn(self, space: str) -> float:
        self.spaces.append(space)
        return 0.0


class FakeGateway:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any], str]] = []
        self.create_error: BaseException | None = None
        self.recovered: dict[str, dict[str, str]] = {}

    def create_structured_card(
        self,
        space: str,
        _thread: str,
        body: dict[str, Any],
        *,
        message_id: str,
    ) -> dict[str, str]:
        self.created.append((space, body, message_id))
        if self.create_error is not None:
            raise self.create_error
        return {"name": f"{space}/messages/{message_id}"}

    def get_message_by_client_id(
        self, space: str, *, message_id: str
    ) -> dict[str, str]:
        try:
            return self.recovered[message_id]
        except KeyError:
            raise ChatMessageNotFoundError(f"not found in {space}") from None


class FakeRequest:
    def __init__(
        self, response: object = None, error: BaseException | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.execute_calls: list[int] = []

    def execute(self, *, num_retries: int) -> object:
        self.execute_calls.append(num_retries)
        if self.error is not None:
            raise self.error
        return self.response


class FakeMessagesApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.requests: list[FakeRequest] = []
        self.error: BaseException | None = None

    def delete(self, **kwargs: Any) -> FakeRequest:
        self.calls.append(kwargs)
        request = FakeRequest({}, self.error)
        self.requests.append(request)
        return request


class FakeSpacesApi:
    def __init__(self) -> None:
        self.messages_api = FakeMessagesApi()

    def messages(self) -> FakeMessagesApi:
        return self.messages_api


class FakeApi:
    def __init__(self) -> None:
        self.spaces_api = FakeSpacesApi()

    def spaces(self) -> FakeSpacesApi:
        return self.spaces_api


def request(source: str) -> ClearJobRequest:
    return ClearJobRequest(
        source_message_name=f"{SPACE}/messages/{source}",
        requester_name=USER,
        space_name=SPACE,
        source_event_time_utc="2026-08-11T00:00:00.000000Z",
        reference_time_utc="2026-08-11T00:00:00.000000Z",
        cutoff_utc="2026-08-10T00:00:00.000000Z",
        display_timezone="Asia/Bangkok",
        normalized_argument="1d",
    )


class ChatDeleteClientTests(unittest.TestCase):
    def test_delete_executor_has_no_processing_gate_or_provider_dependency(
        self,
    ) -> None:
        source = inspect.getsource(delete_module)
        self.assertNotIn("ProcessingGate", source)
        self.assertNotIn("MessageOrchestrator", source)

    def test_user_and_bot_clients_are_separate_and_force_is_always_false(
        self,
    ) -> None:
        user_api = FakeApi()
        bot_api = FakeApi()
        refresh_flags: list[tuple[str, bool]] = []
        user = UserChatDeleteClient(
            object(),  # type: ignore[arg-type]
            allowed_space=SPACE,
            api_factory=lambda refresh: refresh_flags.append(("USER", refresh))
            or user_api,
        )
        bot = BotChatDeleteClient(
            object(),  # type: ignore[arg-type]
            allowed_space=SPACE,
            api_factory=lambda refresh: refresh_flags.append(("BOT", refresh))
            or bot_api,
        )

        user.delete(name=f"{SPACE}/messages/user-one", force=False)
        bot.delete(name=f"{SPACE}/messages/bot-one", force=False)

        self.assertEqual(refresh_flags, [("USER", False), ("BOT", False)])
        self.assertEqual(
            user_api.spaces_api.messages_api.calls,
            [{"name": f"{SPACE}/messages/user-one", "force": False}],
        )
        self.assertEqual(
            bot_api.spaces_api.messages_api.calls,
            [{"name": f"{SPACE}/messages/bot-one", "force": False}],
        )
        self.assertEqual(
            user_api.spaces_api.messages_api.requests[0].execute_calls, [0]
        )
        with self.assertRaises(ChatDeleteValidationError):
            user.delete(name="spaces/other/messages/escape", force=False)
        with self.assertRaises(ChatDeleteValidationError):
            user.delete(name=f"{SPACE}/messages/user-one", force=True)
        self.assertEqual(len(user_api.spaces_api.messages_api.calls), 1)

    def test_refresh_rebuilds_only_the_selected_client(self) -> None:
        calls: list[bool] = []
        client = UserChatDeleteClient(
            object(),  # type: ignore[arg-type]
            allowed_space=SPACE,
            api_factory=lambda refresh: calls.append(refresh) or FakeApi(),
        )

        client.refresh_credentials()
        client.delete(name=f"{SPACE}/messages/one", force=False)

        self.assertEqual(calls, [True])

    def test_http_error_is_sanitized_without_response_body(self) -> None:
        api = FakeApi()
        response = httplib2.Response(
            {"status": "429", "retry-after": "7"}
        )
        api.spaces_api.messages_api.error = HttpError(
            response,
            b"SENTINEL_PRIVATE_GOOGLE_RESPONSE",
            uri="https://chat.googleapis.com/private",
        )
        client = BotChatDeleteClient(
            object(),  # type: ignore[arg-type]
            allowed_space=SPACE,
            api_factory=lambda _refresh: api,
        )

        with self.assertRaises(ChatDeleteApiError) as raised:
            client.delete(name=f"{SPACE}/messages/bot", force=False)

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, "7")
        self.assertNotIn(
            "SENTINEL_PRIVATE_GOOGLE_RESPONSE", str(raised.exception)
        )
        self.assertIsNone(raised.exception.__cause__)


class ChatHistoryDeleteExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name) / "history"
        self.clock = FakeClock()
        self.store = ChatClearStore(self.state_dir, clock=self.clock)
        self.events: list[tuple[str, str]] = []
        self.user_client = FakeDeleteClient(CredentialPartition.USER, self.events)
        self.bot_client = FakeDeleteClient(CredentialPartition.BOT, self.events)
        self.gateway = FakeGateway()
        self.pacer = FakePacer()
        self.enabled = True

    def create_delete_job(
        self,
        source: str,
        items: list[tuple[str, str, CredentialPartition]],
    ) -> str:
        operation_id = self.store.create_job(request(source)).job.operation_id
        self.store.claim_preview(now=self.clock())
        snapshot = []
        for identifier, created, partition in items:
            snapshot.append(
                SnapshotItem(
                    message_name=f"{SPACE}/messages/{identifier}",
                    create_time_utc=created,
                    sender_name=(
                        USER
                        if partition is CredentialPartition.USER
                        else "users/bot"
                        if partition is CredentialPartition.BOT
                        else "users/unknown"
                    ),
                    sender_type=(
                        "HUMAN"
                        if partition is CredentialPartition.USER
                        else "BOT"
                        if partition is CredentialPartition.BOT
                        else "TYPE_UNSPECIFIED"
                    ),
                    credential_partition=partition,
                )
            )
        self.store.append_snapshot_items(operation_id, snapshot)
        self.store.finalize_snapshot(operation_id)
        self.store.bind_confirmation(
            operation_id,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation-{source}",
            confirmation_client_message_id=f"client-confirmation-{source}",
            delivery_generation=1,
            confirm_token_hash=hashlib.sha256(f"confirm-{source}".encode()).digest(),
            cancel_token_hash=hashlib.sha256(f"cancel-{source}".encode()).digest(),
            expires_at=self.clock() + timedelta(minutes=10),
        )
        result = self.store.apply_action(
            action=ActionKind.CONFIRM,
            token_hash=hashlib.sha256(f"confirm-{source}".encode()).digest(),
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation-{source}",
            now=self.clock(),
        )
        self.assertEqual(result.status, JobStatus.DELETE_QUEUED)
        return operation_id

    def executor(
        self,
        *,
        jitter: Any = lambda _low, high: high,
        max_attempts: int = 5,
        pacer: Any | None = None,
    ) -> ChatHistoryDeleteExecutor:
        return ChatHistoryDeleteExecutor(
            self.store,
            self.user_client,  # type: ignore[arg-type]
            self.bot_client,  # type: ignore[arg-type]
            self.gateway,  # type: ignore[arg-type]
            pacer or self.pacer,  # type: ignore[arg-type]
            delete_enabled=lambda: self.enabled,
            clock=self.clock,
            jitter=jitter,
            max_attempts=max_attempts,
        )

    def run_job(self, operation_id: str, executor: ChatHistoryDeleteExecutor) -> None:
        job = self.store.claim_next_delete_job(
            delete_enabled=True, now=self.clock()
        )
        self.assertEqual(job.operation_id, operation_id)  # type: ignore[union-attr]
        executor.handle_delete(job)  # type: ignore[arg-type]

    def test_mixed_partitions_delete_exact_frozen_set_oldest_first(self) -> None:
        operation_id = self.create_delete_job(
            "mixed",
            [
                ("user-later", "2026-08-02T00:00:00Z", CredentialPartition.USER),
                ("bot-z", "2026-08-01T00:00:00Z", CredentialPartition.BOT),
                ("user-a", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("unknown", "2026-07-01T00:00:00Z", CredentialPartition.NONE),
            ],
        )

        self.run_job(operation_id, self.executor())

        self.assertEqual(
            self.events,
            [
                ("BOT", f"{SPACE}/messages/bot-z"),
                ("USER", f"{SPACE}/messages/user-a"),
                ("USER", f"{SPACE}/messages/user-later"),
            ],
        )
        self.assertEqual(self.pacer.spaces, [SPACE, SPACE, SPACE])
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(
            (job.deleted_count, job.skipped_count, job.failed_count),  # type: ignore[union-attr]
            (3, 1, 0),
        )
        self.assertEqual(
            {item.message_name for item in self.store.list_items(operation_id)},
            {
                f"{SPACE}/messages/user-later",
                f"{SPACE}/messages/bot-z",
                f"{SPACE}/messages/user-a",
                f"{SPACE}/messages/unknown",
            },
        )

    def test_404_and_item_403_finalize_partial_without_fallback(self) -> None:
        operation_id = self.create_delete_job(
            "http",
            [
                ("user", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("bot", "2026-08-02T00:00:00Z", CredentialPartition.BOT),
            ],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user"] = [
            ChatDeleteApiError(404)
        ]
        self.bot_client.outcomes[f"{SPACE}/messages/bot"] = [
            ChatDeleteApiError(403, systemic_permission=False)
        ]

        self.run_job(operation_id, self.executor())

        items = self.store.list_items(operation_id)
        self.assertEqual(
            [item.status for item in items],
            [ItemStatus.ALREADY_ABSENT, ItemStatus.FAILED],
        )
        self.assertEqual(
            self.store.get_job(operation_id).status,  # type: ignore[union-attr]
            JobStatus.PARTIAL_FAILED,
        )

    def test_systemic_failure_stops_only_its_partition(self) -> None:
        operation_id = self.create_delete_job(
            "circuit",
            [
                ("user-one", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("user-two", "2026-08-02T00:00:00Z", CredentialPartition.USER),
                ("bot", "2026-08-03T00:00:00Z", CredentialPartition.BOT),
            ],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user-one"] = [
            ChatDeleteApiError(403, systemic_permission=True)
        ]

        self.run_job(operation_id, self.executor())

        self.assertEqual(
            self.events,
            [
                ("USER", f"{SPACE}/messages/user-one"),
                ("BOT", f"{SPACE}/messages/bot"),
            ],
        )
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.PARTIAL_FAILED)  # type: ignore[union-attr]
        self.assertEqual(job.user_partition_error, "partition_permission_failure")  # type: ignore[union-attr]
        self.assertEqual(job.bot_partition_error, "")  # type: ignore[union-attr]

    def test_item_specific_403_does_not_open_partition_circuit(self) -> None:
        operation_id = self.create_delete_job(
            "item-permission",
            [
                ("user-one", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("user-two", "2026-08-02T00:00:00Z", CredentialPartition.USER),
            ],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user-one"] = [
            ChatDeleteApiError(403, systemic_permission=False)
        ]

        self.run_job(operation_id, self.executor())

        self.assertEqual(
            self.events,
            [
                ("USER", f"{SPACE}/messages/user-one"),
                ("USER", f"{SPACE}/messages/user-two"),
            ],
        )
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.PARTIAL_FAILED)  # type: ignore[union-attr]
        self.assertEqual(job.user_partition_error, "")  # type: ignore[union-attr]

    def test_401_refreshes_once_then_retries_same_partition(self) -> None:
        operation_id = self.create_delete_job(
            "refresh",
            [("user", "2026-08-01T00:00:00Z", CredentialPartition.USER)],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user"] = [
            ChatDeleteApiError(401),
            None,
        ]

        self.run_job(operation_id, self.executor())

        self.assertEqual(self.user_client.refresh_calls, 1)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(
            self.store.get_job(operation_id).status,  # type: ignore[union-attr]
            JobStatus.COMPLETED,
        )

    def test_second_401_opens_partition_circuit(self) -> None:
        operation_id = self.create_delete_job(
            "refresh-fail",
            [
                ("user-one", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("user-two", "2026-08-02T00:00:00Z", CredentialPartition.USER),
            ],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user-one"] = [
            ChatDeleteApiError(401),
            ChatDeleteApiError(401),
        ]

        self.run_job(operation_id, self.executor())

        self.assertEqual(self.user_client.refresh_calls, 1)
        self.assertEqual(len(self.events), 2)
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(job.failed_count, 2)  # type: ignore[union-attr]

    def test_401_refresh_failure_fails_only_credential_partition(self) -> None:
        operation_id = self.create_delete_job(
            "refresh-error",
            [
                ("user", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("bot", "2026-08-02T00:00:00Z", CredentialPartition.BOT),
            ],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user"] = [
            ChatDeleteApiError(401)
        ]
        self.user_client.refresh_error = RuntimeError("private-token-response")

        self.run_job(operation_id, self.executor())

        self.assertEqual(self.user_client.refresh_calls, 1)
        self.assertEqual(
            self.events,
            [
                ("USER", f"{SPACE}/messages/user"),
                ("BOT", f"{SPACE}/messages/bot"),
            ],
        )
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.PARTIAL_FAILED)  # type: ignore[union-attr]
        self.assertNotIn(
            b"private-token-response", self.store.database_path.read_bytes()
        )

    def test_401_refresh_budget_is_shared_across_partition_items(self) -> None:
        operation_id = self.create_delete_job(
            "partition-refresh",
            [
                ("user-one", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("user-two", "2026-08-02T00:00:00Z", CredentialPartition.USER),
            ],
        )
        self.user_client.outcomes[f"{SPACE}/messages/user-one"] = [
            ChatDeleteApiError(401),
            None,
        ]
        self.user_client.outcomes[f"{SPACE}/messages/user-two"] = [
            ChatDeleteApiError(401)
        ]

        self.run_job(operation_id, self.executor())

        self.assertEqual(self.user_client.refresh_calls, 1)
        self.assertEqual(len(self.events), 3)
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.PARTIAL_FAILED)  # type: ignore[union-attr]
        self.assertEqual(job.deleted_count, 1)  # type: ignore[union-attr]
        self.assertEqual(job.failed_count, 1)  # type: ignore[union-attr]

    def test_429_retry_after_is_durable_and_does_not_sleep_suite(self) -> None:
        operation_id = self.create_delete_job(
            "rate",
            [("bot", "2026-08-01T00:00:00Z", CredentialPartition.BOT)],
        )
        self.bot_client.outcomes[f"{SPACE}/messages/bot"] = [
            ChatDeleteApiError(429, retry_after="7"),
            None,
        ]
        executor = self.executor()

        self.run_job(operation_id, executor)
        item = self.store.list_items(operation_id)[0]
        self.assertEqual(item.status, ItemStatus.PENDING)
        self.assertEqual(item.next_attempt_at, "2026-08-11T00:00:07.000000Z")
        self.run_job(operation_id, executor)
        self.assertEqual(len(self.events), 1)

        self.clock.advance(7)
        self.run_job(operation_id, executor)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(
            self.store.get_job(operation_id).status,  # type: ignore[union-attr]
            JobStatus.COMPLETED,
        )

    def test_429_http_date_retry_after_uses_injected_wall_clock(self) -> None:
        operation_id = self.create_delete_job(
            "rate-date",
            [("bot", "2026-08-01T00:00:00Z", CredentialPartition.BOT)],
        )
        self.bot_client.outcomes[f"{SPACE}/messages/bot"] = [
            ChatDeleteApiError(
                429, retry_after="Tue, 11 Aug 2026 00:00:09 GMT"
            )
        ]

        self.run_job(operation_id, self.executor())

        item = self.store.list_items(operation_id)[0]
        self.assertEqual(item.next_attempt_at, "2026-08-11T00:00:09.000000Z")

    def test_timeout_and_5xx_exhaust_bounded_attempts(self) -> None:
        for source, failure in (
            ("timeout", TimeoutError("raw-timeout-sentinel")),
            ("reset", ConnectionResetError("raw-reset-sentinel")),
            ("server", ChatDeleteApiError(503)),
        ):
            with self.subTest(source=source):
                operation_id = self.create_delete_job(
                    source,
                    [(source, "2026-08-01T00:00:00Z", CredentialPartition.USER)],
                )
                self.user_client.outcomes[f"{SPACE}/messages/{source}"] = [
                    failure,
                    failure,
                ]
                executor = self.executor(jitter=lambda _low, _high: 0, max_attempts=2)

                self.run_job(operation_id, executor)

                job = self.store.get_job(operation_id)
                self.assertEqual(job.status, JobStatus.FAILED)  # type: ignore[union-attr]
                self.assertEqual(job.failed_count, 1)  # type: ignore[union-attr]
                self.assertNotIn(
                    b"raw-timeout-sentinel", self.store.database_path.read_bytes()
                )
                self.assertNotIn(
                    b"raw-reset-sentinel", self.store.database_path.read_bytes()
                )

    def test_kill_switch_is_checked_before_claiming_next_item(self) -> None:
        operation_id = self.create_delete_job(
            "kill",
            [
                ("one", "2026-08-01T00:00:00Z", CredentialPartition.USER),
                ("two", "2026-08-02T00:00:00Z", CredentialPartition.USER),
            ],
        )
        original_delete = self.user_client.delete

        def delete_then_disable(**kwargs: Any) -> None:
            original_delete(**kwargs)
            self.enabled = False

        self.user_client.delete = delete_then_disable  # type: ignore[method-assign]
        executor = self.executor()

        self.run_job(operation_id, executor)

        self.assertEqual(len(self.events), 1)
        self.assertEqual(
            [item.status for item in self.store.list_items(operation_id)],
            [ItemStatus.DELETED, ItemStatus.PENDING],
        )
        self.enabled = True
        self.user_client.delete = original_delete  # type: ignore[method-assign]
        self.run_job(operation_id, executor)
        self.assertEqual(len(self.events), 2)

    def test_crash_after_remote_success_recovers_as_404_without_duplicate_terminal(
        self,
    ) -> None:
        operation_id = self.create_delete_job(
            "crash",
            [("one", "2026-08-01T00:00:00Z", CredentialPartition.USER)],
        )
        job = self.store.claim_next_delete_job(
            delete_enabled=True, now=self.clock()
        )
        claimed = self.store.claim_next_item(operation_id, now=self.clock())
        self.assertEqual(claimed.status, ItemStatus.RUNNING)  # type: ignore[union-attr]
        # Simulate a successful remote delete followed by process death.
        self.store.recover_job_claims(operation_id, now=self.clock())
        self.user_client.outcomes[f"{SPACE}/messages/one"] = [
            ChatDeleteApiError(404)
        ]

        self.executor().handle_delete(job)  # type: ignore[arg-type]

        item = self.store.list_items(operation_id)[0]
        self.assertEqual(item.status, ItemStatus.ALREADY_ABSENT)
        terminal_calls = len(self.events)
        self.assertIsNone(
            self.store.claim_next_delete_job(
                delete_enabled=True, now=self.clock()
            )
        )
        self.assertEqual(len(self.events), terminal_calls)

    def test_restart_after_terminal_item_before_job_reconcile_calls_no_api(self) -> None:
        operation_id = self.create_delete_job(
            "reconcile-crash",
            [("one", "2026-08-01T00:00:00Z", CredentialPartition.BOT)],
        )
        job = self.store.claim_next_delete_job(
            delete_enabled=True, now=self.clock()
        )
        item = self.store.claim_next_item(operation_id, now=self.clock())
        self.store.record_item_result(
            operation_id,
            item.message_name,  # type: ignore[union-attr]
            status=ItemStatus.DELETED,
        )

        self.executor().handle_delete(job)  # type: ignore[arg-type]

        self.assertEqual(self.events, [])
        terminal = self.store.get_job(operation_id)
        self.assertEqual(terminal.status, JobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(terminal.deleted_count, 1)  # type: ignore[union-attr]

    def test_callback_pacer_delays_first_delete_by_minimum_interval(self) -> None:
        operation_id = self.create_delete_job(
            "pacing",
            [("one", "2026-08-01T00:00:00Z", CredentialPartition.USER)],
        )
        monotonic = [100.0]
        sleeps: list[float] = []
        pacer = ChatWritePacer(
            self.state_dir,
            clock=lambda: monotonic[0],
            sleeper=sleeps.append,
        )
        pacer.record_external_write(SPACE, at=100.0)

        self.run_job(operation_id, self.executor(pacer=pacer))

        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.1, places=6)

    def test_final_notification_retry_never_reexecutes_terminal_items(self) -> None:
        operation_id = self.create_delete_job(
            "final",
            [("one", "2026-08-01T00:00:00Z", CredentialPartition.USER)],
        )
        executor = self.executor(jitter=lambda _low, _high: 1)
        self.run_job(operation_id, executor)
        delete_calls = len(self.events)
        self.gateway.create_error = TimeoutError("private-final-body")
        final_job = self.store.claim_next_final_notification(now=self.clock())

        executor.handle_final_notification(final_job)  # type: ignore[arg-type]

        pending = self.store.get_job(operation_id)
        self.assertEqual(
            pending.final_notification_state,  # type: ignore[union-attr]
            NotificationStatus.PENDING,
        )
        self.assertEqual(len(self.events), delete_calls)
        self.clock.advance(1)
        self.gateway.create_error = None
        retry = self.store.claim_next_final_notification(now=self.clock())
        executor.handle_final_notification(retry)  # type: ignore[arg-type]

        sent = self.store.get_job(operation_id)
        self.assertEqual(sent.final_notification_state, NotificationStatus.SENT)  # type: ignore[union-attr]
        self.assertEqual(
            sent.final_message_name,  # type: ignore[union-attr]
            f"{SPACE}/messages/{sent.final_client_message_id}",  # type: ignore[union-attr]
        )
        self.assertEqual(len(self.events), delete_calls)
        card = self.gateway.created[-1][1]
        rendered = repr(card)
        self.assertIn("ลบแล้ว", rendered)
        self.assertNotIn("private-final-body", rendered)

    def test_final_create_timeout_binds_recovered_custom_message(self) -> None:
        operation_id = self.create_delete_job(
            "final-recover",
            [("one", "2026-08-01T00:00:00Z", CredentialPartition.BOT)],
        )
        executor = self.executor()
        self.run_job(operation_id, executor)
        terminal = self.store.get_job(operation_id)
        self.gateway.create_error = TimeoutError("ambiguous create")
        canonical = f"{SPACE}/messages/{terminal.final_client_message_id}"  # type: ignore[union-attr]
        self.gateway.recovered[terminal.final_client_message_id] = {  # type: ignore[union-attr,index]
            "name": canonical
        }
        final_job = self.store.claim_next_final_notification(now=self.clock())

        executor.handle_final_notification(final_job)  # type: ignore[arg-type]

        bound = self.store.get_job(operation_id)
        self.assertEqual(bound.final_notification_state, NotificationStatus.SENT)  # type: ignore[union-attr]
        self.assertEqual(bound.final_message_name, canonical)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
