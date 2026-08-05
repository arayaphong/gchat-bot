from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from helpers.message_orchestrator import MessageOrchestrator
from helpers.providers import ProviderSettings
from helpers.providers.model_selection import ModelSelection


class NewSessionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current_settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:c0ffee",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )
        self.new_settings = ProviderSettings(
            openclaw_agent="main",
            openclaw_session_key="agent:main:gchat:decade",
            openclaw_base_url="http://127.0.0.1:18789/v1",
            openclaw_model="openclaw/default",
        )
        self.gateway = Mock()
        self.session_manager = Mock()
        self.session_manager.settings = self.current_settings
        self.session_manager.abort_current.return_value = (True, "aborted")
        self.session_manager.rotate_with_model.return_value = self.new_settings
        self.orchestrator = MessageOrchestrator(
            gateway=self.gateway,
            session_manager=self.session_manager,
            attachment_service=Mock(),
        )

    def test_new_creates_the_session_with_the_current_session_model(self) -> None:
        selection = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        with patch(
            "helpers.message_orchestrator.get_model_selection",
            return_value=selection,
        ) as get_selection:
            self.orchestrator._handle_new_session("spaces/one", "threads/two")

        get_selection.assert_called_once_with("agent:main:gchat:c0ffee")
        self.session_manager.abort_current.assert_called_once_with(
            "spaces/one", "threads/two"
        )
        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/current"
        )
        self.session_manager.rotate.assert_not_called()
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn("provider/current", message)
        self.assertEqual(self.gateway.send_followup.call_args.args[3], "jinx_system")

    def test_new_uses_the_effective_default_when_session_has_no_override(self) -> None:
        selection = ModelSelection(
            default_model="provider/default",
            session_model=None,
        )

        with patch(
            "helpers.message_orchestrator.get_model_selection",
            return_value=selection,
        ):
            self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/default"
        )

    def test_model_lookup_failure_does_not_abort_or_rotate_the_session(self) -> None:
        with patch(
            "helpers.message_orchestrator.get_model_selection",
            side_effect=subprocess.TimeoutExpired(["openclaw"], 15),
        ):
            self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.session_manager.abort_current.assert_not_called()
        self.session_manager.rotate_with_model.assert_not_called()
        self.session_manager.rotate.assert_not_called()
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn("ไม่สามารถเริ่มเซสชั่นใหม่", message)
        self.assertIn("เซสชั่นเดิมยังคงใช้งานอยู่", message)

    def test_create_failure_never_falls_back_to_an_unmodeled_rotation(self) -> None:
        self.session_manager.rotate_with_model.side_effect = RuntimeError(
            "gateway rejected *model*"
        )
        selection = ModelSelection(
            default_model="provider/default",
            session_model="provider/current",
        )

        with patch(
            "helpers.message_orchestrator.get_model_selection",
            return_value=selection,
        ):
            self.orchestrator._handle_new_session("spaces/one", "threads/two")

        self.session_manager.abort_current.assert_called_once()
        self.session_manager.rotate_with_model.assert_called_once_with(
            "provider/current"
        )
        self.session_manager.rotate.assert_not_called()
        message = self.gateway.send_followup.call_args.args[2]
        self.assertIn(r"gateway rejected \*model\*", message)
        self.assertIn("เซสชั่นเดิมยังคงใช้งานอยู่", message)


if __name__ == "__main__":
    unittest.main()
