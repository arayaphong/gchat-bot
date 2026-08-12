from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from helpers.inbound_message_store import InboundMessageStore
from helpers.message_orchestrator import MessageOrchestrator
from helpers.providers import OpenClawClient
from helpers.session_keys import ChatSessionContext

SPACE = "spaces/one"
THREAD = "spaces/one/threads/two"
CONTEXT = ChatSessionContext.for_thread(SPACE, THREAD)
MESSAGE_NAME = "spaces/one/messages/message-one"


class ImmediateThread:
    def __init__(
        self,
        *,
        target: object,
        args: tuple[object, ...],
        daemon: bool,
    ) -> None:
        self._target = target
        self._args = args
        self.daemon = daemon

    def start(self) -> None:
        self._target(*self._args)  # type: ignore[operator]


class InboundMessageDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.gateway = Mock()
        self.session_manager = Mock()
        self.attachment_service = Mock()
        self.openclaw_client = Mock(spec=OpenClawClient)
        self.store = InboundMessageStore(
            Path(self.temporary_directory.name) / "inbound-messages"
        )
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=self.attachment_service,
            openclaw_client=self.openclaw_client,
            inbound_message_store=self.store,
        )

    def dispatch(self, message_name: str, text: str = "hello") -> None:
        self.orchestrator.dispatch(
            SPACE,
            THREAD,
            "Alice",
            text,
            [],
            context=CONTEXT,
            command_id=message_name,
        )

    def test_completed_redelivery_is_silent_and_runs_only_once(self) -> None:
        with patch(
            "helpers.message_orchestrator.threading.Thread",
            ImmediateThread,
        ):
            self.dispatch(MESSAGE_NAME)
            self.dispatch(MESSAGE_NAME)

        expected_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"gchat-bot:google-chat-message:{MESSAGE_NAME}",
            )
        )
        self.openclaw_client.send_turn.assert_called_once_with(
            "hello",
            "Alice",
            [],
            CONTEXT.session_key,
            None,
            idempotency_key=expected_key,
            resume_run_id=None,
            on_run_accepted=self.openclaw_client.send_turn.call_args.kwargs[
                "on_run_accepted"
            ],
        )
        callback = self.openclaw_client.send_turn.call_args.kwargs["on_run_accepted"]
        self.assertTrue(callable(callback))
        self.gateway.send_followup.assert_not_called()

    def test_recovered_accepted_run_is_resumed_without_a_new_logical_run(self) -> None:
        claimed = self.store.try_claim(MESSAGE_NAME)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        claimed.begin_dispatch()
        expected_run_id = claimed.idempotency_key
        claimed.record_run_id(expected_run_id)
        claimed.release()

        def complete_resumed_run(*_args: object, **kwargs: object) -> object:
            kwargs["on_run_accepted"](expected_run_id)
            return object()

        self.openclaw_client.send_turn.side_effect = complete_resumed_run

        with patch(
            "helpers.message_orchestrator.threading.Thread",
            ImmediateThread,
        ):
            self.dispatch(MESSAGE_NAME)

        call_args = self.openclaw_client.send_turn.call_args
        self.assertEqual(call_args.kwargs["resume_run_id"], expected_run_id)
        self.assertIsNone(self.store.try_claim(MESSAGE_NAME))

    def test_simultaneous_redelivery_is_not_reported_as_a_busy_turn(self) -> None:
        send_started = threading.Event()
        release_send = threading.Event()
        real_thread = threading.Thread
        workers: list[threading.Thread] = []

        def blocked_send(*_args: object, **_kwargs: object) -> object:
            send_started.set()
            if not release_send.wait(timeout=2):
                raise TimeoutError("test did not release provider turn")
            return object()

        def recording_thread(
            *, target: object, args: tuple[object, ...], daemon: bool
        ) -> threading.Thread:
            worker = real_thread(
                target=target,
                args=args,
                daemon=daemon,
            )  # type: ignore[arg-type]
            workers.append(worker)
            return worker

        self.openclaw_client.send_turn.side_effect = blocked_send
        with patch(
            "helpers.message_orchestrator.threading.Thread",
            side_effect=recording_thread,
        ):
            self.dispatch(MESSAGE_NAME)
            self.assertTrue(send_started.wait(timeout=1))
            try:
                self.dispatch(MESSAGE_NAME)
                self.openclaw_client.send_turn.assert_called_once()
                self.gateway.send_followup.assert_not_called()
            finally:
                release_send.set()
                for worker in workers:
                    worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.openclaw_client.send_turn.assert_called_once()

    def test_distinct_message_names_with_identical_text_get_distinct_keys(self) -> None:
        with patch(
            "helpers.message_orchestrator.threading.Thread",
            ImmediateThread,
        ):
            self.dispatch("spaces/one/messages/first", "same text")
            self.dispatch("spaces/one/messages/second", "same text")

        calls = self.openclaw_client.send_turn.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(
            calls[0].kwargs["idempotency_key"],
            calls[1].kwargs["idempotency_key"],
        )

    def test_thread_start_failure_releases_claim_for_a_retry(self) -> None:
        class FailingThread:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("cannot start worker")

        with (
            patch(
                "helpers.message_orchestrator.threading.Thread",
                FailingThread,
            ),
            self.assertRaisesRegex(RuntimeError, "cannot start worker"),
        ):
            self.dispatch(MESSAGE_NAME)

        reclaimed = self.store.try_claim(MESSAGE_NAME)
        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        reclaimed.release()

    def test_missing_or_cross_space_resource_name_fails_closed(self) -> None:
        for invalid_name in ("", "spaces/other/messages/message-one"):
            with (
                self.subTest(message_name=invalid_name),
                self.assertRaisesRegex(ValueError, "message.name"),
            ):
                self.dispatch(invalid_name)

        self.openclaw_client.send_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
