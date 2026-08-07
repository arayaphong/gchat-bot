from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from helpers.chat_target_store import (
    ChatTarget,
    ChatTargetConflictError,
    ChatTargetStateError,
    FixedChatTargetStore,
)

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"


class FixedChatTargetStoreTests(unittest.TestCase):
    def test_first_authenticated_target_is_persisted_and_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state" / "target.json"
            store = FixedChatTargetStore(state_file)

            target = store.remember(SPACE, THREAD)

            self.assertEqual(target, ChatTarget(SPACE, THREAD))
            self.assertEqual(FixedChatTargetStore(state_file).get(), target)
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8")),
                {"space": SPACE, "thread": THREAD},
            )
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

    def test_same_space_threads_use_the_first_outbound_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FixedChatTargetStore(Path(directory) / "target.json")
            existing = store.remember(SPACE, THREAD)

            observed = store.remember(SPACE, "spaces/one/threads/other")

            self.assertEqual(observed, existing)
            self.assertEqual(store.get(), existing)

    def test_configured_target_is_fixed_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            store = FixedChatTargetStore(state_file, SPACE, THREAD)

            self.assertEqual(store.get(), ChatTarget(SPACE, THREAD))
            self.assertFalse(state_file.exists())
            self.assertEqual(
                store.remember(SPACE, "spaces/one/threads/other"),
                ChatTarget(SPACE, THREAD),
            )
            with self.assertRaises(ChatTargetConflictError):
                store.remember("spaces/other", "spaces/other/threads/new")

    def test_partial_configuration_and_mismatched_names_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            with self.assertRaises(ValueError):
                FixedChatTargetStore(state_file, configured_space=SPACE)
            with self.assertRaises(ValueError):
                FixedChatTargetStore(
                    state_file,
                    configured_space=SPACE,
                    configured_thread="spaces/other/threads/two",
                )

    def test_invalid_persisted_target_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            state_file.write_text("not-json", encoding="utf-8")

            with self.assertRaises(ChatTargetStateError):
                FixedChatTargetStore(state_file).get()

    def test_non_string_and_malformed_resource_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            state_file.write_text(
                json.dumps({"space": 123, "thread": THREAD}), encoding="utf-8"
            )
            with self.assertRaises(ChatTargetStateError):
                FixedChatTargetStore(state_file).get()

        for space, thread in (
            ("spaces/one/extra", THREAD),
            (SPACE, f"{THREAD}/extra"),
            (SPACE, "spaces/other/threads/two"),
        ):
            with (
                self.subTest(space=space, thread=thread),
                self.assertRaises(ValueError),
            ):
                ChatTarget.from_names(space, thread)

    def test_two_store_instances_share_the_first_same_space_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            stores = [
                FixedChatTargetStore(state_file),
                FixedChatTargetStore(state_file),
            ]
            barrier = threading.Barrier(2)

            def claim(store: FixedChatTargetStore, thread: str) -> ChatTarget:
                barrier.wait()
                return store.remember(SPACE, thread)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        claim,
                        stores,
                        (THREAD, "spaces/one/threads/other"),
                    )
                )

            self.assertEqual(results[0], results[1])
            self.assertEqual(FixedChatTargetStore(state_file).get(), results[0])

    def test_two_store_instances_cannot_claim_different_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            stores = [
                FixedChatTargetStore(state_file),
                FixedChatTargetStore(state_file),
            ]
            barrier = threading.Barrier(2)

            def claim(
                store: FixedChatTargetStore, target: tuple[str, str]
            ) -> ChatTarget | str:
                barrier.wait()
                try:
                    return store.remember(*target)
                except ChatTargetConflictError:
                    return "conflict"

            targets = (
                (SPACE, THREAD),
                ("spaces/other", "spaces/other/threads/new"),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, stores, targets))

            self.assertEqual(results.count("conflict"), 1)
            winner = next(result for result in results if result != "conflict")
            self.assertEqual(FixedChatTargetStore(state_file).get(), winner)

    def test_two_store_instances_can_claim_the_same_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "target.json"
            stores = [
                FixedChatTargetStore(state_file),
                FixedChatTargetStore(state_file),
            ]
            barrier = threading.Barrier(2)

            def claim(store: FixedChatTargetStore) -> ChatTarget:
                barrier.wait()
                return store.remember(SPACE, THREAD)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, stores))

            self.assertEqual(results, [ChatTarget(SPACE, THREAD)] * 2)

    def test_space_can_be_derived_from_a_full_thread_name(self) -> None:
        self.assertEqual(
            ChatTarget.from_names("", THREAD),
            ChatTarget(SPACE, THREAD),
        )


if __name__ == "__main__":
    unittest.main()
