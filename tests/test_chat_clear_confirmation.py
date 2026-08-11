from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from helpers.chat_clear_confirmation import (
    ChatClearCoordinator,
    ChatClearPresenter,
)
from helpers.chat_clear_store import (
    ChatClearStore,
    ChatClearStoreConflictError,
    ClearJobRequest,
    JobStatus,
)
from helpers.chat_gateway import ChatMessageNotFoundError

UTC = timezone.utc
USER = "users/allowed-user"
SPACE = "spaces/allowed-dm"
NOW = datetime(2026, 8, 11, tzinfo=UTC)


class FakeClient:
    allowed_space = SPACE

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[dict[str, Any]] = []

    def iter_clear_candidates(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        yield from self.messages


class FakeGateway:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, dict[str, Any], str]] = []
        self.recovered: dict[str, dict[str, Any]] = {}
        self.get_calls: list[str] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def create_structured_card(
        self,
        space: str,
        thread: str,
        message: dict[str, Any],
        *,
        message_id: str,
    ) -> dict[str, str]:
        self.created.append((space, thread, message, message_id))
        return {"name": f"{space}/messages/{message_id}"}

    def get_message_by_client_id(
        self, space: str, *, message_id: str
    ) -> dict[str, Any]:
        self.get_calls.append(message_id)
        try:
            return self.recovered[message_id]
        except KeyError:
            raise ChatMessageNotFoundError("not found") from None

    def send_structured_card(
        self,
        space: str,
        thread: str,
        message: dict[str, Any],
        *,
        message_id: str,
    ) -> dict[str, str]:
        return self.create_structured_card(
            space, thread, message, message_id=message_id
        )

    def update_structured_card(
        self, message_name: str, message: dict[str, Any]
    ) -> dict[str, str]:
        self.updated.append((message_name, message))
        return {"name": message_name}


def request(source: str = "source") -> ClearJobRequest:
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


def message(identifier: str, sender_type: str, sender_name: str) -> dict[str, Any]:
    return {
        "name": f"{SPACE}/messages/{identifier}",
        "createTime": "2026-08-09T23:59:59.000000Z",
        "sender": {"type": sender_type, "name": sender_name},
        # Content fields must never reach durable snapshot rows.
        "text": f"private-{identifier}",
        "cardsV2": [{"private": identifier}],
    }


def handles(card: dict[str, Any]) -> tuple[str, str]:
    widgets = card["cardsV2"][0]["card"]["sections"][0]["widgets"]
    buttons = widgets[-1]["buttonList"]["buttons"]
    return tuple(
        button["onClick"]["action"]["parameters"][0]["value"]
        for button in buttons
    )  # type: ignore[return-value]


class ChatClearConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name) / "history"
        self.store = ChatClearStore(self.state_dir, clock=lambda: NOW)

    def coordinator(
        self, client: FakeClient, gateway: FakeGateway
    ) -> ChatClearCoordinator:
        return ChatClearCoordinator(
            self.store,
            client,  # type: ignore[arg-type]
            gateway,  # type: ignore[arg-type]
            action_url="https://bot.example.test/chat",
            ttl_seconds=600,
            clock=lambda: NOW,
            sleeper=lambda _delay: None,
        )

    def test_exact_snapshot_classification_and_secure_confirmation(self) -> None:
        client = FakeClient(
            [
                message("human", "HUMAN", USER),
                message("other-human", "HUMAN", "users/other"),
                message("bot", "BOT", "users/bot"),
                message("unknown", "TYPE_UNSPECIFIED", ""),
            ]
        )
        gateway = FakeGateway()
        created = self.store.create_job(request()).job
        claimed = self.store.claim_preview(now=NOW)
        self.assertIsNotNone(claimed)

        self.coordinator(client, gateway).handle_preview(claimed)  # type: ignore[arg-type]

        job = self.store.get_job(created.operation_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, JobStatus.PENDING_CONFIRMATION)  # type: ignore[union-attr]
        self.assertEqual(
            (job.candidate_human, job.candidate_bot, job.candidate_skipped),  # type: ignore[union-attr]
            (1, 1, 2),
        )
        self.assertEqual(
            client.calls,
            [
                {
                    "actor_name": USER,
                    "space_name": SPACE,
                    "cutoff_utc": "2026-08-10T00:00:00.000000Z",
                }
            ],
        )
        card = gateway.created[0][2]
        confirm, cancel = handles(card)
        self.assertNotEqual(confirm, cancel)
        self.assertGreaterEqual(len(confirm), 43)
        for button in card["cardsV2"][0]["card"]["sections"][0]["widgets"][-1][
            "buttonList"
        ]["buttons"]:
            action = button["onClick"]["action"]
            self.assertEqual(action["function"], "https://bot.example.test/chat")
            self.assertEqual(
                set(action["parameters"][0]), {"key", "value"}
            )
            self.assertEqual(
                action["parameters"][0]["key"], "historyActionHandle"
            )

        database_bytes = self.store.database_path.read_bytes()
        self.assertNotIn(confirm.encode(), database_bytes)
        self.assertNotIn(cancel.encode(), database_bytes)
        serialized_items = repr(self.store.list_items(created.operation_id))
        self.assertNotIn("private-human", serialized_items)
        result = self.store.apply_handle(
            token_hash=hashlib.sha256(cancel.encode()).digest(),
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=job.confirmation_message_name,  # type: ignore[union-attr,arg-type]
            now=NOW,
        )
        self.assertTrue(result.authorized)
        self.assertEqual(result.status, JobStatus.CANCELLED)

    def test_malformed_page_aborts_and_discards_partial_snapshot(self) -> None:
        client = FakeClient(
            [
                message("valid", "HUMAN", USER),
                {"name": f"{SPACE}/messages/bad", "createTime": "not-time"},
            ]
        )
        gateway = FakeGateway()
        created = self.store.create_job(request("malformed")).job
        claimed = self.store.claim_preview(now=NOW)

        self.coordinator(client, gateway).handle_preview(claimed)  # type: ignore[arg-type]

        job = self.store.get_job(created.operation_id)
        self.assertEqual(job.status, JobStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(self.store.list_items(created.operation_id), ())
        self.assertEqual(gateway.created, [])

    def test_zero_deletable_is_terminal_without_handles_or_buttons(self) -> None:
        client = FakeClient([message("other", "HUMAN", "users/other")])
        gateway = FakeGateway()
        created = self.store.create_job(request("zero")).job
        claimed = self.store.claim_preview(now=NOW)

        self.coordinator(client, gateway).handle_preview(claimed)  # type: ignore[arg-type]

        job = self.store.get_job(created.operation_id)
        self.assertEqual(job.status, JobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertIsNone(job.confirmation_client_message_id)  # type: ignore[union-attr]
        card = gateway.created[0][2]
        self.assertNotIn("buttonList", repr(card))
        self.assertNotIn("historyActionHandle", repr(card))

    def test_restart_reconciles_persisted_generation_without_recreate(self) -> None:
        gateway = FakeGateway()
        created = self.store.create_job(request("recover")).job
        claimed = self.store.claim_preview(now=NOW)
        client = FakeClient([message("bot", "BOT", "users/bot")])
        coordinator = self.coordinator(client, gateway)
        coordinator._build_snapshot(claimed)  # type: ignore[arg-type]
        confirm = "c" * 43
        cancel = "x" * 43
        client_id = "client-jinx-hc-0123456789abcdef0123456789abcdef-01"
        prepared = self.store.prepare_confirmation_delivery(
            created.operation_id,
            requester_name=USER,
            space_name=SPACE,
            confirmation_client_message_id=client_id,
            delivery_generation=1,
            confirm_token_hash=hashlib.sha256(confirm.encode()).digest(),
            cancel_token_hash=hashlib.sha256(cancel.encode()).digest(),
        )
        canonical = f"{SPACE}/messages/{client_id}"
        gateway.recovered[client_id] = {"name": canonical}

        coordinator.handle_preview(prepared)

        bound = self.store.get_job(created.operation_id)
        self.assertEqual(bound.status, JobStatus.PENDING_CONFIRMATION)  # type: ignore[union-attr]
        self.assertEqual(bound.confirmation_message_name, canonical)  # type: ignore[union-attr]
        self.assertEqual(gateway.created, [])
        self.assertEqual(gateway.get_calls, [client_id])
        with self.assertRaises(ChatClearStoreConflictError):
            self.store.bind_prepared_confirmation(
                created.operation_id,
                requester_name=USER,
                space_name=SPACE,
                confirmation_message_name=canonical,
                confirmation_client_message_id=client_id,
                delivery_generation=1,
                expires_at=NOW + timedelta(minutes=10),
            )

    def test_absent_recovery_abandons_generation_before_new_body_and_id(self) -> None:
        client = FakeClient([message("bot", "BOT", "users/bot")])
        gateway = FakeGateway()
        created = self.store.create_job(request("regenerate")).job
        coordinator = self.coordinator(client, gateway)
        claimed = self.store.claim_preview(now=NOW)
        coordinator._build_snapshot(claimed)  # type: ignore[arg-type]
        old_id = "client-jinx-hc-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-01"
        old_confirm_hash = hashlib.sha256(b"old-confirm").digest()
        old_cancel_hash = hashlib.sha256(b"old-cancel").digest()
        prepared = self.store.prepare_confirmation_delivery(
            created.operation_id,
            requester_name=USER,
            space_name=SPACE,
            confirmation_client_message_id=old_id,
            delivery_generation=1,
            confirm_token_hash=old_confirm_hash,
            cancel_token_hash=old_cancel_hash,
        )

        coordinator.handle_preview(prepared)

        bound = self.store.get_job(created.operation_id)
        self.assertEqual(bound.status, JobStatus.PENDING_CONFIRMATION)  # type: ignore[union-attr]
        self.assertEqual(bound.confirmation_delivery_generation, 2)  # type: ignore[union-attr]
        self.assertNotEqual(bound.confirmation_client_message_id, old_id)  # type: ignore[union-attr]
        self.assertEqual(gateway.get_calls, [old_id] * 4)
        self.assertEqual(len(gateway.created), 1)
        self.assertNotEqual(gateway.created[0][3], old_id)
        with self.assertRaises(ChatClearStoreConflictError):
            self.store.bind_confirmation(
                created.operation_id,
                requester_name=USER,
                space_name=SPACE,
                confirmation_message_name=f"{SPACE}/messages/{old_id}",
                confirmation_client_message_id=old_id,
                delivery_generation=1,
                confirm_token_hash=old_confirm_hash,
                cancel_token_hash=old_cancel_hash,
                expires_at=NOW + timedelta(minutes=10),
            )

    def test_callback_presenter_removes_buttons(self) -> None:
        presenter = ChatClearPresenter()
        for status in (
            JobStatus.DELETE_QUEUED,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
        ):
            with self.subTest(status=status):
                envelope = presenter.addon_update(
                    presenter.build_status_message(status)
                )
                self.assertIn("updateMessageAction", repr(envelope))
                self.assertNotIn("buttonList", repr(envelope))

    def test_forged_wrong_binding_and_expired_handles_fail_closed(self) -> None:
        client = FakeClient([message("bot", "BOT", "users/bot")])
        gateway = FakeGateway()
        created = self.store.create_job(request("bindings")).job
        coordinator = self.coordinator(client, gateway)
        coordinator.handle_preview(self.store.claim_preview(now=NOW))  # type: ignore[arg-type]
        pending = self.store.get_job(created.operation_id)
        confirm = handles(gateway.created[0][2])[0]
        valid_hash = hashlib.sha256(confirm.encode()).digest()

        attempts = (
            {
                "token_hash": hashlib.sha256(b"forged").digest(),
                "requester_name": USER,
                "space_name": SPACE,
                "confirmation_message_name": pending.confirmation_message_name,
            },
            {
                "token_hash": valid_hash,
                "requester_name": "users/same-display-name-intruder",
                "space_name": SPACE,
                "confirmation_message_name": pending.confirmation_message_name,
            },
            {
                "token_hash": valid_hash,
                "requester_name": USER,
                "space_name": "spaces/other",
                "confirmation_message_name": "spaces/other/messages/wrong-card",
            },
            {
                "token_hash": valid_hash,
                "requester_name": USER,
                "space_name": SPACE,
                "confirmation_message_name": f"{SPACE}/messages/wrong-card",
            },
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                result = self.store.apply_handle(**attempt, now=NOW)  # type: ignore[arg-type]
                self.assertFalse(result.authorized)
                self.assertEqual(
                    self.store.get_job(created.operation_id).status,  # type: ignore[union-attr]
                    JobStatus.PENDING_CONFIRMATION,
                )

        expired = self.store.apply_handle(
            token_hash=valid_hash,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=pending.confirmation_message_name,  # type: ignore[union-attr,arg-type]
            now=NOW + timedelta(seconds=601),
        )
        self.assertTrue(expired.authorized)
        self.assertEqual(expired.status, JobStatus.EXPIRED)
        self.assertFalse(
            self.store.apply_handle(
                token_hash=valid_hash,
                requester_name=USER,
                space_name=SPACE,
                confirmation_message_name=f"{SPACE}/messages/unbound",
                now=NOW,
            ).authorized
        )

    def test_supersede_between_prepare_and_post_creates_no_orphan_card(self) -> None:
        client = FakeClient([message("bot", "BOT", "users/bot")])
        gateway = FakeGateway()
        created = self.store.create_job(request("old-source")).job
        coordinator = self.coordinator(client, gateway)
        claimed = self.store.claim_preview(now=NOW)
        frozen = coordinator._build_snapshot(claimed)  # type: ignore[arg-type]
        original_get_job = self.store.get_job
        calls = 0

        def supersede_before_post(operation_id: str) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.store.create_job(request("new-source"))
            return original_get_job(operation_id)

        with patch.object(
            self.store, "get_job", side_effect=supersede_before_post
        ):
            coordinator.handle_preview(frozen)  # type: ignore[arg-type]

        self.assertEqual(gateway.created, [])
        self.assertEqual(
            original_get_job(created.operation_id).status,  # type: ignore[union-attr]
            JobStatus.CANCELLED,
        )

    def test_cancelled_card_cleanup_has_separate_durable_receipt(self) -> None:
        client = FakeClient([message("bot", "BOT", "users/bot")])
        gateway = FakeGateway()
        created = self.store.create_job(request("cleanup")).job
        coordinator = self.coordinator(client, gateway)
        coordinator.handle_preview(self.store.claim_preview(now=NOW))  # type: ignore[arg-type]
        pending = self.store.get_job(created.operation_id)
        cancel = handles(gateway.created[0][2])[1]
        result = self.store.apply_handle(
            token_hash=hashlib.sha256(cancel.encode()).digest(),
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=pending.confirmation_message_name,  # type: ignore[union-attr,arg-type]
            now=NOW,
        )
        self.assertEqual(result.status, JobStatus.CANCELLED)

        cleanup = self.store.claim_confirmation_cleanup(now=NOW)
        self.assertIsNotNone(cleanup)
        coordinator.handle_confirmation_cleanup(cleanup)  # type: ignore[arg-type]

        terminal = self.store.get_job(created.operation_id)
        self.assertEqual(terminal.final_notification_state.value, "SENT")  # type: ignore[union-attr]
        self.assertEqual(len(gateway.updated), 1)
        self.assertNotIn("buttonList", repr(gateway.updated[0][1]))


if __name__ == "__main__":
    unittest.main()
