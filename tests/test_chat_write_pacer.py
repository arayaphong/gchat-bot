from __future__ import annotations

import multiprocessing
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from helpers.chat_clear_store import ChatWritePacer, ChatWritePacerError

SPACE = "spaces/paced-dm"


def _process_reserve(
    state_dir: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    pacer = ChatWritePacer(Path(state_dir), clock=lambda: 1_000.0)
    ready.put(True)
    start.wait(10)
    try:
        results.put(("ok", pacer.reserve(SPACE)))
    except Exception as error:  # noqa: BLE001
        results.put(("error", type(error).__name__))


class ChatWritePacerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.state_dir = Path(temporary_directory.name) / "history"

    def test_reservations_are_durable_across_instances_and_clock_rollback(self) -> None:
        first = ChatWritePacer(self.state_dir, clock=lambda: 100.0)
        second = ChatWritePacer(self.state_dir, clock=lambda: 50.0)

        self.assertEqual(first.reserve(SPACE), 100.0)
        self.assertAlmostEqual(second.reserve(SPACE), 101.1)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "history.sqlite3").stat().st_mode),
            0o600,
        )

    def test_different_spaces_do_not_delay_each_other(self) -> None:
        pacer = ChatWritePacer(self.state_dir, clock=lambda: 200.0)
        self.assertEqual(pacer.reserve("spaces/one"), 200.0)
        self.assertEqual(pacer.reserve("spaces/two"), 200.0)

    def test_wait_occurs_after_reservation_transaction_is_released(self) -> None:
        clock_value = 300.0
        first = ChatWritePacer(self.state_dir, clock=lambda: clock_value)
        first.reserve(SPACE)
        nested_slots: list[float] = []

        def sleeper(_delay: float) -> None:
            nested = ChatWritePacer(self.state_dir, clock=lambda: clock_value)
            nested_slots.append(nested.reserve(SPACE))

        waiting = ChatWritePacer(
            self.state_dir,
            clock=lambda: clock_value,
            sleeper=sleeper,
        )
        self.assertAlmostEqual(waiting.wait_for_turn(SPACE), 301.1)
        self.assertEqual(len(nested_slots), 1)
        self.assertAlmostEqual(nested_slots[0], 302.2)

    def test_threads_get_unique_ordered_slots(self) -> None:
        pacer = ChatWritePacer(self.state_dir, clock=lambda: 500.0)
        slots: list[float] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def reserve() -> None:
            try:
                barrier.wait()
                slots.append(pacer.reserve(SPACE))
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=reserve) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(errors, [])
        self.assertEqual(len(slots), 6)
        self.assertEqual(
            sorted(round(slot, 6) for slot in slots),
            [500.0, 501.1, 502.2, 503.3, 504.4, 505.5],
        )

    def test_processes_share_the_same_durable_schedule(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_process_reserve,
                args=(str(self.state_dir), ready, start, results),
            )
            for _ in range(3)
        ]
        for process in processes:
            process.start()
        for _ in processes:
            self.assertTrue(ready.get(timeout=10))
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)

        self.assertTrue(all(status == "ok" for status, _value in outcomes), outcomes)
        slots = sorted(round(float(value), 6) for _status, value in outcomes)
        self.assertEqual(slots, [1000.0, 1001.1, 1002.2])

    def test_invalid_space_and_symlink_state_fail_closed(self) -> None:
        pacer = ChatWritePacer(self.state_dir, clock=lambda: 0.0)
        with self.assertRaises(ChatWritePacerError):
            pacer.reserve("not-a-space")

        target = self.state_dir.parent / "target"
        target.mkdir()
        self.state_dir.symlink_to(target, target_is_directory=True)
        linked = ChatWritePacer(self.state_dir, clock=lambda: 0.0)
        with self.assertRaises(ChatWritePacerError):
            linked.reserve(SPACE)

        self.assertFalse((target / "history.sqlite3").exists())


if __name__ == "__main__":
    # Spawned children import this module; keep all execution under unittest.
    unittest.main()
