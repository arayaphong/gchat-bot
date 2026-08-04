from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers.processing_gate import ProcessingGate


class ProcessingGateTests(unittest.TestCase):
    def test_two_instances_share_one_nonblocking_file_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "processing.lock"
            first = ProcessingGate(lock_file)
            second = ProcessingGate(lock_file)

            first_lease = first.try_acquire()
            self.assertIsNotNone(first_lease)
            self.assertIsNone(second.try_acquire())

            assert first_lease is not None
            first_lease.release()
            second_lease = second.try_acquire()
            self.assertIsNotNone(second_lease)
            assert second_lease is not None
            second_lease.release()

    def test_one_instance_preserves_nonblocking_local_busy_behavior(self) -> None:
        gate = ProcessingGate()

        lease = gate.try_acquire()
        self.assertIsNotNone(lease)
        self.assertTrue(gate.is_locked)
        self.assertIsNone(gate.try_acquire())

        assert lease is not None
        lease.release()
        self.assertFalse(gate.is_locked)


if __name__ == "__main__":
    unittest.main()
