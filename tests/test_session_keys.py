from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import Mock, call, patch

from helpers.providers import ProviderSettings
from helpers.providers.openclaw_cli import (
    create_session,
    list_sessions,
    patch_session_model,
    reset_session,
)
from helpers.providers.openclaw_client import AbortResult, OpenClawClient
from helpers.session_keys import (
    SESSION_AGENT,
    ChatSessionContext,
    derive_session_key,
    parse_session_key,
)
from helpers.session_manager import SessionManager

SPACE = "spaces/AAQAjEa3Dp8"
THREAD = "spaces/AAQAjEa3Dp8/threads/abc_123.456"
ROOT_KEY = "agent:main:gchat:AAQAjEa3Dp8:main"
THREAD_KEY = "agent:main:gchat:AAQAjEa3Dp8:abc_123.456"


def openclaw_client_mock() -> Mock:
    return Mock(spec=OpenClawClient)


class ChatSessionContextTests(unittest.TestCase):
    def test_direct_message_always_uses_the_space_main_session(self) -> None:
        context = ChatSessionContext.from_event(
            SPACE,
            THREAD,
            is_direct_message=True,
            thread_reply=True,
        )

        self.assertEqual(context.space, SPACE)
        self.assertEqual(context.reply_thread, "")
        self.assertEqual(context.session_key, ROOT_KEY)
        self.assertTrue(context.is_direct_message)
        self.assertFalse(context.is_thread)

    def test_top_level_or_missing_thread_reply_uses_main_session(self) -> None:
        for thread_reply in (False, None):
            with self.subTest(thread_reply=thread_reply):
                context = ChatSessionContext.from_event(
                    SPACE,
                    THREAD,
                    is_direct_message=False,
                    thread_reply=thread_reply,
                )

                self.assertEqual(context.reply_thread, "")
                self.assertEqual(context.session_key, ROOT_KEY)

    def test_explicit_thread_reply_uses_the_thread_id(self) -> None:
        context = ChatSessionContext.from_event(
            SPACE,
            THREAD,
            is_direct_message=False,
            thread_reply=True,
        )

        self.assertEqual(context.space, SPACE)
        self.assertEqual(context.reply_thread, THREAD)
        self.assertEqual(context.session_key, THREAD_KEY)
        self.assertFalse(context.is_direct_message)
        self.assertTrue(context.is_thread)

    def test_for_thread_forces_the_new_thread_context(self) -> None:
        self.assertEqual(
            ChatSessionContext.for_thread(SPACE, THREAD),
            ChatSessionContext(
                space=SPACE,
                reply_thread=THREAD,
                session_key=THREAD_KEY,
            ),
        )

    def test_root_and_thread_session_keys_round_trip_to_routes(self) -> None:
        for session_key, expected_thread in (
            (ROOT_KEY, ""),
            (THREAD_KEY, THREAD),
        ):
            with self.subTest(session_key=session_key):
                context = parse_session_key(session_key)
                self.assertEqual(context.space, SPACE)
                self.assertEqual(context.reply_thread, expected_thread)
                self.assertEqual(context.session_key, session_key)

    def test_derive_helper_returns_the_context_key(self) -> None:
        self.assertEqual(
            derive_session_key(
                SPACE,
                THREAD,
                is_direct_message=False,
                thread_reply=True,
            ),
            THREAD_KEY,
        )

    def test_thread_must_belong_to_the_space(self) -> None:
        for factory in (
            lambda: ChatSessionContext.from_event(
                SPACE,
                "spaces/other/threads/abc",
                is_direct_message=False,
                thread_reply=True,
            ),
            lambda: ChatSessionContext.for_thread(
                SPACE,
                "spaces/other/threads/abc",
            ),
        ):
            with self.subTest(factory=factory), self.assertRaisesRegex(
                ValueError, "does not belong"
            ):
                factory()

    def test_root_and_dm_events_still_validate_a_present_thread_parent(self) -> None:
        cross_space_thread = "spaces/other/threads/abc"
        for is_direct_message, thread_reply in (
            (False, False),
            (False, None),
            (True, False),
            (True, True),
        ):
            with self.subTest(
                is_direct_message=is_direct_message,
                thread_reply=thread_reply,
            ), self.assertRaisesRegex(ValueError, "does not belong"):
                ChatSessionContext.from_event(
                    SPACE,
                    cross_space_thread,
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                )

    def test_root_and_dm_events_allow_a_missing_thread_name(self) -> None:
        for is_direct_message, thread_reply in (
            (False, False),
            (False, None),
            (True, False),
        ):
            with self.subTest(
                is_direct_message=is_direct_message,
                thread_reply=thread_reply,
            ):
                context = ChatSessionContext.from_event(
                    SPACE,
                    "",
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                )

                self.assertEqual(context.session_key, ROOT_KEY)
                self.assertEqual(context.reply_thread, "")

    def test_reserved_main_thread_id_is_rejected(self) -> None:
        reserved_thread = f"{SPACE}/threads/main"
        for factory in (
            lambda: ChatSessionContext.from_event(
                SPACE,
                reserved_thread,
                is_direct_message=False,
                thread_reply=True,
            ),
            lambda: ChatSessionContext.for_thread(SPACE, reserved_thread),
        ):
            with self.subTest(factory=factory), self.assertRaisesRegex(
                ValueError, "reserved main"
            ):
                factory()

    def test_invalid_resources_and_session_keys_fail_closed(self) -> None:
        cases = (
            lambda: ChatSessionContext.from_event(
                "AAQAjEa3Dp8",
                THREAD,
                is_direct_message=False,
                thread_reply=False,
            ),
            lambda: ChatSessionContext.from_event(
                SPACE,
                "threads/abc",
                is_direct_message=False,
                thread_reply=True,
            ),
            lambda: parse_session_key("agent:main:gchat:only-one-component"),
            lambda: parse_session_key("agent:other:gchat:space:main"),
            lambda: parse_session_key("agent:main:gchat:space:bad/context"),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case()

    def test_event_flags_must_be_boolean_values(self) -> None:
        for is_direct_message, thread_reply in (
            ("false", False),
            (False, "true"),
        ):
            with self.subTest(
                is_direct_message=is_direct_message,
                thread_reply=thread_reply,
            ), self.assertRaises(TypeError):
                ChatSessionContext.from_event(
                    SPACE,
                    THREAD,
                    is_direct_message=is_direct_message,  # type: ignore[arg-type]
                    thread_reply=thread_reply,  # type: ignore[arg-type]
                )


class ProviderSettingsTests(unittest.TestCase):
    def test_settings_are_shared_and_contain_no_global_session_key(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OPENCLAW_AGENT": "other",
                "OPENCLAW_SESSION_KEY": "agent:main:gchat:legacy",
            },
            clear=True,
        ):
            settings = ProviderSettings.from_env()

        self.assertEqual(settings.openclaw_agent, SESSION_AGENT)
        self.assertEqual(settings.openclaw_base_url, "http://127.0.0.1:18789/v1")
        self.assertEqual(settings.openclaw_model, "openclaw/default")
        self.assertFalse(hasattr(settings, "openclaw_session_key"))


