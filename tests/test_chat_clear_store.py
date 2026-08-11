from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from helpers.chat_clear_store import (
    CHAT_HISTORY_SCHEMA_VERSION,
    ActionKind,
    ActiveDeleteJobError,
    ChatClearStore,
    ChatClearStoreConflictError,
    ChatClearStoreSchemaError,
    ChatClearStoreUnavailableError,
    ClearJobRequest,
    CredentialPartition,
    ItemStatus,
    JobStatus,
    NotificationStatus,
    SnapshotFrozenError,
    SnapshotItem,
)

UTC = timezone.utc
USER = "users/allowed-user"
SPACE = "spaces/allowed-dm"


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def _request(source_id: str = "source-one") -> ClearJobRequest:
    return ClearJobRequest(
        source_message_name=f"{SPACE}/messages/{source_id}",
        requester_name=USER,
        space_name=SPACE,
        source_event_time_utc="2026-08-11T00:00:00.000000Z",
        reference_time_utc="2026-08-11T00:00:00.000000Z",
        cutoff_utc="2026-08-10T00:00:00.000000Z",
        display_timezone="Asia/Bangkok",
        normalized_argument="1d",
    )


def _item(
    identifier: str,
    created: str,
    partition: CredentialPartition = CredentialPartition.USER,
) -> SnapshotItem:
    return SnapshotItem(
        message_name=f"{SPACE}/messages/{identifier}",
        create_time_utc=created,
        sender_name=USER if partition is not CredentialPartition.NONE else "",
        sender_type=(
            "HUMAN"
            if partition is CredentialPartition.USER
            else "BOT"
            if partition is CredentialPartition.BOT
            else "TYPE_UNSPECIFIED"
        ),
        credential_partition=partition,
    )


def _action_process(
    state_dir: str,
    action_value: str,
    token_hash: bytes,
    start: Any,
    results: Any,
) -> None:
    store = ChatClearStore(Path(state_dir))
    start.wait(10)
    result = store.apply_action(
        action=ActionKind(action_value),
        token_hash=token_hash,
        requester_name=USER,
        space_name=SPACE,
        confirmation_message_name=f"{SPACE}/messages/confirmation",
        now=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
    )
    results.put((result.status.value if result.status else None, result.transitioned))


class ChatClearStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.state_dir = Path(temporary_directory.name) / "history"
        self.clock = FakeClock(datetime(2026, 8, 11, 0, 0, tzinfo=UTC))
        self.store = ChatClearStore(self.state_dir, clock=self.clock)

    def create_claimed_job(self, source_id: str = "source-one") -> str:
        operation_id = self.store.create_job(_request(source_id)).job.operation_id
        claimed = self.store.claim_preview(now=self.clock.current)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.operation_id, operation_id)
        return operation_id

    def create_snapshot_job(
        self,
        source_id: str = "source-one",
        items: tuple[SnapshotItem, ...] | None = None,
    ) -> str:
        operation_id = self.create_claimed_job(source_id)
        self.store.append_snapshot_items(
            operation_id,
            items
            or (
                _item("older", "2026-08-01T00:00:00Z"),
                _item(
                    "newer",
                    "2026-08-02T00:00:00.123456789Z",
                    CredentialPartition.BOT,
                ),
            ),
        )
        self.store.finalize_snapshot(operation_id)
        return operation_id

    def bind_confirmation(self, operation_id: str) -> tuple[bytes, bytes]:
        confirm_hash = hashlib.sha256(b"confirm-secret").digest()
        cancel_hash = hashlib.sha256(b"cancel-secret").digest()
        self.store.bind_confirmation(
            operation_id,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            confirmation_client_message_id="client-confirmation-one",
            delivery_generation=1,
            confirm_token_hash=confirm_hash,
            cancel_token_hash=cancel_hash,
            expires_at=self.clock.current + timedelta(minutes=10),
        )
        return confirm_hash, cancel_hash

    def queue_delete(self, operation_id: str) -> None:
        confirm_hash, _cancel_hash = self.bind_confirmation(operation_id)
        result = self.store.apply_action(
            action=ActionKind.CONFIRM,
            token_hash=confirm_hash,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            now=self.clock.current + timedelta(minutes=1),
        )
        self.assertTrue(result.transitioned)


