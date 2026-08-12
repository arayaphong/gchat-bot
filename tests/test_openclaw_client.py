"""Contract tests for the transport-hiding OpenClaw facade."""

from __future__ import annotations

import json
import subprocess
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from helpers.providers.openclaw_client import (
    AbortResult,
    OpenClawClient,
    SendTurnResult,
)
from helpers.providers.openclaw_ws import OpenclawRunCancelled
from helpers.providers.provider_settings import ProviderSettings
from helpers.session_keys import ChatSessionContext
from helpers.thread_uploads import thread_upload_directory


class OpenClawClientSendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OpenClawClient(
            agent="main",
            base_url="http://127.0.0.1:18789/v1",
            model="openclaw/default",
        )
        self.session_key = "agent:main:gchat:c0ffee"

    def test_send_turn_routes_to_the_http_transport(self) -> None:
        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                return_value={"text": "", "run_id": "chatcmpl_abc"},
            ) as send_http,
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=[],
            ) as check_errors,
        ):
            result = self.client.send_turn(
                "hello",
                "Alice",
                [],
                self.session_key,
            )

        self.assertEqual(
            result,
            SendTurnResult(text="", run_id="chatcmpl_abc"),
        )
        send_http.assert_called_once_with(
            "hello",
            "Alice",
            [],
            self.session_key,
            "http://127.0.0.1:18789/v1",
            "openclaw/default",
            None,
        )
        check_errors.assert_called_once()
        run_id, sent_at = check_errors.call_args.args
        self.assertEqual(run_id, "chatcmpl_abc")
        self.assertIsInstance(sent_at, datetime)

    def test_send_turn_forwards_an_explicit_idempotency_key(self) -> None:
        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                return_value={"text": "", "run_id": "run-stable"},
            ) as send_http,
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=[],
            ),
        ):
            result = self.client.send_turn(
                "hello",
                "Alice",
                [],
                self.session_key,
                idempotency_key="gchat-message-123",
            )

        self.assertEqual(result, SendTurnResult(text="", run_id="run-stable"))
        send_http.assert_called_once_with(
            "hello",
            "Alice",
            [],
            self.session_key,
            "http://127.0.0.1:18789/v1",
            "openclaw/default",
            None,
            idempotency_key="gchat-message-123",
        )

    def test_send_turn_forwards_resume_state_and_preserves_cancellation(self) -> None:
        accepted = unittest.mock.Mock()
        cancelled = OpenclawRunCancelled("run-resumed")
        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                side_effect=cancelled,
            ) as send_http,
            self.assertRaises(OpenclawRunCancelled) as raised,
        ):
            self.client.send_turn(
                "hello",
                "Alice",
                [],
                self.session_key,
                idempotency_key="stable-key",
                resume_run_id="run-resumed",
                on_run_accepted=accepted,
            )

        self.assertIs(raised.exception, cancelled)
        self.assertEqual(send_http.call_args.kwargs["resume_run_id"], "run-resumed")
        self.assertIs(send_http.call_args.kwargs["on_run_accepted"], accepted)

    def test_configured_upload_root_adds_the_current_thread_directory(self) -> None:
        context = ChatSessionContext.for_thread(
            "spaces/one",
            "spaces/one/threads/two",
        )
        upload_root = Path("/home/arme/.openclaw/workspace/uploads")
        client = OpenClawClient(
            agent="main",
            base_url="http://127.0.0.1:18789/v1",
            model="openclaw/default",
            outbound_upload_root=upload_root,
        )
        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                return_value={"text": "", "run_id": "chatcmpl_scoped"},
            ) as send_http,
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=[],
            ),
        ):
            client.send_turn("hello", "Alice", [], context.session_key)

        send_http.assert_called_once_with(
            "hello",
            "Alice",
            [],
            context.session_key,
            "http://127.0.0.1:18789/v1",
            "openclaw/default",
            None,
            outbound_upload_directory=thread_upload_directory(
                upload_root,
                context.session_key,
            ),
        )

    def test_quoted_message_and_inbound_files_are_forwarded(self) -> None:
        files = [{"path": "/tmp/photo.png", "mimeType": "image/png"}]
        quoted = {"sender": "Bob", "text": "previous message"}

        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                return_value={"text": "", "run_id": "chatcmpl_quote"},
            ) as send_http,
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=[],
            ),
        ):
            result = self.client.send_turn(
                "inspect",
                "Alice",
                files,
                self.session_key,
                quoted,
            )

        self.assertEqual(
            result,
            SendTurnResult(text="", run_id="chatcmpl_quote"),
        )
        send_http.assert_called_once_with(
            "inspect",
            "Alice",
            files,
            self.session_key,
            "http://127.0.0.1:18789/v1",
            "openclaw/default",
            quoted,
        )

    def test_model_shaped_text_is_forwarded_unchanged_to_the_http_transport(
        self,
    ) -> None:
        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                return_value={"text": "", "run_id": "chatcmpl_model"},
            ) as send_http,
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=[],
            ),
        ):
            result = self.client.send_turn(
                "/model minimax/MiniMax-M3",
                "Alice",
                [],
                self.session_key,
            )

        self.assertEqual(
            result,
            SendTurnResult(text="", run_id="chatcmpl_model"),
        )
        send_http.assert_called_once_with(
            "/model minimax/MiniMax-M3",
            "Alice",
            [],
            self.session_key,
            "http://127.0.0.1:18789/v1",
            "openclaw/default",
            None,
        )

    def test_send_turn_normalizes_gateway_error_payloads(self) -> None:
        transport_error = RuntimeError(
            'openclaw failed (status=400): {"error":{"message":"model rejected"}}'
        )

        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                side_effect=transport_error,
            ),
            patch("helpers.providers.openclaw_client.check_run_errors") as check_errors,
            self.assertRaisesRegex(
                RuntimeError,
                "^เกิดข้อผิดพลาด: model rejected$",
            ) as raised,
        ):
            self.client.send_turn("hello", "Alice", [], self.session_key)

        self.assertIs(raised.exception.__cause__, transport_error)
        check_errors.assert_not_called()

    def test_send_turn_prints_each_run_specific_logcheck_warning(self) -> None:
        warnings = [
            "gateway errorMessage for chatcmpl_warn",
            "gateway AbortError for chatcmpl_warn",
        ]

        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                return_value={"text": "", "run_id": "chatcmpl_warn"},
            ),
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=warnings,
            ),
            patch("builtins.print") as print_message,
        ):
            result = self.client.send_turn(
                "hello",
                "Alice",
                [],
                self.session_key,
            )

        self.assertEqual(result, SendTurnResult(text="", run_id="chatcmpl_warn"))
        print_message.assert_any_call("🔀 [provider] provider=openclaw")
        for warning in warnings:
            print_message.assert_any_call(f"⚠️ [logcheck] run=chatcmpl_warn: {warning}")


class OpenClawClientControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OpenClawClient(
            agent="main",
            base_url="http://127.0.0.1:18789/v1",
            model="openclaw/default",
        )
        # Uppercase Google IDs are percent-encoded into an already-lowercase
        # key, so OpenClaw's canonicalization must leave this value unchanged.
        self.session_key = "agent:main:gchat:%41%41q:%5az"

    def test_create_session_returns_the_verified_session_key(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "key": self.session_key,
                }
            ),
            stderr="",
        )

        with patch(
            "helpers.providers.openclaw_client.create_session_cli",
            return_value=response,
        ) as create_session:
            created_key = self.client.create_session(
                self.session_key,
                " minimax/MiniMax-M3 ",
            )

        self.assertEqual(created_key, self.session_key)
        create_session.assert_called_once_with(
            self.session_key,
            "main",
            "minimax/MiniMax-M3",
        )

    def test_create_session_rejects_a_mismatched_returned_key(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout='{"ok":true,"key":"agent:main:gchat:different"}',
            stderr="",
        )

        with (
            patch(
                "helpers.providers.openclaw_client.create_session_cli",
                return_value=response,
            ),
            self.assertRaisesRegex(RuntimeError, "session key"),
        ):
            self.client.create_session(self.session_key, "provider/model")

    def test_create_session_surfaces_nonzero_cli_details(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            2,
            stdout="",
            stderr="gateway   rejected\nmodel",
        )

        with (
            patch(
                "helpers.providers.openclaw_client.create_session_cli",
                return_value=response,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "คืนค่ารหัส 2: gateway rejected model",
            ),
        ):
            self.client.create_session(self.session_key, "provider/model")

    def test_create_session_rejects_invalid_json(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout="not json",
            stderr="",
        )

        with (
            patch(
                "helpers.providers.openclaw_client.create_session_cli",
                return_value=response,
            ),
            self.assertRaisesRegex(RuntimeError, "JSON"),
        ):
            self.client.create_session(self.session_key, "provider/model")

    def test_create_session_rejects_ok_false(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"ok": False, "key": self.session_key}),
            stderr="",
        )

        with (
            patch(
                "helpers.providers.openclaw_client.create_session_cli",
                return_value=response,
            ),
            self.assertRaisesRegex(RuntimeError, "สร้าง session ไม่สำเร็จ"),
        ):
            self.client.create_session(self.session_key, "provider/model")

    def test_reset_session_returns_the_verified_same_key(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "key": self.session_key,
                        "entry": {"sessionId": "fresh-session-id"},
                    },
                }
            ),
            stderr="",
        )

        with patch(
            "helpers.providers.openclaw_client.reset_session_cli",
            return_value=response,
        ) as reset_session:
            reset_key = self.client.reset_session(self.session_key)

        self.assertEqual(reset_key, self.session_key)
        reset_session.assert_called_once_with(self.session_key)

    def test_reset_session_surfaces_nonzero_cli_details(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            3,
            stdout="",
            stderr="session does not exist",
        )

        with (
            patch(
                "helpers.providers.openclaw_client.reset_session_cli",
                return_value=response,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "คืนค่ารหัส 3: session does not exist",
            ),
        ):
            self.client.reset_session(self.session_key)

    def test_reset_session_rejects_invalid_or_mismatched_response(self) -> None:
        responses = (
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"ok": True, "key": "agent:main:gchat:different"}),
                stderr="",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"ok": False, "key": self.session_key}),
                stderr="",
            ),
        )
        for response in responses:
            with (
                self.subTest(response=response.stdout),
                patch(
                    "helpers.providers.openclaw_client.reset_session_cli",
                    return_value=response,
                ),
                self.assertRaises((RuntimeError, TypeError)),
            ):
                self.client.reset_session(self.session_key)

    def test_has_session_matches_only_the_exact_key(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "sessions": [
                        {"key": "agent:main:gchat:other"},
                        {"key": self.session_key},
                    ]
                }
            ),
            stderr="",
        )

        with patch(
            "helpers.providers.openclaw_client.list_sessions_cli",
            return_value=response,
        ):
            self.assertTrue(self.client.has_session(self.session_key))
            self.assertFalse(self.client.has_session("agent:main:gchat:missing"))

    def test_has_session_rejects_duplicate_exact_keys(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "sessions": [
                        {"key": self.session_key},
                        {"key": self.session_key},
                    ]
                }
            ),
            stderr="",
        )

        with (
            patch(
                "helpers.providers.openclaw_client.list_sessions_cli",
                return_value=response,
            ),
            self.assertRaisesRegex(TypeError, "ซ้ำ"),
        ):
            self.client.has_session(self.session_key)

    def test_abort_session_reports_success(self) -> None:
        response = subprocess.CompletedProcess([], 0, stdout='{"ok":true}', stderr="")

        with patch(
            "helpers.providers.openclaw_client.abort_session_cli",
            return_value=response,
        ) as abort_session:
            result = self.client.abort_session(self.session_key)

        self.assertEqual(result, AbortResult(ok=True, reason=""))
        abort_session.assert_called_once_with(self.session_key)

    def test_abort_session_prefers_stderr_as_the_failure_reason(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            3,
            stdout="fallback stdout",
            stderr="permission denied",
        )

        with patch(
            "helpers.providers.openclaw_client.abort_session_cli",
            return_value=response,
        ):
            result = self.client.abort_session(self.session_key)

        self.assertEqual(
            result,
            AbortResult(ok=False, reason="permission denied"),
        )

    def test_abort_is_not_blocked_by_an_inflight_http_turn(self) -> None:
        send_started = threading.Event()
        release_send = threading.Event()
        abort_finished = threading.Event()
        send_errors: list[RuntimeError] = []

        def blocked_send(*_args: object) -> dict[str, str]:
            send_started.set()
            if not release_send.wait(timeout=2):
                raise TimeoutError("test did not release the HTTP turn")
            return {"text": "", "run_id": "chatcmpl_blocked"}

        def run_send() -> None:
            try:
                self.client.send_turn("hello", "Alice", [], self.session_key)
            except RuntimeError as error:  # pragma: no cover - assertion below
                send_errors.append(error)

        def run_abort() -> None:
            self.client.abort_session(self.session_key)
            abort_finished.set()

        response = subprocess.CompletedProcess([], 0, stdout='{"ok":true}', stderr="")
        with (
            patch(
                "helpers.providers.openclaw_client.ask_openclaw_direct",
                side_effect=blocked_send,
            ),
            patch(
                "helpers.providers.openclaw_client.check_run_errors",
                return_value=[],
            ),
            patch(
                "helpers.providers.openclaw_client.abort_session_cli",
                return_value=response,
            ),
        ):
            send_thread = threading.Thread(target=run_send)
            abort_thread = threading.Thread(target=run_abort)
            send_thread.start()
            self.assertTrue(send_started.wait(timeout=1))
            abort_thread.start()
            try:
                self.assertTrue(abort_finished.wait(timeout=1))
            finally:
                release_send.set()
                send_thread.join(timeout=2)
                abort_thread.join(timeout=2)

        self.assertFalse(send_thread.is_alive())
        self.assertFalse(abort_thread.is_alive())
        self.assertEqual(send_errors, [])

    def test_list_models_returns_only_the_catalog_entries(self) -> None:
        models = [{"key": "provider/model", "available": True}]
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"count": 1, "models": models}),
            stderr="",
        )

        with patch(
            "helpers.providers.openclaw_client.list_models_cli",
            return_value=response,
        ) as list_models:
            result = self.client.list_models()

        self.assertEqual(result, models)
        list_models.assert_called_once_with()

    def test_list_models_rejects_invalid_top_level_payloads(self) -> None:
        invalid_payloads = ([], {"models": {}}, {"count": 0})

        for payload in invalid_payloads:
            with (
                self.subTest(payload=payload),
                patch(
                    "helpers.providers.openclaw_client.list_models_cli",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(payload),
                        stderr="",
                    ),
                ),
                self.assertRaisesRegex(TypeError, "รูปแบบข้อมูล"),
            ):
                self.client.list_models()


class ProviderSettingsTests(unittest.TestCase):
    def test_provider_settings_use_openclaw_defaults(self) -> None:
        settings = ProviderSettings.from_env()

        self.assertEqual(settings.openclaw_agent, "main")
        self.assertEqual(settings.openclaw_base_url, "http://127.0.0.1:18789/v1")
        self.assertEqual(settings.openclaw_model, "openclaw/default")
        self.assertFalse(hasattr(settings, "openclaw_session_key"))


if __name__ == "__main__":
    unittest.main()