class OpenClawCliSessionTests(unittest.TestCase):
    def test_session_catalog_requests_all_deterministic_sessions(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        with patch(
            "helpers.providers.openclaw_cli._run",
            return_value=completed,
        ) as run:
            result = list_sessions()

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["sessions", "list", "--agent", "main", "--json", "--limit", "all"]
        )

    def test_create_session_sets_the_exact_key_and_initial_model(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        with patch(
            "helpers.providers.openclaw_cli._run",
            return_value=completed,
        ) as run:
            result = create_session(THREAD_KEY, "main", "minimax/MiniMax-M3")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            [
                "gateway",
                "call",
                "sessions.create",
                "--json",
                "--params",
                json.dumps(
                    {
                        "key": THREAD_KEY,
                        "agentId": "main",
                        "model": "minimax/MiniMax-M3",
                    }
                ),
            ]
        )

    def test_patch_session_model_keeps_the_exact_key(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        with patch(
            "helpers.providers.openclaw_cli._run",
            return_value=completed,
        ) as run:
            result = patch_session_model(THREAD_KEY, "minimax/MiniMax-M3")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            [
                "gateway",
                "call",
                "sessions.patch",
                "--json",
                "--params",
                json.dumps(
                    {
                        "key": THREAD_KEY,
                        "model": "minimax/MiniMax-M3",
                    }
                ),
            ]
        )

    def test_reset_session_keeps_the_exact_key_and_uses_new_reason(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

        with patch(
            "helpers.providers.openclaw_cli._run",
            return_value=completed,
        ) as run:
            result = reset_session(ROOT_KEY)

        self.assertIs(result, completed)
        run.assert_called_once_with(
            [
                "gateway",
                "call",
                "sessions.reset",
                "--json",
                "--params",
                json.dumps({"key": ROOT_KEY, "reason": "new"}),
            ]
        )


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = openclaw_client_mock()
        self.manager = SessionManager(openclaw_client=self.client)

    def test_create_with_model_uses_the_exact_key(self) -> None:
        self.client.create_session.return_value = THREAD_KEY

        created = self.manager.create_with_model(
            f" {THREAD_KEY} ",
            " minimax/MiniMax-M3 ",
        )

        self.assertEqual(created, THREAD_KEY)
        self.client.create_session.assert_called_once_with(
            THREAD_KEY,
            "minimax/MiniMax-M3",
        )

    def test_ensure_existing_session_does_not_rewrite_it(self) -> None:
        self.client.has_session.return_value = True

        ensured = self.manager.ensure_with_model(THREAD_KEY, "provider/model")

        self.assertEqual(ensured, THREAD_KEY)
        self.client.create_session.assert_not_called()
        self.client.patch_session_model.assert_not_called()

    def test_ensure_missing_session_creates_the_exact_key(self) -> None:
        self.client.has_session.return_value = False
        self.client.create_session.return_value = THREAD_KEY

        ensured = self.manager.ensure_with_model(THREAD_KEY, "provider/model")

        self.assertEqual(ensured, THREAD_KEY)
        self.client.create_session.assert_called_once_with(
            THREAD_KEY,
            "provider/model",
        )

    def test_ensure_accepts_a_concurrent_create_of_the_same_key(self) -> None:
        self.client.has_session.side_effect = [False, True]
        self.client.create_session.side_effect = RuntimeError("already exists")

        self.assertEqual(
            self.manager.ensure_with_model(THREAD_KEY, "provider/model"),
            THREAD_KEY,
        )
        self.assertEqual(
            self.client.has_session.call_args_list,
            [call(THREAD_KEY), call(THREAD_KEY)],
        )

    def test_set_model_patches_an_existing_session_without_rotation(self) -> None:
        self.client.has_session.return_value = True
        self.client.patch_session_model.return_value = THREAD_KEY

        selected = self.manager.set_model(THREAD_KEY, " provider/model ")

        self.assertEqual(selected, THREAD_KEY)
        self.client.patch_session_model.assert_called_once_with(
            THREAD_KEY,
            "provider/model",
        )
        self.client.create_session.assert_not_called()

    def test_set_model_creates_the_exact_key_when_missing(self) -> None:
        self.client.has_session.return_value = False
        self.client.create_session.return_value = ROOT_KEY

        selected = self.manager.set_model(ROOT_KEY, "provider/model")

        self.assertEqual(selected, ROOT_KEY)
        self.client.create_session.assert_called_once_with(ROOT_KEY, "provider/model")
        self.client.patch_session_model.assert_not_called()

    def test_set_model_patches_after_a_concurrent_create(self) -> None:
        self.client.has_session.side_effect = [False, True]
        self.client.create_session.side_effect = RuntimeError("already exists")
        self.client.patch_session_model.return_value = THREAD_KEY

        self.assertEqual(
            self.manager.set_model(THREAD_KEY, "provider/model"),
            THREAD_KEY,
        )
        self.client.patch_session_model.assert_called_once_with(
            THREAD_KEY,
            "provider/model",
        )

    def test_reset_existing_session_keeps_the_exact_key(self) -> None:
        self.client.has_session.return_value = True
        self.client.reset_session.return_value = ROOT_KEY

        reset_key = self.manager.reset(
            f" {ROOT_KEY} ",
            " provider/current ",
        )

        self.assertEqual(reset_key, ROOT_KEY)
        self.client.has_session.assert_called_once_with(ROOT_KEY)
        self.client.reset_session.assert_called_once_with(ROOT_KEY)
        self.client.create_session.assert_not_called()

    def test_reset_missing_session_creates_the_exact_key_with_model(self) -> None:
        self.client.has_session.return_value = False
        self.client.create_session.return_value = ROOT_KEY

        reset_key = self.manager.reset(ROOT_KEY, " provider/current ")

        self.assertEqual(reset_key, ROOT_KEY)
        self.client.create_session.assert_called_once_with(
            ROOT_KEY,
            "provider/current",
        )
        self.client.reset_session.assert_not_called()

    def test_reset_after_a_concurrent_create_resets_the_same_key(self) -> None:
        self.client.has_session.side_effect = [False, True]
        self.client.create_session.side_effect = RuntimeError("already exists")
        self.client.reset_session.return_value = ROOT_KEY

        self.assertEqual(
            self.manager.reset(ROOT_KEY, "provider/current"),
            ROOT_KEY,
        )
        self.assertEqual(
            self.client.has_session.call_args_list,
            [call(ROOT_KEY), call(ROOT_KEY)],
        )
        self.client.reset_session.assert_called_once_with(ROOT_KEY)

    def test_invalid_key_or_model_is_rejected_before_client_calls(self) -> None:
        cases = (
            ("agent:main:gchat:legacy", "provider/model"),
            (THREAD_KEY, " "),
        )
        for session_key, model in cases:
            with self.subTest(session_key=session_key, model=model), self.assertRaises(
                ValueError
            ):
                self.manager.set_model(session_key, model)
        self.client.has_session.assert_not_called()

    def test_abort_maps_the_exact_session_result(self) -> None:
        for result, expected in (
            (AbortResult(ok=True), (True, "")),
            (
                AbortResult(ok=False, reason="nothing to abort"),
                (False, "nothing to abort"),
            ),
        ):
            with self.subTest(result=result):
                self.client.abort_session.reset_mock()
                self.client.abort_session.return_value = result

                self.assertEqual(self.manager.abort(THREAD_KEY), expected)
                self.client.abort_session.assert_called_once_with(THREAD_KEY)

    def test_abort_maps_client_exceptions_to_failure(self) -> None:
        self.client.abort_session.side_effect = RuntimeError("gateway unavailable")

        self.assertEqual(
            self.manager.abort(THREAD_KEY, space=SPACE, thread=THREAD),
            (False, "gateway unavailable"),
        )


if __name__ == "__main__":
    unittest.main()