class ChatClearSchemaTests(ChatClearStoreTestCase):
    def test_fresh_preflight_sets_schema_pragmas_and_private_modes(self) -> None:
        result = self.store.preflight()

        self.assertEqual(result.schema_version, CHAT_HISTORY_SCHEMA_VERSION)
        self.assertEqual(result.journal_mode, "wal")
        self.assertEqual(result.synchronous, 2)
        self.assertTrue(result.foreign_keys)
        self.assertEqual(result.busy_timeout_milliseconds, 5000)
        self.assertEqual(result.display_timezone, "Asia/Bangkok")
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "history.sqlite3").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "migration.lock").stat().st_mode),
            0o600,
        )
        for suffix in ("-wal", "-shm"):
            path = Path(f"{self.state_dir / 'history.sqlite3'}{suffix}")
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_phase3_pacer_schema_is_migrated_without_losing_reservation(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        database = self.state_dir / "history.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            """
            CREATE TABLE chat_write_pacer(
                space_name TEXT PRIMARY KEY,
                next_write_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO chat_write_pacer VALUES (?, ?, ?)", (SPACE, 1001.1, 1000.0)
        )
        connection.commit()
        connection.close()

        self.store.preflight()
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            row = connection.execute(
                "SELECT next_write_at_utc FROM space_write_pacer WHERE space_name=?",
                (SPACE,),
            ).fetchone()
        finally:
            connection.close()
        self.assertNotIn("chat_write_pacer", tables)
        self.assertIsNotNone(row)
        self.assertIn("1970-01-01T00:16:41.100000Z", row[0])

    def test_newer_incomplete_and_corrupt_databases_fail_closed(self) -> None:
        cases = ("newer", "incomplete", "corrupt")
        for case in cases:
            with self.subTest(case=case):
                temporary_directory = tempfile.TemporaryDirectory()
                self.addCleanup(temporary_directory.cleanup)
                state = Path(temporary_directory.name) / "history"
                state.mkdir()
                database = state / "history.sqlite3"
                if case == "corrupt":
                    database.write_bytes(b"not a sqlite database sentinel-private")
                else:
                    connection = sqlite3.connect(database)
                    if case == "newer":
                        connection.execute("PRAGMA user_version=99")
                    else:
                        for table in (
                            "clear_jobs",
                            "clear_items",
                            "space_write_pacer",
                            "worker_state",
                        ):
                            connection.execute(f"CREATE TABLE {table}(dummy TEXT)")
                        connection.execute("PRAGMA user_version=1")
                    connection.commit()
                    connection.close()
                with self.assertRaises(ChatClearStoreSchemaError):
                    ChatClearStore(state).preflight()

    def test_symlink_state_or_database_is_rejected(self) -> None:
        target = self.state_dir.parent / "target"
        target.mkdir()
        self.state_dir.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ChatClearStoreUnavailableError):
            self.store.preflight()

        self.state_dir.unlink()
        self.state_dir.mkdir()
        database_target = target / "database"
        database_target.write_bytes(b"")
        (self.state_dir / "history.sqlite3").symlink_to(database_target)
        with self.assertRaises(ChatClearStoreUnavailableError):
            ChatClearStore(self.state_dir).preflight()

    def test_fifo_database_is_rejected_without_blocking(self) -> None:
        self.state_dir.mkdir()
        os.mkfifo(self.state_dir / "history.sqlite3")
        started = time.monotonic()
        with self.assertRaises(ChatClearStoreUnavailableError):
            ChatClearStore(self.state_dir).preflight()
        self.assertLess(time.monotonic() - started, 1)

    def test_database_busy_is_sanitized_and_does_not_partially_insert(self) -> None:
        self.store.preflight()
        blocker = sqlite3.connect(self.store.database_path, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with (
                patch(
                    "helpers.chat_clear_store.CHAT_HISTORY_BUSY_TIMEOUT_MILLISECONDS",
                    20,
                ),
                self.assertRaises(ChatClearStoreUnavailableError),
            ):
                ChatClearStore(self.state_dir, clock=self.clock).create_job(_request())
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        connection = sqlite3.connect(self.store.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM clear_jobs").fetchone()[0], 0
            )
        finally:
            connection.close()

    def test_delete_preflight_rejects_ephemeral_tmp_state(self) -> None:
        with self.assertRaises(ChatClearStoreUnavailableError):
            self.store.preflight(delete_execution=True)


class ChatClearJobTests(ChatClearStoreTestCase):
    def test_source_retry_deduplicates_and_new_job_supersedes_predelete(self) -> None:
        first = self.store.create_job(_request("same-source"))
        duplicate = self.store.create_job(_request("same-source"))
        replacement = self.store.create_job(_request("new-source"))

        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(first.job.operation_id, duplicate.job.operation_id)
        self.assertEqual(
            replacement.superseded_operation_ids,
            (first.job.operation_id,),
        )
        self.assertEqual(
            self.store.get_job(first.job.operation_id).status,
            JobStatus.CANCELLED,
        )
        self.assertEqual(
            self.store.get_job(first.job.operation_id).safe_error_category,
            "superseded",
        )

    def test_active_delete_job_blocks_new_clear(self) -> None:
        operation_id = self.create_snapshot_job()
        self.queue_delete(operation_id)
        with self.assertRaises(ActiveDeleteJobError):
            self.store.create_job(_request("blocked-source"))

    def test_zero_deletable_snapshot_completes_without_confirmation(self) -> None:
        operation_id = self.create_claimed_job()
        self.store.append_snapshot_items(
            operation_id,
            (
                _item(
                    "unknown",
                    "2026-08-01T00:00:00Z",
                    CredentialPartition.NONE,
                ),
            ),
        )
        job = self.store.finalize_snapshot(operation_id)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertTrue(job.snapshot_complete)
        self.assertEqual(job.candidate_skipped, 1)
        self.assertIsNone(job.confirmation_message_name)


class ChatClearSnapshotTests(ChatClearStoreTestCase):
    def test_snapshot_pages_finalize_counts_and_freeze_identity(self) -> None:
        operation_id = self.create_claimed_job()
        self.assertEqual(
            self.store.append_snapshot_items(
                operation_id,
                (_item("third", "2026-08-03T00:00:00Z"),),
            ),
            1,
        )
        self.store.append_snapshot_items(
            operation_id,
            (
                _item("first", "2026-08-01T00:00:00Z"),
                _item(
                    "second",
                    "2026-08-02T00:00:00Z",
                    CredentialPartition.BOT,
                ),
                _item(
                    "skip",
                    "2026-08-02T12:00:00Z",
                    CredentialPartition.NONE,
                ),
            ),
        )
        job = self.store.finalize_snapshot(operation_id)

        self.assertEqual(
            (job.candidate_human, job.candidate_bot, job.candidate_skipped),
            (2, 1, 1),
        )
        items = self.store.list_items(operation_id)
        self.assertEqual(
            [item.message_name.rsplit("/", 1)[-1] for item in items],
            ["first", "second", "skip", "third"],
        )
        with self.assertRaises(SnapshotFrozenError):
            self.store.append_snapshot_items(
                operation_id, (_item("late", "2026-08-04T00:00:00Z"),)
            )

        connection = sqlite3.connect(self.store.database_path)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE clear_items SET sender_name='users/changed'
                WHERE operation_id=?
                """,
                (operation_id,),
            )
        connection.close()

    def test_incomplete_snapshot_is_discarded_and_requeued_on_recovery(self) -> None:
        operation_id = self.create_claimed_job()
        self.store.append_snapshot_items(
            operation_id, (_item("partial", "2026-08-01T00:00:00Z"),)
        )

        recovered = ChatClearStore(
            self.state_dir, clock=self.clock
        ).recover_interrupted(now=self.clock.current)

        self.assertEqual(recovered.rebuilt_snapshots, 1)
        self.assertEqual(self.store.list_items(operation_id), ())
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.PREVIEW_QUEUED)
        self.assertFalse(job.snapshot_complete)

    def test_snapshot_limit_aborts_page_transaction_without_truncation(self) -> None:
        limited = ChatClearStore(
            self.state_dir,
            clock=self.clock,
            max_snapshot_items=1,
        )
        operation_id = limited.create_job(_request()).job.operation_id
        limited.claim_preview(now=self.clock.current)
        with self.assertRaises(ChatClearStoreConflictError):
            limited.append_snapshot_items(
                operation_id,
                (
                    _item("one", "2026-08-01T00:00:00Z"),
                    _item("two", "2026-08-02T00:00:00Z"),
                ),
            )
        self.assertEqual(limited.list_items(operation_id), ())


class ChatClearActionTests(ChatClearStoreTestCase):
    def test_action_checks_every_binding_and_is_idempotent_after_winner(self) -> None:
        operation_id = self.create_snapshot_job()
        confirm_hash, cancel_hash = self.bind_confirmation(operation_id)

        forged = self.store.apply_action(
            action=ActionKind.CONFIRM,
            token_hash=hashlib.sha256(b"forged").digest(),
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            now=self.clock.current + timedelta(minutes=1),
        )
        wrong_user = self.store.apply_action(
            action=ActionKind.CONFIRM,
            token_hash=confirm_hash,
            requester_name="users/someone-else",
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            now=self.clock.current + timedelta(minutes=1),
        )
        winner = self.store.apply_action(
            action=ActionKind.CANCEL,
            token_hash=cancel_hash,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            now=self.clock.current + timedelta(minutes=1),
        )
        retry = self.store.apply_action(
            action=ActionKind.CANCEL,
            token_hash=cancel_hash,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            now=self.clock.current + timedelta(minutes=2),
        )

        self.assertFalse(forged.authorized)
        self.assertFalse(wrong_user.authorized)
        self.assertEqual(winner.status, JobStatus.CANCELLED)
        self.assertTrue(winner.transitioned)
        self.assertEqual(retry.status, JobStatus.CANCELLED)
        self.assertFalse(retry.transitioned)
        self.assertEqual(
            self.store.get_job(operation_id).status,
            JobStatus.CANCELLED,
        )

    def test_expired_action_cannot_queue_delete(self) -> None:
        operation_id = self.create_snapshot_job()
        confirm_hash, _ = self.bind_confirmation(operation_id)
        result = self.store.apply_action(
            action=ActionKind.CONFIRM,
            token_hash=confirm_hash,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            now=self.clock.current + timedelta(minutes=10),
        )
        self.assertEqual(result.status, JobStatus.EXPIRED)
        self.assertEqual(self.store.get_job(operation_id).status, JobStatus.EXPIRED)

    def test_multiprocess_confirm_cancel_race_has_one_transition(self) -> None:
        operation_id = self.create_snapshot_job()
        confirm_hash, cancel_hash = self.bind_confirmation(operation_id)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_action_process,
                args=(
                    str(self.state_dir),
                    ActionKind.CONFIRM.value,
                    confirm_hash,
                    start,
                    results,
                ),
            ),
            context.Process(
                target=_action_process,
                args=(
                    str(self.state_dir),
                    ActionKind.CANCEL.value,
                    cancel_hash,
                    start,
                    results,
                ),
            ),
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(sum(transitioned for _status, transitioned in outcomes), 1)
        final = self.store.get_job(operation_id).status.value
        self.assertIn(final, {JobStatus.DELETE_QUEUED.value, JobStatus.CANCELLED.value})
        self.assertEqual({status for status, _ in outcomes}, {final})


class ChatClearItemTests(ChatClearStoreTestCase):
    def prepare_running_job(self) -> str:
        operation_id = self.create_snapshot_job()
        self.queue_delete(operation_id)
        self.assertIsNone(
            self.store.claim_delete_job(operation_id, delete_enabled=False)
        )
        running = self.store.claim_delete_job(operation_id, delete_enabled=True)
        self.assertIsNotNone(running)
        return operation_id

    def test_items_claim_oldest_retry_and_finalize_deterministically(self) -> None:
        operation_id = self.prepare_running_job()
        first = self.store.claim_next_item(
            operation_id, now=self.clock.current + timedelta(minutes=2)
        )
        self.assertTrue(first.message_name.endswith("/older"))
        self.assertEqual(first.attempts, 1)
        self.assertTrue(
            self.store.record_item_retry(
                operation_id,
                first.message_name,
                next_attempt_at=self.clock.current + timedelta(minutes=5),
                safe_error_category="transient_timeout",
            )
        )
        second = self.store.claim_next_item(
            operation_id, now=self.clock.current + timedelta(minutes=2)
        )
        self.assertTrue(second.message_name.endswith("/newer"))
        self.store.record_item_result(
            operation_id, second.message_name, status=ItemStatus.DELETED
        )
        retried = self.store.claim_next_item(
            operation_id, now=self.clock.current + timedelta(minutes=6)
        )
        self.assertEqual(retried.message_name, first.message_name)
        self.assertEqual(retried.attempts, 2)
        self.store.record_item_result(
            operation_id,
            retried.message_name,
            status=ItemStatus.ALREADY_ABSENT,
        )
        final = self.store.reconcile_job(operation_id, finalize=True)
        self.assertEqual(final.status, JobStatus.COMPLETED)
        self.assertEqual((final.deleted_count, final.already_absent_count), (1, 1))

    def test_partition_failure_only_fails_that_partition(self) -> None:
        operation_id = self.prepare_running_job()
        failed = self.store.fail_partition(
            operation_id,
            CredentialPartition.USER,
            "user_auth_failure",
        )
        self.assertEqual(failed, 1)
        remaining = self.store.claim_next_item(operation_id, now=self.clock.current)
        self.assertEqual(remaining.credential_partition, CredentialPartition.BOT)
        self.store.record_item_result(
            operation_id, remaining.message_name, status=ItemStatus.DELETED
        )
        final = self.store.reconcile_job(operation_id, finalize=True)
        self.assertEqual(final.status, JobStatus.PARTIAL_FAILED)

    def test_running_claim_and_notification_are_recovered_after_restart(self) -> None:
        operation_id = self.prepare_running_job()
        claimed = self.store.claim_next_item(operation_id, now=self.clock.current)
        self.assertIsNotNone(claimed)

        recovered = ChatClearStore(
            self.state_dir, clock=self.clock
        ).recover_interrupted(now=self.clock.current + timedelta(minutes=1))
        self.assertEqual(recovered.recovered_items, 1)
        items = self.store.list_items(operation_id)
        reset = next(
            item for item in items if item.message_name == claimed.message_name
        )
        self.assertEqual(reset.status, ItemStatus.PENDING)

    def test_stale_running_job_fails_remaining_items_safely(self) -> None:
        operation_id = self.prepare_running_job()
        result = ChatClearStore(self.state_dir, clock=self.clock).recover_interrupted(
            now=self.clock.current + timedelta(hours=25)
        )
        self.assertEqual(result.stale_jobs, 1)
        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertTrue(
            all(
                item.status is ItemStatus.FAILED
                for item in self.store.list_items(operation_id)
            )
        )

    def test_restart_reconciles_terminal_items_when_job_cache_was_not_committed(
        self,
    ) -> None:
        operation_id = self.prepare_running_job()
        while True:
            item = self.store.claim_next_item(operation_id, now=self.clock.current)
            if item is None:
                break
            self.store.record_item_result(
                operation_id,
                item.message_name,
                status=ItemStatus.DELETED,
            )

        ChatClearStore(self.state_dir, clock=self.clock).recover_interrupted(
            now=self.clock.current + timedelta(minutes=1)
        )

        job = self.store.get_job(operation_id)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.deleted_count, 2)


class ChatClearMaintenanceTests(ChatClearStoreTestCase):
    def test_terminal_retention_prunes_only_old_terminal_jobs(self) -> None:
        old_terminal = self.create_claimed_job("old-terminal")
        self.store.finalize_snapshot(old_terminal)
        active = self.store.create_job(_request("active")).job.operation_id

        result = self.store.maintain(
            now=self.clock.current + timedelta(days=31),
            retention_days=30,
        )

        self.assertEqual(result.pruned_jobs, 1)
        self.assertIsNone(self.store.get_job(old_terminal))
        self.assertIsNotNone(self.store.get_job(active))
        with self.assertRaises(ChatClearStoreConflictError):
            self.store.checkpoint("TRUNCATE")

    def test_diagnostics_have_counts_age_and_sanitized_worker_identity(self) -> None:
        self.store.create_job(_request())
        self.store.record_worker_heartbeat("worker-test", now=self.clock.current)
        diagnostics = self.store.diagnostics(
            now=self.clock.current + timedelta(seconds=10)
        )
        self.assertEqual(diagnostics.job_counts[JobStatus.PREVIEW_QUEUED.value], 1)
        self.assertEqual(diagnostics.oldest_active_age_seconds, 10)
        self.assertEqual(diagnostics.worker_owner, "worker-test")

    def test_database_contains_hashes_but_no_raw_handle_or_message_content(
        self,
    ) -> None:
        operation_id = self.create_snapshot_job()
        raw_confirm = b"sentinel-raw-confirm-handle"
        raw_cancel = b"sentinel-raw-cancel-handle"
        self.store.bind_confirmation(
            operation_id,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation",
            confirmation_client_message_id="client-confirmation-one",
            delivery_generation=1,
            confirm_token_hash=hashlib.sha256(raw_confirm).digest(),
            cancel_token_hash=hashlib.sha256(raw_cancel).digest(),
            expires_at=self.clock.current + timedelta(minutes=10),
        )

        connection = sqlite3.connect(self.store.database_path)
        dump = "\n".join(connection.iterdump()).encode("utf-8")
        connection.close()
        disk = b"".join(
            path.read_bytes()
            for path in (
                self.store.database_path,
                Path(f"{self.store.database_path}-wal"),
            )
            if path.exists()
        )
        for sentinel in (raw_confirm, raw_cancel, b"sentinel-message-content"):
            self.assertNotIn(sentinel, dump)
            self.assertNotIn(sentinel, disk)

    def test_final_notification_recovery_does_not_change_job_outcome(self) -> None:
        operation_id = self.create_claimed_job("notification")
        completed = self.store.finalize_snapshot(operation_id)
        self.assertEqual(completed.status, JobStatus.COMPLETED)
        sending = self.store.claim_final_notification(
            operation_id, now=self.clock.current
        )
        self.assertEqual(
            sending.final_notification_state,
            NotificationStatus.SENDING,
        )

        result = ChatClearStore(self.state_dir, clock=self.clock).recover_interrupted(
            now=self.clock.current + timedelta(minutes=1)
        )

        self.assertEqual(result.recovered_notifications, 1)
        recovered = self.store.get_job(operation_id)
        self.assertEqual(recovered.status, JobStatus.COMPLETED)
        self.assertEqual(
            recovered.final_notification_state,
            NotificationStatus.PENDING,
        )
        self.assertEqual(recovered.final_notification_attempts, 1)


if __name__ == "__main__":
    unittest.main()
