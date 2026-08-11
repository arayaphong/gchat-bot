from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from helpers.chat_clear_store import ChatClearStore
from helpers.chat_history_operations import (
    backup_history_store,
    restore_history_store,
    run_history_preflight,
    worker_lease_ready,
)
from helpers.chat_history_settings import ChatHistorySettings
from helpers.services import CredentialReauthorizationRequiredError

UTC = timezone.utc


class ChatHistoryOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ChatClearStore(self.root / "state")
        self.settings = ChatHistorySettings(
            enabled=True,
            delete_enabled=False,
            allowed_user="users/allowed-user",
            allowed_space="spaces/allowed-dm",
            card_action_url=None,
            state_dir=self.root / "state",
        )

    def test_local_preflight_checks_state_without_touching_credentials(self) -> None:
        credentials = Mock()
        client = Mock()

        result = run_history_preflight(
            self.settings,
            configuration_error=None,
            store=self.store,
            credential_service=credentials,
            client=client,
            remote=False,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.checks,
            ("configuration", "timezone", "state", "schema"),
        )
        credentials.get_user_creds.assert_not_called()
        client.preflight_access.assert_not_called()

    def test_remote_preflight_checks_granted_scopes_and_dm_access(self) -> None:
        credentials = Mock()
        client = Mock()

        result = run_history_preflight(
            self.settings,
            configuration_error=None,
            store=self.store,
            credential_service=credentials,
            client=client,
            remote=True,
        )

        self.assertTrue(result.ok)
        self.assertIn("user_scopes", result.checks)
        self.assertIn("allowed_dm", result.checks)
        credentials.get_user_creds.assert_called_once_with()
        client.preflight_access.assert_called_once_with()
        credentials.get_bot_creds.assert_not_called()

    def test_preflight_returns_only_sanitized_credential_category(self) -> None:
        credentials = Mock()
        credentials.get_user_creds.side_effect = (
            CredentialReauthorizationRequiredError()
        )

        result = run_history_preflight(
            self.settings,
            configuration_error=None,
            store=self.store,
            credential_service=credentials,
            client=Mock(),
            remote=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "credential_reauthorize_required")
        self.assertNotIn("users/allowed-user", repr(result.as_dict()))
        self.assertNotIn("spaces/allowed-dm", repr(result.as_dict()))

    def test_worker_readiness_requires_matching_fresh_durable_heartbeat(self) -> None:
        now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        self.store.preflight()
        self.store.record_worker_heartbeat("worker-one", now=now)
        worker = SimpleNamespace(owner_id="worker-one", is_active=True)

        self.assertTrue(worker_lease_ready(self.store, worker, now=now))
        self.assertFalse(
            worker_lease_ready(
                self.store,
                worker,
                now=now + timedelta(seconds=21),
            )
        )
        self.assertFalse(
            worker_lease_ready(
                self.store,
                SimpleNamespace(owner_id="worker-two", is_active=True),
                now=now,
            )
        )

    def test_online_backup_and_offline_restore_round_trip_worker_state(self) -> None:
        now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        self.store.preflight()
        self.store.record_worker_heartbeat("worker-before", now=now)
        backup_path = self.root / "backups" / "history.sqlite3"

        backup = backup_history_store(self.store, backup_path)
        self.store.record_worker_heartbeat(
            "worker-after", now=now + timedelta(minutes=1)
        )
        restored = restore_history_store(
            self.store,
            backup.path,
            service_stopped=True,
        )

        self.assertEqual(self.store.diagnostics().worker_owner, "worker-before")
        self.assertTrue(restored.safety_backup_path.is_file())
        self.assertEqual(restored.safety_backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(restored.safety_backup_path) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_restore_requires_explicit_service_stopped_acknowledgement(self) -> None:
        self.store.preflight()
        backup = backup_history_store(self.store, self.root / "backup.sqlite3")

        with self.assertRaises(ValueError):
            restore_history_store(
                self.store,
                backup.path,
                service_stopped=False,
            )
