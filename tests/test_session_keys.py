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
    MAX_SESSION_KEY_LENGTH,
    SESSION_AGENT,
    ChatSessionContext,
    derive_session_key,
    normalize_space_name,
    normalize_thread_name,
    parse_session_key,
)
from helpers.session_manager import SessionManager

SPACE = "spaces/AAQAjEa3Dp8"
THREAD = "spaces/AAQAjEa3Dp8/threads/abc_123.456"
SESSION_KEY = "agent:main:gchat:%41%41%51%41j%45a3%44p8:abc_123.456"
LEGACY_MAIN_KEY = "agent:main:gchat:%41%41%51%41j%45a3%44p8:main"


def openclaw_client_mock() -> Mock:
    return Mock(spec=OpenClawClient)


class ChatSessionContextTests(unittest.TestCase):
    def test_dm_root_and_reply_share_the_canonical_thread_identity(self) -> None:
        cases = (
            (True, False, ""),
            (True, True, ""),
            (False, False, THREAD),
            (False, None, THREAD),
            (False, True, THREAD),
        )
        for is_direct_message, thread_reply, expected_reply_thread in cases:
            with self.subTest(
                is_direct_message=is_direct_message,
                thread_reply=thread_reply,
            ):
                context = ChatSessionContext.from_event(
                    SPACE,
                    THREAD,
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                )

                self.assertEqual(context.space, SPACE)
                self.assertEqual(context.thread, THREAD)
                self.assertEqual(context.reply_thread, expected_reply_thread)
                self.assertEqual(context.session_key, SESSION_KEY)
                self.assertEqual(context.session_key, context.session_key.lower())
                self.assertEqual(context.is_direct_message, is_direct_message)
                self.assertEqual(context.is_thread, not is_direct_message)

    def test_mixed_case_and_reserved_bytes_round_trip_without_case_loss(self) -> None:
        space = "spaces/Aa-%"
        thread = "spaces/Aa-%/threads/Zz~"
        expected_key = "agent:main:gchat:%41a-%25:%5az%7e"

        context = ChatSessionContext.from_event(
            space,
            thread,
            is_direct_message=True,
            thread_reply=False,
        )

        self.assertEqual(context.session_key, expected_key)
        self.assertEqual(context.session_key, context.session_key.lower())
        parsed = parse_session_key(expected_key)
        self.assertEqual(parsed.space, space)
        self.assertEqual(parsed.thread, thread)

    def test_case_sensitive_ids_remain_distinct_after_encoding(self) -> None:
        uppercase = ChatSessionContext.for_thread(
            "spaces/A",
            "spaces/A/threads/x",
        )
        lowercase = ChatSessionContext.for_thread(
            "spaces/a",
            "spaces/a/threads/x",
        )

        self.assertEqual(uppercase.session_key, "agent:main:gchat:%41:x")
        self.assertEqual(lowercase.session_key, "agent:main:gchat:a:x")
        self.assertNotEqual(uppercase.session_key, lowercase.session_key)

    def test_unicode_ids_round_trip_through_strict_utf8_encoding(self) -> None:
        space = "spaces/กA"
        thread = "spaces/กA/threads/ßZ"

        context = ChatSessionContext.for_thread(space, thread)

        self.assertEqual(context.session_key, context.session_key.lower())
        self.assertEqual(parse_session_key(context.session_key).space, space)
        self.assertEqual(parse_session_key(context.session_key).thread, thread)

    def test_openclaw_lowercase_canonicalization_is_idempotent(self) -> None:
        raw_legacy_key = "agent:main:gchat:AAQAjEa3Dp8:abc_123.456"

        self.assertNotEqual(raw_legacy_key.lower(), raw_legacy_key)
        self.assertEqual(SESSION_KEY.lower(), SESSION_KEY)

    def test_for_thread_forces_the_new_thread_context(self) -> None:
        self.assertEqual(
            ChatSessionContext.for_thread(SPACE, THREAD),
            ChatSessionContext(
                space=SPACE,
                thread=THREAD,
                reply_thread=THREAD,
                session_key=SESSION_KEY,
            ),
        )

    def test_session_key_round_trip_defaults_to_the_full_thread_reply_route(
        self,
    ) -> None:
        context = parse_session_key(SESSION_KEY)

        self.assertEqual(context.space, SPACE)
        self.assertEqual(context.thread, THREAD)
        self.assertEqual(context.reply_thread, THREAD)
        self.assertEqual(context.session_key, SESSION_KEY)
        self.assertFalse(context.is_direct_message)

    def test_derive_helper_returns_the_context_key(self) -> None:
        for is_direct_message, thread_reply in (
            (True, False),
            (False, False),
            (False, None),
            (False, True),
        ):
            with self.subTest(
                is_direct_message=is_direct_message,
                thread_reply=thread_reply,
            ):
                self.assertEqual(
                    derive_session_key(
                        SPACE,
                        THREAD,
                        is_direct_message=is_direct_message,
                        thread_reply=thread_reply,
                    ),
                    SESSION_KEY,
                )

    def test_public_resource_validators_return_canonical_names(self) -> None:
        self.assertEqual(normalize_space_name(f" {SPACE} "), SPACE)
        self.assertEqual(normalize_thread_name(f" {SPACE} ", f" {THREAD} "), THREAD)

    def test_thread_must_belong_to_the_space(self) -> None:
        cross_space_thread = "spaces/other/threads/abc"
        for is_direct_message, thread_reply in (
            (True, False),
            (True, True),
            (False, False),
            (False, None),
            (False, True),
        ):
            with (
                self.subTest(
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                ),
                self.assertRaisesRegex(ValueError, "does not belong"),
            ):
                ChatSessionContext.from_event(
                    SPACE,
                    cross_space_thread,
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                )

        for factory in (
            lambda: ChatSessionContext.for_thread(
                SPACE,
                cross_space_thread,
            ),
            lambda: normalize_thread_name(
                SPACE,
                cross_space_thread,
            ),
        ):
            with (
                self.subTest(factory=factory),
                self.assertRaisesRegex(ValueError, "does not belong"),
            ):
                factory()

    def test_every_event_requires_a_full_thread_resource_name(self) -> None:
        for is_direct_message, thread_reply in (
            (False, False),
            (False, None),
            (True, False),
            (True, True),
        ):
            with (
                self.subTest(
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                ),
                self.assertRaises(ValueError),
            ):
                ChatSessionContext.from_event(
                    SPACE,
                    "",
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                )

    def test_reserved_main_thread_id_is_rejected(self) -> None:
        reserved_thread = f"{SPACE}/threads/main"
        for is_direct_message, thread_reply in (
            (True, False),
            (False, False),
            (False, True),
        ):
            with (
                self.subTest(
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                ),
                self.assertRaises(ValueError),
            ):
                ChatSessionContext.from_event(
                    SPACE,
                    reserved_thread,
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                )

        for factory in (
            lambda: ChatSessionContext.for_thread(SPACE, reserved_thread),
            lambda: normalize_thread_name(SPACE, reserved_thread),
            lambda: parse_session_key(LEGACY_MAIN_KEY),
        ):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
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
            lambda: parse_session_key("agent:other:gchat:space:thread"),
            lambda: parse_session_key("agent:main:gchat:space:bad/context"),
            lambda: parse_session_key("agent:main:gchat:Space:thread"),
            lambda: parse_session_key("agent:main:gchat:%4A:thread"),
            lambda: parse_session_key("agent:main:gchat:%zz:thread"),
            lambda: parse_session_key("agent:main:gchat:%:thread"),
            lambda: parse_session_key("agent:main:gchat:%0:thread"),
            lambda: parse_session_key("agent:main:gchat:%ff:thread"),
            lambda: parse_session_key("agent:main:gchat:%6a:thread"),
            lambda: parse_session_key("agent:main:gchat:%2f:thread"),
            lambda: parse_session_key("agent:main:gchat:%3a:thread"),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case()

    def test_session_key_length_limit_is_enforced_before_openclaw(self) -> None:
        long_space_id = "A" * 170
        with self.assertRaisesRegex(ValueError, "too long"):
            ChatSessionContext.for_thread(
                f"spaces/{long_space_id}",
                f"spaces/{long_space_id}/threads/x",
            )

        oversized_key = f"agent:main:gchat:{'a' * MAX_SESSION_KEY_LENGTH}:b"
        with self.assertRaisesRegex(ValueError, "too long"):
            parse_session_key(oversized_key)

    def test_event_flags_must_be_boolean_values(self) -> None:
        for is_direct_message, thread_reply in (
            ("false", False),
            (False, "true"),
        ):
            with (
                self.subTest(
                    is_direct_message=is_direct_message,
                    thread_reply=thread_reply,
                ),
                self.assertRaises(TypeError),
            ):
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
            result = create_session(SESSION_KEY, "main", "minimax/MiniMax-M3")

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
                        "key": SESSION_KEY,
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
            result = patch_session_model(SESSION_KEY, "minimax/MiniMax-M3")

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
                        "key": SESSION_KEY,
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
            result = reset_session(SESSION_KEY)

        self.assertIs(result, completed)
        run.assert_called_once_with(
            [
                "gateway",
                "call",
                "sessions.reset",
                "--json",
                "--params",
                json.dumps({"key": SESSION_KEY, "reason": "new"}),
            ]
        )


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = openclaw_client_mock()
        self.manager = SessionManager(openclaw_client=self.client)

    def test_create_with_model_uses_the_exact_key(self) -> None:
        self.client.create_session.return_value = SESSION_KEY

        created = self.manager.create_with_model(
            f" {SESSION_KEY} ",
            " minimax/MiniMax-M3 ",
        )

        self.assertEqual(created, SESSION_KEY)
        self.client.create_session.assert_called_once_with(
            SESSION_KEY,
            "minimax/MiniMax-M3",
        )

    def test_ensure_existing_session_does_not_rewrite_it(self) -> None:
        self.client.has_session.return_value = True

        ensured = self.manager.ensure_with_model(SESSION_KEY, "provider/model")

        self.assertEqual(ensured, SESSION_KEY)
        self.client.create_session.assert_not_called()
        self.client.patch_session_model.assert_not_called()

    def test_ensure_missing_session_creates_the_exact_key(self) -> None:
        self.client.has_session.return_value = False
        self.client.create_session.return_value = SESSION_KEY

        ensured = self.manager.ensure_with_model(SESSION_KEY, "provider/model")

        self.assertEqual(ensured, SESSION_KEY)
        self.client.create_session.assert_called_once_with(
            SESSION_KEY,
            "provider/model",
        )

    def test_ensure_accepts_a_concurrent_create_of_the_same_key(self) -> None:
        self.client.has_session.side_effect = [False, True]
        self.client.create_session.side_effect = RuntimeError("already exists")

        self.assertEqual(
            self.manager.ensure_with_model(SESSION_KEY, "provider/model"),
            SESSION_KEY,
        )
        self.assertEqual(
            self.client.has_session.call_args_list,
            [call(SESSION_KEY), call(SESSION_KEY)],
        )

    def test_set_model_patches_an_existing_session_without_rotation(self) -> None:
        self.client.has_session.return_value = True
        self.client.patch_session_model.return_value = SESSION_KEY

        selected = self.manager.set_model(SESSION_KEY, " provider/model ")

        self.assertEqual(selected, SESSION_KEY)
        self.client.patch_session_model.assert_called_once_with(
            SESSION_KEY,
            "provider/model",
        )
        self.client.create_session.assert_not_called()

    def test_set_model_creates_the_exact_key_when_missing(self) -> None:
        self.client.has_session.return_value = False
        self.client.create_session.return_value = SESSION_KEY

        selected = self.manager.set_model(SESSION_KEY, "provider/model")

        self.assertEqual(selected, SESSION_KEY)
        self.client.create_session.assert_called_once_with(
            SESSION_KEY, "provider/model"
        )
        self.client.patch_session_model.assert_not_called()

    def test_set_model_patches_after_a_concurrent_create(self) -> None:
        self.client.has_session.side_effect = [False, True]
        self.client.create_session.side_effect = RuntimeError("already exists")
        self.client.patch_session_model.return_value = SESSION_KEY

        self.assertEqual(
            self.manager.set_model(SESSION_KEY, "provider/model"),
            SESSION_KEY,
        )
        self.client.patch_session_model.assert_called_once_with(
            SESSION_KEY,
            "provider/model",
        )

    def test_reset_existing_session_keeps_the_exact_key(self) -> None:
        self.client.has_session.return_value = True
        self.client.reset_session.return_value = SESSION_KEY

        reset_key = self.manager.reset(
            f" {SESSION_KEY} ",
            " provider/current ",
        )

        self.assertEqual(reset_key, SESSION_KEY)
        self.client.has_session.assert_called_once_with(SESSION_KEY)
        self.client.reset_session.assert_called_once_with(SESSION_KEY)
        self.client.create_session.assert_not_called()

    def test_reset_missing_session_creates_the_exact_key_with_model(self) -> None:
        self.client.has_session.return_value = False
        self.client.create_session.return_value = SESSION_KEY

        reset_key = self.manager.reset(SESSION_KEY, " provider/current ")

        self.assertEqual(reset_key, SESSION_KEY)
        self.client.create_session.assert_called_once_with(
            SESSION_KEY,
            "provider/current",
        )
        self.client.reset_session.assert_not_called()

    def test_reset_after_a_concurrent_create_resets_the_same_key(self) -> None:
        self.client.has_session.side_effect = [False, True]
        self.client.create_session.side_effect = RuntimeError("already exists")
        self.client.reset_session.return_value = SESSION_KEY

        self.assertEqual(
            self.manager.reset(SESSION_KEY, "provider/current"),
            SESSION_KEY,
        )
        self.assertEqual(
            self.client.has_session.call_args_list,
            [call(SESSION_KEY), call(SESSION_KEY)],
        )
        self.client.reset_session.assert_called_once_with(SESSION_KEY)

    def test_invalid_key_or_model_is_rejected_before_client_calls(self) -> None:
        cases = (
            ("agent:main:gchat:legacy", "provider/model"),
            (SESSION_KEY, " "),
        )
        for session_key, model in cases:
            with (
                self.subTest(session_key=session_key, model=model),
                self.assertRaises(ValueError),
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

                self.assertEqual(self.manager.abort(SESSION_KEY), expected)
                self.client.abort_session.assert_called_once_with(SESSION_KEY)

    def test_abort_maps_client_exceptions_to_failure(self) -> None:
        self.client.abort_session.side_effect = RuntimeError("gateway unavailable")

        self.assertEqual(
            self.manager.abort(SESSION_KEY, space=SPACE, thread=THREAD),
            (False, "gateway unavailable"),
        )


if __name__ == "__main__":
    unittest.main()
