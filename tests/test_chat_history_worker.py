from __future__ import annotations

import hashlib
import stat
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers.chat_clear_store import (
    ActionKind,
    ChatClearStore,
    ClearJobRequest,
    CredentialPartition,
    JobStatus,
    SnapshotItem,
)
from helpers.chat_history_worker import ChatHistoryWorkerSupervisor

UTC = timezone.utc
USER = "users/allowed-user"
SPACE = "spaces/allowed-dm"


class WorkerClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.current

    def set(self, value: datetime) -> None:
        with self._lock:
            self.current = value


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


class ChatHistoryWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.state_dir = Path(temporary_directory.name) / "history"
        self.clock = WorkerClock(datetime(2026, 8, 11, 0, 0, tzinfo=UTC))
        self.store = ChatClearStore(self.state_dir, clock=self.clock)
        self.supervisors: list[ChatHistoryWorkerSupervisor] = []

    def tearDown(self) -> None:
        for supervisor in self.supervisors:
            try:
                supervisor.stop(timeout=5)
            except TimeoutError:
                pass

    def supervisor(self, **kwargs: object) -> ChatHistoryWorkerSupervisor:
        worker = ChatHistoryWorkerSupervisor(
            self.store,
            poll_interval_seconds=0.02,
            lock_retry_seconds=0.02,
            maintenance_interval_seconds=3600,
            clock=self.clock,
            **kwargs,
        )
        self.supervisors.append(worker)
        return worker

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def create_pending_confirmation(self, source: str = "source") -> str:
        operation_id = self.store.create_job(request(source)).job.operation_id
        self.store.claim_preview(now=self.clock())
        self.store.append_snapshot_items(
            operation_id,
            (
                SnapshotItem(
                    message_name=f"{SPACE}/messages/candidate-{source}",
                    create_time_utc="2026-08-01T00:00:00Z",
                    sender_name=USER,
                    sender_type="HUMAN",
                    credential_partition=CredentialPartition.USER,
                ),
            ),
        )
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
        return operation_id

    def test_startup_recovery_then_poll_claims_preview_outside_transaction(
        self,
    ) -> None:
        operation_id = self.store.create_job(request("partial")).job.operation_id
        self.store.claim_preview(now=self.clock())
        self.store.append_snapshot_items(
            operation_id,
            (
                SnapshotItem(
                    message_name=f"{SPACE}/messages/partial-item",
                    create_time_utc="2026-08-01T00:00:00Z",
                    sender_name=USER,
                    sender_type="HUMAN",
                    credential_partition=CredentialPartition.USER,
                ),
            ),
        )
        handled = threading.Event()
        observed_items: list[int] = []

        def handler(job: object) -> None:
            # A second write transaction succeeds here, proving claim commit
            # happened before orchestration entered the injected handler.
            self.store.record_worker_heartbeat("handler-transaction", now=self.clock())
            observed_items.append(len(self.store.list_items(job.operation_id)))  # type: ignore[attr-defined]
            handled.set()

        worker = self.supervisor(preview_handler=handler)
        worker.start()
        worker.start()

        self.assertTrue(worker.wait_until_active(2))
        self.assertTrue(handled.wait(2))
        self.assertEqual(observed_items, [0])
        self.assertEqual(self.store.get_job(operation_id).status, JobStatus.PREPARING)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "worker.lock").stat().st_mode),
            0o600,
        )

    def test_periodic_poll_expires_without_webhook_wakeup(self) -> None:
        operation_id = self.create_pending_confirmation("expiry")
        worker = self.supervisor()
        worker.start()
        self.assertTrue(worker.wait_until_active(2))

        self.clock.set(self.clock() + timedelta(minutes=11))

        self.assertTrue(
            self.wait_for(
                lambda: self.store.get_job(operation_id).status is JobStatus.EXPIRED
            )
        )

    def test_wakeup_is_low_latency_but_periodic_poll_remains_source_of_truth(
        self,
    ) -> None:
        handled = threading.Event()
        worker = ChatHistoryWorkerSupervisor(
            self.store,
            preview_handler=lambda _job: handled.set(),
            poll_interval_seconds=5,
            lock_retry_seconds=0.02,
            maintenance_interval_seconds=3600,
            clock=self.clock,
        )
        self.supervisors.append(worker)
        worker.start()
        self.assertTrue(worker.wait_until_active(2))
        self.store.create_job(request("after-start"))

        worker.wake()

        self.assertTrue(handled.wait(1))

    def test_singleton_owner_and_standby_takeover(self) -> None:
        first = self.supervisor(owner_id="worker-first")
        second = self.supervisor(owner_id="worker-second")
        first.start()
        self.assertTrue(first.wait_until_active(2))
        second.start()
        time.sleep(0.1)
        self.assertFalse(second.is_active)
        self.assertEqual(self.store.diagnostics().worker_owner, "worker-first")

        first.stop(timeout=5)

        self.assertTrue(second.wait_until_active(2))
        self.assertEqual(self.store.diagnostics().worker_owner, "worker-second")

    def test_delete_queued_job_is_never_claimed_by_phase4_skeleton(self) -> None:
        operation_id = self.create_pending_confirmation("delete-disabled")
        confirm_hash = hashlib.sha256(b"confirm-delete-disabled").digest()
        result = self.store.apply_action(
            action=ActionKind.CONFIRM,
            token_hash=confirm_hash,
            requester_name=USER,
            space_name=SPACE,
            confirmation_message_name=f"{SPACE}/messages/confirmation-delete-disabled",
            now=self.clock() + timedelta(minutes=1),
        )
        self.assertEqual(result.status, JobStatus.DELETE_QUEUED)
        worker = self.supervisor()
        worker.start()
        self.assertTrue(worker.wait_until_active(2))
        time.sleep(0.1)

        self.assertEqual(
            self.store.get_job(operation_id).status, JobStatus.DELETE_QUEUED
        )

    def test_corrupt_store_never_reaches_preview_handler(self) -> None:
        self.state_dir.mkdir()
        (self.state_dir / "history.sqlite3").write_bytes(b"corrupt sentinel")
        corrupt_store = ChatClearStore(self.state_dir, clock=self.clock)
        handled = threading.Event()
        worker = ChatHistoryWorkerSupervisor(
            corrupt_store,
            preview_handler=lambda _job: handled.set(),
            poll_interval_seconds=0.02,
            lock_retry_seconds=0.02,
            maintenance_interval_seconds=1,
            clock=self.clock,
        )
        self.supervisors.append(worker)
        worker.start()

        self.assertTrue(self.wait_for(lambda: worker.last_error is not None))
        self.assertFalse(worker.is_active)
        self.assertFalse(handled.is_set())


if __name__ == "__main__":
    unittest.main()
