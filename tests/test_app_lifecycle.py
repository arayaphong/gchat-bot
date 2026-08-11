from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers.chat_history_operations import HistoryPreflightResult
from helpers.chat_history_settings import ChatHistorySettings

with patch("pathlib.Path.mkdir"):
    import app as app_module


class AppLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module.app.config.update(TESTING=True)

    def test_runtime_start_is_idempotent_and_orders_preflight_before_workers(
        self,
    ) -> None:
        events: list[str] = []
        result = HistoryPreflightResult(
            ok=True,
            enabled=False,
            delete_enabled=False,
            checks=("configuration",),
        )
        with (
            patch.object(app_module, "_runtime_started", False),
            patch.object(app_module, "_runtime_start_error_code", "not_started"),
            patch.object(
                app_module,
                "_refresh_history_preflight",
                side_effect=lambda **_kwargs: events.append("preflight") or result,
            ) as preflight,
            patch.object(
                app_module,
                "_start_outbound_attachment_service",
                side_effect=lambda: events.append("outbound") or True,
            ) as outbound,
            patch.object(
                app_module,
                "_start_chat_history_worker",
                side_effect=lambda: events.append("history") or True,
            ) as history,
        ):
            self.assertTrue(app_module.start_runtime_services())
            self.assertTrue(app_module.start_runtime_services())

        self.assertEqual(events, ["preflight", "outbound", "history"])
        preflight.assert_called_once_with(remote=False)
        outbound.assert_called_once_with()
        history.assert_called_once_with()

    def test_runtime_fails_closed_before_watchers_when_preflight_fails(self) -> None:
        result = HistoryPreflightResult(
            ok=False,
            enabled=True,
            delete_enabled=True,
            checks=("configuration",),
            error_code="state_unavailable",
        )
        with (
            patch.object(app_module, "_runtime_started", False),
            patch.object(
                app_module, "_refresh_history_preflight", return_value=result
            ),
            patch.object(app_module, "_start_outbound_attachment_service") as outbound,
            patch.object(app_module, "_start_chat_history_worker") as history,
        ):
            self.assertFalse(app_module.start_runtime_services())

        outbound.assert_not_called()
        history.assert_not_called()

    def test_preflight_fails_closed_when_enabled_components_were_not_built(
        self,
    ) -> None:
        result = HistoryPreflightResult(
            ok=True,
            enabled=True,
            delete_enabled=False,
            checks=("configuration", "state"),
        )
        with (
            patch.object(app_module, "run_history_preflight", return_value=result),
            patch.object(app_module, "history_client", None),
            patch.object(app_module, "history_service", None),
            patch.object(app_module, "history_worker", None),
        ):
            checked = app_module._refresh_history_preflight(remote=False)

        self.assertFalse(checked.ok)
        self.assertEqual(checked.error_code, "history_components_unavailable")

    def test_readiness_is_sanitized_and_separate_from_liveness(self) -> None:
        disabled = ChatHistorySettings(
            enabled=False,
            delete_enabled=False,
            allowed_user=None,
            allowed_space=None,
            card_action_url=None,
            state_dir=app_module.history_state_dir,
        )
        result = HistoryPreflightResult(
            ok=True,
            enabled=False,
            delete_enabled=False,
            checks=("configuration",),
        )
        with (
            patch.object(app_module, "history_settings", disabled),
            patch.object(app_module, "_runtime_started", True),
            patch.object(app_module, "_runtime_start_error_code", None),
            patch.object(app_module, "_history_preflight_result", result),
            patch.object(
                app_module,
                "auth_settings",
                SimpleNamespace(audiences={"configured"}),
            ),
            patch.object(
                app_module,
                "outbound_attachment_service",
                SimpleNamespace(is_active=True),
            ),
            patch.object(app_module, "_release_sha", return_value="a" * 40),
        ):
            client = app_module.app.test_client()
            live = client.get("/")
            ready = client.get("/readyz")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        payload = ready.get_json()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["release_sha"], "a" * 40)
        self.assertNotIn("users/", repr(payload))
        self.assertNotIn("spaces/", repr(payload))

    def test_enabled_history_readiness_requires_owned_fresh_worker_lease(self) -> None:
        enabled = ChatHistorySettings(
            enabled=True,
            delete_enabled=False,
            allowed_user="users/allowed-user",
            allowed_space="spaces/allowed-dm",
            card_action_url=None,
            state_dir=app_module.history_state_dir,
        )
        result = HistoryPreflightResult(
            ok=True,
            enabled=True,
            delete_enabled=False,
            checks=("configuration", "timezone", "state", "schema"),
        )
        with (
            patch.object(app_module, "history_settings", enabled),
            patch.object(app_module, "_runtime_started", True),
            patch.object(app_module, "_runtime_start_error_code", None),
            patch.object(app_module, "_history_preflight_result", result),
            patch.object(
                app_module,
                "auth_settings",
                SimpleNamespace(audiences={"configured"}),
            ),
            patch.object(
                app_module,
                "outbound_attachment_service",
                SimpleNamespace(is_active=True),
            ),
            patch.object(app_module, "worker_lease_ready", return_value=False),
        ):
            response = app_module.app.test_client().get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["history"]["worker_lease"])
