from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from helpers.inbound_message_store import (
    InboundMessageStore,
    InboundMessageStoreError,
)


class InboundMessageStoreTests(unittest.TestCase):
    MESSAGE_NAME = "spaces/AAAA/messages/BBBB.BBBB"

    def test_pre_dispatch_release_can_be_reclaimed_with_same_uuid5_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboundMessageStore(Path(directory) / "inbound")

            first = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(first)
            assert first is not None
            expected_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"gchat-bot:google-chat-message:{self.MESSAGE_NAME}",
                )
            )
            self.assertEqual(first.idempotency_key, expected_key)
            self.assertFalse(first.recovered)
            self.assertFalse(first.dispatch_started)
            self.assertIsNone(first.run_id)
            first.release()

            second = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.idempotency_key, expected_key)
            self.assertFalse(second.recovered)
            second.release()

    def test_begin_dispatch_persists_expected_run_id_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            first_store = InboundMessageStore(state_dir)
            first = first_store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(first)
            assert first is not None
            first.begin_dispatch()
            self.assertTrue(first.dispatch_started)
            # A fresh lease still needs to make the initial agent request.
            self.assertFalse(first.recovered)
            self.assertIsNone(first.run_id)
            first.release()

            restarted_store = InboundMessageStore(state_dir)
            recovered = restarted_store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertTrue(recovered.recovered)
            self.assertTrue(recovered.dispatch_started)
            self.assertEqual(recovered.run_id, first.idempotency_key)
            self.assertEqual(recovered.idempotency_key, first.idempotency_key)
            recovered.release()

    def test_expected_run_id_is_the_only_accepted_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            first_store = InboundMessageStore(state_dir)
            first = first_store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(first)
            assert first is not None
            first.begin_dispatch()
            first.record_run_id(first.idempotency_key)
            first.record_run_id(first.idempotency_key)
            first.release()

            recovered = InboundMessageStore(state_dir).try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertTrue(recovered.recovered)
            self.assertEqual(recovered.run_id, recovered.idempotency_key)
            with self.assertRaisesRegex(
                InboundMessageStoreError,
                "another run",
            ):
                recovered.record_run_id("run-different")
            recovered.release()

    def test_record_run_requires_begin_dispatch_and_valid_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboundMessageStore(Path(directory) / "inbound")
            lease = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(lease)
            assert lease is not None
            with self.assertRaisesRegex(InboundMessageStoreError, "before provider"):
                lease.record_run_id("run-one")
            lease.begin_dispatch()
            for invalid in ("", "  ", " run-one"):
                with self.subTest(run_id=invalid), self.assertRaises(ValueError):
                    lease.record_run_id(invalid)
            with self.assertRaisesRegex(InboundMessageStoreError, "another run"):
                lease.record_run_id("run-one")
            lease.record_run_id(lease.idempotency_key)
            lease.release()

    def test_two_instances_reject_a_simultaneous_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            first_store = InboundMessageStore(state_dir)
            second_store = InboundMessageStore(state_dir)

            first = first_store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(first)
            self.assertIsNone(second_store.try_claim(self.MESSAGE_NAME))

            assert first is not None
            first.release()
            second = second_store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(second)
            assert second is not None
            second.release()

    def test_completed_tombstone_survives_a_new_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            store = InboundMessageStore(state_dir)
            lease = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(lease)
            assert lease is not None
            lease.begin_dispatch()
            lease.record_run_id(lease.idempotency_key)
            lease.complete()
            lease.release()

            restarted_store = InboundMessageStore(state_dir)
            self.assertIsNone(restarted_store.try_claim(self.MESSAGE_NAME))

            digest = hashlib.sha256(self.MESSAGE_NAME.encode("utf-8")).hexdigest()
            self.assertEqual((state_dir.stat().st_mode & 0o777), 0o700)
            self.assertEqual((state_dir / "locks").stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (state_dir / "in-progress").stat().st_mode & 0o777,
                0o700,
            )
            self.assertEqual(
                (state_dir / "completed").stat().st_mode & 0o777,
                0o700,
            )
            self.assertEqual(
                (state_dir / "locks" / digest).stat().st_mode & 0o777,
                0o600,
            )
            self.assertFalse((state_dir / "in-progress" / digest).exists())
            self.assertEqual(
                (state_dir / "completed" / digest).stat().st_mode & 0o777,
                0o600,
            )

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process semantics")
    def test_process_exit_releases_an_inflight_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            ready_read, ready_write = os.pipe()
            exit_read, exit_write = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    os.close(ready_read)
                    os.close(exit_write)
                    child_store = InboundMessageStore(state_dir)
                    child_lease = child_store.try_claim(self.MESSAGE_NAME)
                    os.write(ready_write, b"1" if child_lease is not None else b"0")
                    os.close(ready_write)
                    os.read(exit_read, 1)
                finally:
                    os._exit(0)

            os.close(ready_write)
            os.close(exit_read)
            child_reaped = False
            try:
                self.assertEqual(os.read(ready_read, 1), b"1")
                parent_store = InboundMessageStore(state_dir)
                self.assertIsNone(parent_store.try_claim(self.MESSAGE_NAME))

                os.write(exit_write, b"x")
                os.close(exit_write)
                exit_write = -1
                _, status = os.waitpid(child_pid, 0)
                child_reaped = True
                self.assertTrue(os.WIFEXITED(status))

                reclaimed = parent_store.try_claim(self.MESSAGE_NAME)
                self.assertIsNotNone(reclaimed)
                assert reclaimed is not None
                self.assertFalse(reclaimed.recovered)
                reclaimed.release()
            finally:
                os.close(ready_read)
                if exit_write >= 0:
                    os.close(exit_write)
                if not child_reaped:
                    os.waitpid(child_pid, 0)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process semantics")
    def test_process_exit_after_begin_dispatch_recovers_expected_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            ready_read, ready_write = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    os.close(ready_read)
                    child_store = InboundMessageStore(state_dir)
                    child_lease = child_store.try_claim(self.MESSAGE_NAME)
                    if child_lease is None:
                        os.write(ready_write, b"0")
                    else:
                        child_lease.begin_dispatch()
                        os.write(ready_write, b"1")
                finally:
                    os.close(ready_write)
                    os._exit(0)

            os.close(ready_write)
            try:
                self.assertEqual(os.read(ready_read, 1), b"1")
                _, status = os.waitpid(child_pid, 0)
                self.assertTrue(os.WIFEXITED(status))

                recovered = InboundMessageStore(state_dir).try_claim(self.MESSAGE_NAME)
                self.assertIsNotNone(recovered)
                assert recovered is not None
                self.assertTrue(recovered.recovered)
                self.assertTrue(recovered.dispatch_started)
                self.assertEqual(recovered.run_id, recovered.idempotency_key)
                recovered.release()
            finally:
                os.close(ready_read)

    def test_names_are_exact_and_blank_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboundMessageStore(Path(directory) / "inbound")
            with self.assertRaises(ValueError):
                store.try_claim(" \t\n")

            first = store.try_claim(self.MESSAGE_NAME)
            second = store.try_claim(f"{self.MESSAGE_NAME} ")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first.idempotency_key, second.idempotency_key)
            first.release()
            second.release()

    def test_symlink_state_and_completion_marker_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            state_link = root / "state-link"
            state_link.symlink_to(outside, target_is_directory=True)
            unsafe_store = InboundMessageStore(state_link)
            with self.assertRaises(InboundMessageStoreError):
                unsafe_store.try_claim(self.MESSAGE_NAME)

            state_dir = root / "inbound"
            store = InboundMessageStore(state_dir)
            initialization_lease = store.try_claim(
                "spaces/AAAA/messages/initialize-state"
            )
            self.assertIsNotNone(initialization_lease)
            assert initialization_lease is not None
            initialization_lease.release()
            digest = hashlib.sha256(self.MESSAGE_NAME.encode("utf-8")).hexdigest()
            target = root / "marker-target"
            target.write_text("do not follow", encoding="utf-8")
            (state_dir / "completed" / digest).symlink_to(target)
            with self.assertRaises(InboundMessageStoreError):
                store.try_claim(self.MESSAGE_NAME)
            self.assertEqual(target.read_text(encoding="utf-8"), "do not follow")

    def test_corrupt_or_mismatched_in_progress_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            store = InboundMessageStore(state_dir)
            lease = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(lease)
            assert lease is not None
            lease.begin_dispatch()
            lease.release()

            digest = hashlib.sha256(self.MESSAGE_NAME.encode("utf-8")).hexdigest()
            marker = state_dir / "in-progress" / digest
            marker.write_text(
                '{"version":1,"message_name":"spaces/wrong/messages/wrong",'
                '"idempotency_key":"wrong","run_id":"run-one"}',
                encoding="utf-8",
            )
            marker.chmod(0o600)

            with self.assertRaisesRegex(InboundMessageStoreError, "does not match"):
                store.try_claim(self.MESSAGE_NAME)

    def test_recovered_run_id_must_match_the_stable_provider_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            store = InboundMessageStore(state_dir)
            lease = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(lease)
            assert lease is not None
            lease.begin_dispatch()
            lease.release()

            digest = hashlib.sha256(self.MESSAGE_NAME.encode("utf-8")).hexdigest()
            marker = state_dir / "in-progress" / digest
            encoded = marker.read_text(encoding="utf-8")
            marker.write_text(
                encoded.replace(
                    f'"run_id":"{lease.idempotency_key}"',
                    '"run_id":"different-run-id"',
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)

            with self.assertRaisesRegex(
                InboundMessageStoreError,
                "provider identity",
            ):
                store.try_claim(self.MESSAGE_NAME)

    def test_complete_never_removes_in_progress_before_tombstone_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "inbound"
            store = InboundMessageStore(state_dir)
            lease = store.try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(lease)
            assert lease is not None
            lease.begin_dispatch()

            original_write = store._write_completion

            def fail_completion(_digest: str, _marker: bytes) -> None:
                raise InboundMessageStoreError("simulated completion failure")

            store._write_completion = fail_completion  # type: ignore[method-assign]
            with self.assertRaisesRegex(InboundMessageStoreError, "simulated"):
                lease.complete()
            lease.release()

            store._write_completion = original_write  # type: ignore[method-assign]
            recovered = InboundMessageStore(state_dir).try_claim(self.MESSAGE_NAME)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertTrue(recovered.dispatch_started)
            recovered.release()


if __name__ == "__main__":
    unittest.main()
