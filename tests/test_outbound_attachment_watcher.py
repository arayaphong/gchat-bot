from __future__ import annotations

import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from helpers.outbound_attachment_watcher import (
    IN_CLOSE_WRITE,
    IN_DELETE_SELF,
    IN_MOVED_TO,
    IN_Q_OVERFLOW,
    DeliveryDisposition,
    FinalDeliveryFailure,
    OutboundAttachment,
    OutboundAttachmentConfig,
    OutboundAttachmentService,
    OutboundDeliveryResult,
)


@dataclass(frozen=True)
class FakeEvent:
    wd: int
    mask: int
    name: str = ""


class FakeInotify:
    def __init__(self) -> None:
        self._events: queue.Queue[FakeEvent | None] = queue.Queue()
        self._next_wd = 1
        self._watches: dict[Path, int] = {}
        self.closed = False

    def add_watch(self, path: str, _mask: int) -> int:
        watch_path = Path(path)
        wd = self._next_wd
        self._next_wd += 1
        self._watches[watch_path] = wd
        return wd

    def read(self, timeout: int | None = None) -> list[FakeEvent]:
        if self.closed:
            raise OSError("closed")
        try:
            event = self._events.get(timeout=(timeout or 0) / 1000)
        except queue.Empty:
            return []
        if event is None:
            raise OSError("closed")
        events = [event]
        while True:
            try:
                extra = self._events.get_nowait()
            except queue.Empty:
                return events
            if extra is None:
                return events
            events.append(extra)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._events.put(None)

    def emit(self, root: Path, name: str, mask: int) -> None:
        self._events.put(FakeEvent(self._watches[root], mask, name))

    def emit_overflow(self) -> None:
        self._events.put(FakeEvent(-1, IN_Q_OVERFLOW))


class FakeInotifyFactory:
    def __init__(self) -> None:
        self.instances: list[FakeInotify] = []
        self._condition = threading.Condition()

    def __call__(self) -> FakeInotify:
        instance = FakeInotify()
        with self._condition:
            self.instances.append(instance)
            self._condition.notify_all()
        return instance

    def wait_for_instance(self, timeout: float = 2) -> FakeInotify:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self.instances:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("inotify instance was not created")
                self._condition.wait(remaining)
            return self.instances[-1]


class FailingInotifyFactory:
    def __init__(self) -> None:
        self.called = threading.Event()

    def __call__(self) -> FakeInotify:
        self.called.set()
        raise RuntimeError("inotify initialization failed")


class ActivationHookInotify(FakeInotify):
    def __init__(self, on_watches_ready) -> None:
        super().__init__()
        self._on_watches_ready = on_watches_ready
        self._hook_ran = False

    def add_watch(self, path: str, mask: int) -> int:
        wd = super().add_watch(path, mask)
        if len(self._watches) == 2 and not self._hook_ran:
            self._hook_ran = True
            self._on_watches_ready()
            self.emit_overflow()
        return wd


class ActivationHookInotifyFactory(FakeInotifyFactory):
    def __init__(self, on_watches_ready) -> None:
        super().__init__()
        self._on_watches_ready = on_watches_ready

    def __call__(self) -> FakeInotify:
        instance = ActivationHookInotify(self._on_watches_ready)
        with self._condition:
            self.instances.append(instance)
            self._condition.notify_all()
        return instance


class OutboundAttachmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.uploads = self.base / "openclaw" / "workspace" / "uploads"
        self.generated = self.base / "openclaw" / "media" / "tool-image-generation"
        self.state = self.base / "state"
        self.services: list[OutboundAttachmentService] = []

    def tearDown(self) -> None:
        for service in reversed(self.services):
            service.stop()
        self.temporary_directory.cleanup()

    def config(self, **overrides: object) -> OutboundAttachmentConfig:
        values: dict[str, object] = {
            "source_dirs": (self.uploads, self.generated),
            "state_dir": self.state,
            "max_file_bytes": 1024 * 1024,
            "stability_checks": 1,
            "stability_interval_seconds": 0.01,
            "readiness_timeout_seconds": 1,
            "max_delivery_attempts": 3,
            "retry_delays_seconds": (0.01, 0.01),
            "capture_retry_delays_seconds": (0.01, 0.01),
            "deferral_delay_seconds": 0.01,
            "notification_retry_delay_seconds": 0.01,
            "worker_poll_seconds": 0.005,
            "inotify_read_timeout_ms": 10,
            "lock_retry_seconds": 0.01,
        }
        values.update(overrides)
        return OutboundAttachmentConfig(**values)  # type: ignore[arg-type]

    def start_service(
        self,
        delivery,
        failure=lambda _failure: None,
        *,
        config: OutboundAttachmentConfig | None = None,
        factory: FakeInotifyFactory | None = None,
    ) -> tuple[OutboundAttachmentService, FakeInotifyFactory, FakeInotify]:
        factory = factory or FakeInotifyFactory()
        service = OutboundAttachmentService(
            delivery_callback=delivery,
            final_failure_callback=failure,
            config=config or self.config(),
            inotify_factory=factory,
        )
        self.services.append(service)
        service.start()
        self.assertTrue(service.wait_until_active(2), service.last_start_error)
        return service, factory, factory.wait_for_instance()

    def wait_for(self, predicate, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not met before timeout")

    def test_close_write_and_moved_to_deliver_each_file_once(self) -> None:
        delivered: list[tuple[OutboundAttachment, bytes]] = []

        def delivery(attachment: OutboundAttachment) -> DeliveryDisposition:
            delivered.append((attachment, attachment.staged_path.read_bytes()))
            return DeliveryDisposition.DELIVERED

        _service, _factory, inotify = self.start_service(delivery)
        first = self.uploads / "report.txt"
        first.write_bytes(b"report")
        inotify.emit(self.uploads, first.name, IN_CLOSE_WRITE)
        inotify.emit(self.uploads, first.name, IN_CLOSE_WRITE)

        second = self.generated / "image.png"
        second.write_bytes(b"png-data")
        inotify.emit(self.generated, second.name, IN_MOVED_TO)

        self.wait_for(lambda: len(delivered) == 2)
        self.assertEqual([item[1] for item in delivered], [b"report", b"png-data"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(
            all(
                item[0].staged_path is not None and not item[0].staged_path.exists()
                for item in delivered
            )
        )

    def test_temp_and_hidden_files_are_ignored_by_events_and_rescan(self) -> None:
        delivered: list[str] = []
        _service, _factory, inotify = self.start_service(
            lambda item: delivered.append(item.display_name) or True
        )

        temporary = self.uploads / "report.txt.tmp"
        temporary.write_bytes(b"final contents")
        hidden = self.uploads / ".hidden-output"
        hidden.write_bytes(b"hidden")
        inotify.emit(self.uploads, temporary.name, IN_CLOSE_WRITE)
        inotify.emit(self.uploads, hidden.name, IN_CLOSE_WRITE)
        time.sleep(0.05)
        self.assertEqual(delivered, [])

        final = self.uploads / "report.txt"
        temporary.replace(final)
        inotify.emit(self.uploads, final.name, IN_MOVED_TO)
        self.wait_for(lambda: delivered == ["report.txt"])

        ignored_part = self.generated / "second.png.part"
        ignored_part.write_bytes(b"partial")
        unseen = self.generated / "complete.png"
        unseen.write_bytes(b"complete")
        inotify.emit_overflow()
        self.wait_for(lambda: delivered == ["report.txt", "complete.png"])
        time.sleep(0.05)
        self.assertEqual(delivered, ["report.txt", "complete.png"])

    def test_first_start_baselines_existing_then_restart_reconciles_new_file(
        self,
    ) -> None:
        self.uploads.mkdir(parents=True)
        old_file = self.uploads / "old.txt"
        old_file.write_bytes(b"old")
        delivered: list[str] = []

        first, _factory, _inotify = self.start_service(
            lambda item: delivered.append(item.display_name) or True
        )
        time.sleep(0.05)
        self.assertEqual(delivered, [])
        first.stop()

        new_file = self.uploads / "during-downtime.txt"
        new_file.write_bytes(b"new")
        self.start_service(lambda item: delivered.append(item.display_name) or True)
        self.wait_for(lambda: delivered == ["during-downtime.txt"])
        self.assertNotIn("old.txt", delivered)

    def test_interrupted_first_start_preserves_the_original_cutover(self) -> None:
        self.uploads.mkdir(parents=True)
        old_file = self.uploads / "old-before-cutover.txt"
        old_file.write_bytes(b"old")

        failing_factory = FailingInotifyFactory()
        interrupted = OutboundAttachmentService(
            delivery_callback=lambda _item: True,
            final_failure_callback=lambda _failure: None,
            config=self.config(lock_retry_seconds=0.1),
            inotify_factory=failing_factory,
        )
        self.services.append(interrupted)
        interrupted.start()
        self.assertTrue(failing_factory.called.wait(2))
        self.wait_for(lambda: interrupted.last_start_error is not None)
        interrupted.stop()

        created_after_cutover = self.uploads / "created-after-cutover.txt"
        created_after_cutover.write_bytes(b"new")
        delivered: list[str] = []
        self.start_service(lambda item: delivered.append(item.display_name) or True)

        self.wait_for(lambda: delivered == ["created-after-cutover.txt"])
        time.sleep(0.05)
        self.assertEqual(delivered, ["created-after-cutover.txt"])
        self.assertNotIn(old_file.name, delivered)

    def test_first_start_overflow_keeps_post_cutover_file_deliverable(self) -> None:
        self.uploads.mkdir(parents=True)
        old_file = self.uploads / "old-before-start.txt"
        old_file.write_bytes(b"old")
        created_during_start = self.uploads / "created-during-start.txt"

        factory = ActivationHookInotifyFactory(
            lambda: created_during_start.write_bytes(b"new")
        )
        delivered: list[str] = []
        self.start_service(
            lambda item: delivered.append(item.display_name) or True,
            factory=factory,
        )

        self.wait_for(lambda: delivered == ["created-during-start.txt"])
        time.sleep(0.05)
        self.assertEqual(delivered, ["created-during-start.txt"])
        self.assertNotIn(old_file.name, delivered)

    def test_delivered_file_is_not_redelivered_after_restart(self) -> None:
        delivered: list[str] = []
        first, _factory, inotify = self.start_service(
            lambda item: delivered.append(item.display_name) or True
        )
        source = self.uploads / "once.txt"
        source.write_bytes(b"once")
        inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)
        self.wait_for(lambda: delivered == ["once.txt"])
        first.stop()

        self.start_service(lambda item: delivered.append(item.display_name) or True)
        time.sleep(0.1)
        self.assertEqual(delivered, ["once.txt"])

    def test_overflow_rescan_recovers_unseen_file(self) -> None:
        delivered: list[str] = []
        _service, _factory, inotify = self.start_service(
            lambda item: delivered.append(item.display_name) or True
        )
        unseen = self.uploads / "overflow.txt"
        unseen.write_bytes(b"reconcile me")
        inotify.emit_overflow()
        self.wait_for(lambda: delivered == ["overflow.txt"])

    def test_missing_directories_are_created_and_replaced_watch_is_repaired(
        self,
    ) -> None:
        delivered: list[str] = []
        _service, _factory, inotify = self.start_service(
            lambda item: delivered.append(item.display_name) or True
        )
        self.assertTrue(self.uploads.is_dir())
        self.assertTrue(self.generated.is_dir())

        os.rmdir(self.generated)
        inotify.emit(self.generated, "", IN_DELETE_SELF)
        self.wait_for(self.generated.is_dir)
        generated = self.generated / "after-repair.png"
        generated.write_bytes(b"image")
        inotify.emit(self.generated, generated.name, IN_CLOSE_WRITE)
        self.wait_for(lambda: delivered == ["after-repair.png"])

    def test_zero_and_oversize_files_notify_but_symlink_and_directory_do_not(
        self,
    ) -> None:
        delivered: list[str] = []
        failures: list[FinalDeliveryFailure] = []
        _service, _factory, inotify = self.start_service(
            lambda item: delivered.append(item.display_name) or True,
            failures.append,
            config=self.config(max_file_bytes=5),
        )
        zero = self.uploads / "zero.txt"
        zero.touch()
        oversize = self.uploads / "large.bin"
        oversize.write_bytes(b"123456")
        target = self.uploads / "target.txt"
        target.write_bytes(b"safe")
        link = self.uploads / "link.txt"
        link.symlink_to(target)
        directory = self.uploads / "folder"
        directory.mkdir()
        for path in (zero, oversize, link, directory):
            inotify.emit(self.uploads, path.name, IN_CLOSE_WRITE)
        self.wait_for(lambda: len(failures) == 2)
        time.sleep(0.05)
        self.assertEqual(delivered, [])
        self.assertEqual(
            {failure.error_category for failure in failures},
            {"empty_file", "file_too_large"},
        )
        self.assertTrue(all(failure.attempts == 0 for failure in failures))
        self.assertTrue(
            all(failure.attachment.staged_path is None for failure in failures)
        )

    def test_deferred_delivery_does_not_consume_failure_budget(self) -> None:
        calls = 0
        failures: list[FinalDeliveryFailure] = []

        def delivery(_attachment: OutboundAttachment) -> DeliveryDisposition:
            nonlocal calls
            calls += 1
            if calls <= 3:
                return DeliveryDisposition.DEFERRED
            return DeliveryDisposition.DELIVERED

        _service, _factory, inotify = self.start_service(
            delivery,
            failures.append,
            config=self.config(max_delivery_attempts=1, retry_delays_seconds=()),
        )
        path = self.uploads / "wait-for-target.txt"
        path.write_bytes(b"pending")
        inotify.emit(self.uploads, path.name, IN_CLOSE_WRITE)
        self.wait_for(lambda: calls >= 4)
        self.assertEqual(failures, [])

    def test_failed_delivery_retries_then_notifies_once_and_cleans_stage(
        self,
    ) -> None:
        calls = 0
        failures: list[FinalDeliveryFailure] = []

        def delivery(_attachment: OutboundAttachment) -> bool:
            nonlocal calls
            calls += 1
            return False

        _service, _factory, inotify = self.start_service(delivery, failures.append)
        source = self.uploads / "failure.txt"
        source.write_bytes(b"keep both copies")
        inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)

        self.wait_for(lambda: len(failures) == 1)
        time.sleep(0.05)
        self.assertEqual(calls, 3)
        self.assertEqual(failures[0].attempts, 3)
        self.assertEqual(failures[0].error_category, "delivery_failed")
        self.assertTrue(source.exists())
        self.assertIsNotNone(failures[0].attachment.staged_path)
        assert failures[0].attachment.staged_path is not None
        self.assertFalse(failures[0].attachment.staged_path.exists())

    def test_drive_receipt_and_delivery_id_survive_retry(self) -> None:
        seen: list[OutboundAttachment] = []

        def delivery(attachment: OutboundAttachment) -> OutboundDeliveryResult:
            seen.append(attachment)
            if len(seen) == 1:
                return OutboundDeliveryResult(
                    DeliveryDisposition.FAILED,
                    drive_file_id="drive-one",
                    web_view_link="https://drive.example/one",
                )
            return OutboundDeliveryResult(DeliveryDisposition.DELIVERED)

        _service, _factory, inotify = self.start_service(delivery)
        source = self.uploads / "receipt.txt"
        source.write_bytes(b"receipt")
        inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)

        self.wait_for(lambda: len(seen) == 2)
        self.assertTrue(seen[0].delivery_id)
        self.assertEqual(seen[1].delivery_id, seen[0].delivery_id)
        self.assertEqual(seen[1].drive_file_id, "drive-one")
        self.assertEqual(seen[1].web_view_link, "https://drive.example/one")

    def test_drive_receipt_survives_process_restart(self) -> None:
        first_seen: list[OutboundAttachment] = []

        def first_delivery(
            attachment: OutboundAttachment,
        ) -> OutboundDeliveryResult:
            first_seen.append(attachment)
            return OutboundDeliveryResult(
                DeliveryDisposition.FAILED,
                drive_file_id="drive-before-restart",
                web_view_link="https://drive.example/before-restart",
            )

        config = self.config(retry_delays_seconds=(0.3, 0.3))
        first, _factory, inotify = self.start_service(
            first_delivery,
            config=config,
        )
        source = self.uploads / "restart-receipt.txt"
        source.write_bytes(b"receipt")
        inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)
        self.wait_for(lambda: first.status_counts().get("retry") == 1)
        first.stop()

        resumed: list[OutboundAttachment] = []
        self.start_service(
            lambda attachment: resumed.append(attachment) or True,
            config=config,
        )
        self.wait_for(lambda: len(resumed) == 1)

        self.assertEqual(resumed[0].delivery_id, first_seen[0].delivery_id)
        self.assertEqual(resumed[0].drive_file_id, "drive-before-restart")
        self.assertEqual(
            resumed[0].web_view_link,
            "https://drive.example/before-restart",
        )

    def test_capture_error_retries_before_delivery(self) -> None:
        delivered: list[str] = []
        failures: list[FinalDeliveryFailure] = []
        _service, _factory, inotify = self.start_service(
            lambda attachment: delivered.append(attachment.display_name) or True,
            failures.append,
        )
        source = self.uploads / "eventually-readable.txt"
        source.write_bytes(b"payload")
        real_open = os.open
        source_open_attempts = 0

        def flaky_open(path, flags, *args):
            nonlocal source_open_attempts
            if Path(path) == source and source_open_attempts < 2:
                source_open_attempts += 1
                raise PermissionError("temporarily unreadable")
            return real_open(path, flags, *args)

        with patch(
            "helpers.outbound_attachment_watcher.os.open", side_effect=flaky_open
        ):
            inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)
            self.wait_for(lambda: delivered == [source.name])

        self.assertEqual(source_open_attempts, 2)
        self.assertEqual(failures, [])

    def test_permanent_capture_error_notifies_and_does_not_leak_stage(self) -> None:
        failures: list[FinalDeliveryFailure] = []
        _service, _factory, inotify = self.start_service(
            lambda _attachment: self.fail("unreadable file must not be delivered"),
            failures.append,
        )
        source = self.uploads / "unreadable.txt"
        source.write_bytes(b"payload")
        real_open = os.open

        def denied_open(path, flags, *args):
            if Path(path) == source:
                raise PermissionError("unreadable")
            return real_open(path, flags, *args)

        with patch(
            "helpers.outbound_attachment_watcher.os.open", side_effect=denied_open
        ):
            inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)
            self.wait_for(lambda: len(failures) == 1)

        self.assertEqual(failures[0].error_category, "staging_failed")
        self.assertIsNone(failures[0].attachment.staged_path)

    def test_restart_removes_unreferenced_staging_file(self) -> None:
        first, _factory, _inotify = self.start_service(lambda _attachment: True)
        first.stop()
        orphan = self.state / "staging" / "orphan.bin"
        orphan.write_bytes(b"orphan")

        self.start_service(lambda _attachment: True)
        self.wait_for(lambda: not orphan.exists())

    def test_existing_ledger_is_migrated_for_remote_receipts(self) -> None:
        self.state.mkdir(parents=True)
        connection = sqlite3.connect(self.state / "ledger.sqlite3")
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                source_root TEXT NOT NULL,
                source_path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                staged_path TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error_category TEXT NOT NULL DEFAULT '',
                failure_notified INTEGER NOT NULL DEFAULT 0,
                next_notification_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        connection.close()

        self.start_service(lambda _attachment: True)

        migrated = sqlite3.connect(self.state / "ledger.sqlite3")
        try:
            columns = {
                str(row[1]) for row in migrated.execute("PRAGMA table_info(artifacts)")
            }
            version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
        finally:
            migrated.close()
        self.assertIn("drive_file_id", columns)
        self.assertIn("web_view_link", columns)
        self.assertEqual(version, 1)

    def test_newer_ledger_schema_fails_closed(self) -> None:
        self.state.mkdir(parents=True)
        connection = sqlite3.connect(self.state / "ledger.sqlite3")
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        factory = FakeInotifyFactory()
        service = OutboundAttachmentService(
            delivery_callback=lambda _attachment: True,
            final_failure_callback=lambda _failure: None,
            config=self.config(lock_retry_seconds=0.05),
            inotify_factory=factory,
        )
        self.services.append(service)

        service.start()
        self.wait_for(lambda: service.last_start_error is not None)

        self.assertIsInstance(service.last_start_error, RuntimeError)
        self.assertEqual(factory.instances, [])

    def test_failed_notification_is_retried_durably_until_callback_succeeds(
        self,
    ) -> None:
        notification_calls = 0

        def notify(_failure: FinalDeliveryFailure) -> None:
            nonlocal notification_calls
            notification_calls += 1
            if notification_calls < 3:
                raise RuntimeError("notification transport unavailable")

        _service, _factory, inotify = self.start_service(
            lambda _item: False,
            notify,
            config=self.config(max_delivery_attempts=1, retry_delays_seconds=()),
        )
        source = self.uploads / "notify-retry.txt"
        source.write_bytes(b"payload")
        inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)

        self.wait_for(lambda: notification_calls == 3)
        time.sleep(0.05)
        self.assertEqual(notification_calls, 3)

    def test_one_failed_notification_does_not_block_later_failures(self) -> None:
        notifications: list[str] = []
        first_attempted = False

        def notify(failure: FinalDeliveryFailure) -> None:
            nonlocal first_attempted
            name = failure.attachment.display_name
            if name == "first.txt" and not first_attempted:
                first_attempted = True
                raise RuntimeError("first notice is temporarily unavailable")
            notifications.append(name)

        _service, _factory, inotify = self.start_service(
            lambda _item: False,
            notify,
            config=self.config(
                max_delivery_attempts=1,
                retry_delays_seconds=(),
                notification_retry_delay_seconds=0.1,
            ),
        )
        first = self.uploads / "first.txt"
        second = self.uploads / "second.txt"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        inotify.emit(self.uploads, first.name, IN_CLOSE_WRITE)
        inotify.emit(self.uploads, second.name, IN_CLOSE_WRITE)

        self.wait_for(lambda: "second.txt" in notifications)
        self.assertNotIn("first.txt", notifications)
        self.wait_for(lambda: notifications.count("first.txt") == 1)

    def test_unnotified_final_failure_is_retried_after_restart(self) -> None:
        first_notification = threading.Event()

        def unavailable_notification(_failure: FinalDeliveryFailure) -> None:
            first_notification.set()
            raise RuntimeError("notification transport unavailable")

        first, _factory, inotify = self.start_service(
            lambda _item: False,
            unavailable_notification,
            config=self.config(
                max_delivery_attempts=1,
                retry_delays_seconds=(),
                notification_retry_delay_seconds=0.2,
            ),
        )
        source = self.uploads / "notify-after-restart.txt"
        source.write_bytes(b"payload")
        inotify.emit(self.uploads, source.name, IN_CLOSE_WRITE)
        self.assertTrue(first_notification.wait(2))
        first.stop()

        delivered_again: list[str] = []
        notifications: list[FinalDeliveryFailure] = []
        self.start_service(
            lambda item: delivered_again.append(item.display_name) or True,
            notifications.append,
            config=self.config(
                max_delivery_attempts=1,
                retry_delays_seconds=(),
                notification_retry_delay_seconds=0.01,
            ),
        )
        self.wait_for(lambda: len(notifications) == 1)
        self.assertEqual(delivered_again, [])
        self.assertEqual(notifications[0].error_category, "delivery_failed")

    def test_standby_retries_process_lock_and_takes_over(self) -> None:
        first_factory = FakeInotifyFactory()
        first, _factory, _inotify = self.start_service(
            lambda _item: True, factory=first_factory
        )

        second_factory = FakeInotifyFactory()
        second = OutboundAttachmentService(
            delivery_callback=lambda _item: True,
            final_failure_callback=lambda _failure: None,
            config=self.config(),
            inotify_factory=second_factory,
        )
        self.services.append(second)
        second.start()
        self.assertFalse(second.wait_until_active(0.05))

        first.stop()
        self.assertTrue(second.wait_until_active(2), second.last_start_error)
        self.assertIsNotNone(second_factory.wait_for_instance())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux inotify only")
    def test_real_inotify_close_write_delivers_once(self) -> None:
        try:
            import inotify_simple  # noqa: F401
        except ImportError:
            self.skipTest("inotify-simple is not installed")

        delivered: list[bytes] = []

        def delivery(attachment: OutboundAttachment) -> bool:
            assert attachment.staged_path is not None
            delivered.append(attachment.staged_path.read_bytes())
            return True

        service = OutboundAttachmentService(
            delivery_callback=delivery,
            final_failure_callback=lambda _failure: None,
            config=self.config(),
        )
        self.services.append(service)
        service.start()
        self.assertTrue(service.wait_until_active(2), service.last_start_error)

        output = self.uploads / "real-event.txt"
        with output.open("wb") as file_handle:
            file_handle.write(b"real inotify")
        self.wait_for(lambda: delivered == [b"real inotify"])
        time.sleep(0.05)
        self.assertEqual(delivered, [b"real inotify"])


if __name__ == "__main__":
    unittest.main()
