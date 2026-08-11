from __future__ import annotations

import ast
import json
import multiprocessing
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from google.auth.exceptions import RefreshError

from helpers.credential_storage import (
    credential_file_lock,
    credential_lock_path,
    locked_atomic_write_secret,
)
from helpers.google_scopes import (
    BOT_SCOPES,
    CHAT_BOT_SCOPE,
    CHAT_MESSAGES_SCOPE,
    DRIVE_FILE_SCOPE,
    DRIVE_READONLY_SCOPE,
    DRIVE_SCOPES,
    USER_SCOPES,
)
from helpers.services.chat_services import (
    CredentialFileInvalidError,
    CredentialFileMissingError,
    CredentialMissingGrantedScopesError,
    CredentialReauthorizationRequiredError,
    CredentialRefreshError,
    CredentialService,
    CredentialStorageError,
)
from helpers.token_tools.token_writer import TokenScopeGrantError, save_user_token


def _hold_process_credential_lock(
    target: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with credential_file_lock(target):
        entered.set()
        release.wait(timeout=5)


def _token_info(scopes: tuple[str, ...] = USER_SCOPES) -> dict[str, object]:
    return {
        "token": "synthetic-access-value",
        "refresh_token": "synthetic-refresh-value",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "synthetic-client-id",
        "client_secret": "synthetic-client-value",
        "scopes": list(scopes),
        "expiry": "2999-01-01T00:00:00Z",
    }


class GoogleScopeTests(unittest.TestCase):
    def test_canonical_scope_partitions_are_exact_and_immutable(self) -> None:
        self.assertEqual(
            DRIVE_SCOPES,
            (DRIVE_READONLY_SCOPE, DRIVE_FILE_SCOPE),
        )
        self.assertEqual(USER_SCOPES, (*DRIVE_SCOPES, CHAT_MESSAGES_SCOPE))
        self.assertEqual(BOT_SCOPES, (CHAT_BOT_SCOPE,))
        self.assertIsInstance(USER_SCOPES, tuple)
        self.assertNotIn(CHAT_BOT_SCOPE, USER_SCOPES)
        self.assertNotIn(CHAT_MESSAGES_SCOPE, BOT_SCOPES)

    def test_token_helpers_reference_the_canonical_scope_tuple(self) -> None:
        from helpers.token_tools import get_token, get_token_manual

        self.assertIs(get_token.USER_SCOPES, USER_SCOPES)
        self.assertIs(get_token_manual.USER_SCOPES, USER_SCOPES)

    def test_token_helpers_support_direct_script_import_resolution(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        helper_dir = project_root / "helpers" / "token_tools"
        probe = (
            "import runpy,sys; "
            "sys.path=[sys.argv[2]]+"
            "[p for p in sys.path if p not in ('', sys.argv[3])]; "
            "runpy.run_path(sys.argv[1], run_name='credential_helper_probe')"
        )

        for filename in ("get_token.py", "get_token_manual.py", "manual_token.py"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    probe,
                    str(helper_dir / filename),
                    str(helper_dir),
                    str(project_root),
                ],
                cwd=project_root.parent,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_wires_only_canonical_scope_constants(self) -> None:
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "helpers.google_scopes"
            for alias in node.names
        }
        google_scope_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("https://www.googleapis.com/auth/")
        }
        credential_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CredentialService"
        ]

        self.assertEqual(imported_names, {"BOT_SCOPES", "USER_SCOPES"})
        self.assertEqual(google_scope_literals, set())
        self.assertEqual(len(credential_calls), 1)
        keyword_names = {
            keyword.arg: keyword.value.id
            for keyword in credential_calls[0].keywords
            if keyword.arg in {"scopes_bot", "scopes_user"}
            and isinstance(keyword.value, ast.Name)
        }
        self.assertEqual(
            keyword_names,
            {"scopes_bot": "BOT_SCOPES", "scopes_user": "USER_SCOPES"},
        )

    def test_tracked_token_helpers_have_no_embedded_authorization_code(self) -> None:
        helper_dir = Path(__file__).resolve().parents[1] / "helpers" / "token_tools"
        assignment = re.compile(
            r"(?:authorization_)?code\s*=\s*['\"]4/",
            flags=re.IGNORECASE,
        )

        for source_file in helper_dir.glob("*.py"):
            self.assertIsNone(
                assignment.search(source_file.read_text(encoding="utf-8")),
                source_file,
            )

    def test_token_writer_refuses_partial_grant_without_changing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token.json"
            token_file.write_text("unchanged", encoding="utf-8")
            credentials = Mock(
                granted_scopes=DRIVE_SCOPES,
                scopes=USER_SCOPES,
            )

            with self.assertRaises(TokenScopeGrantError) as caught:
                save_user_token(credentials, token_file)

            self.assertEqual(caught.exception.missing_scopes, (CHAT_MESSAGES_SCOPE,))
            self.assertEqual(token_file.read_text(encoding="utf-8"), "unchanged")
            credentials.to_json.assert_not_called()

    def test_token_writer_persists_verified_grants_with_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token.json"
            credentials = Mock(
                granted_scopes=USER_SCOPES,
                scopes=USER_SCOPES,
            )
            credentials.to_json.return_value = json.dumps(_token_info())

            save_user_token(credentials, token_file)

            persisted = json.loads(token_file.read_text(encoding="utf-8"))
            self.assertEqual(tuple(persisted["scopes"]), USER_SCOPES)
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)


class CredentialStorageTests(unittest.TestCase):
    def test_locked_atomic_write_replaces_file_with_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "token.json"
            target.write_text("old", encoding="utf-8")
            target.chmod(0o644)

            locked_atomic_write_secret(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            lock_path = credential_lock_path(target)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(
                list(Path(directory).glob(f".{target.name}.*.tmp")),
                [],
            )

    def test_file_lock_serializes_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "token.json"
            target.touch()
            active = 0
            maximum_active = 0
            state_lock = threading.Lock()

            def enter_lock() -> None:
                nonlocal active, maximum_active
                with credential_file_lock(target):
                    with state_lock:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    time.sleep(0.03)
                    with state_lock:
                        active -= 1

            workers = [threading.Thread(target=enter_lock) for _ in range(3)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)

            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(maximum_active, 1)

    def test_file_lock_serializes_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "token.json"
            target.touch()
            context = multiprocessing.get_context("spawn")
            first_entered = context.Event()
            release_first = context.Event()
            second_entered = context.Event()
            release_second = context.Event()
            first = context.Process(
                target=_hold_process_credential_lock,
                args=(str(target), first_entered, release_first),
            )
            second = context.Process(
                target=_hold_process_credential_lock,
                args=(str(target), second_entered, release_second),
            )
            first.start()
            try:
                self.assertTrue(first_entered.wait(timeout=2))
                second.start()
                self.assertFalse(second_entered.wait(timeout=0.15))

                release_first.set()
                self.assertTrue(second_entered.wait(timeout=2))
            finally:
                release_first.set()
                release_second.set()
                first.join(timeout=2)
                if second.pid is not None:
                    second.join(timeout=2)
                if first.is_alive():
                    first.terminate()
                    first.join(timeout=2)
                if second.pid is not None and second.is_alive():
                    second.terminate()
                    second.join(timeout=2)

            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)


class CredentialServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.directory = Path(temporary_directory.name)
        self.token_file = self.directory / "token.json"
        self.bot_file = self.directory / "credentials.json"
        self.service = CredentialService(
            bot_cred=self.bot_file,
            token_file=self.token_file,
        )

    def write_token(self, info: dict[str, object]) -> None:
        self.token_file.write_text(json.dumps(info), encoding="utf-8")

    def test_missing_user_token_has_distinct_safe_error(self) -> None:
        with self.assertRaises(CredentialFileMissingError) as caught:
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "missing_file")
        self.assertEqual(caught.exception.credential_kind, "user")

    def test_invalid_user_token_has_distinct_safe_error(self) -> None:
        self.token_file.write_text("not-json", encoding="utf-8")

        with self.assertRaises(CredentialFileInvalidError) as caught:
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "invalid_token")
        self.assertNotIn("not-json", str(caught.exception))

    def test_missing_granted_scope_fails_closed(self) -> None:
        self.write_token(_token_info(DRIVE_SCOPES))

        with self.assertRaises(CredentialMissingGrantedScopesError) as caught:
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "missing_granted_scope")
        self.assertEqual(caught.exception.missing_scopes, (CHAT_MESSAGES_SCOPE,))
        self.assertIn("reauthorization", str(caught.exception))

    def test_missing_scope_metadata_requires_reauthorization(self) -> None:
        token_info = _token_info()
        token_info.pop("scopes")
        self.write_token(token_info)

        with self.assertRaises(CredentialReauthorizationRequiredError) as caught:
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "reauthorize_required")

    def test_valid_token_is_returned_without_refresh_or_rewrite(self) -> None:
        self.write_token(_token_info())
        original = self.token_file.read_bytes()

        creds = self.service.get_user_creds()

        self.assertTrue(creds.valid)
        self.assertTrue(creds.has_scopes(USER_SCOPES))
        self.assertEqual(self.token_file.read_bytes(), original)

    def test_space_delimited_scope_metadata_remains_compatible(self) -> None:
        token_info = _token_info()
        token_info["scopes"] = " ".join(USER_SCOPES)
        self.write_token(token_info)

        creds = self.service.get_user_creds()

        self.assertTrue(creds.has_scopes(USER_SCOPES))

    def test_valid_existing_token_permissions_are_tightened(self) -> None:
        self.write_token(_token_info())
        self.token_file.chmod(0o644)

        self.service.get_user_creds()

        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)

    def test_token_read_failure_is_sanitized_as_storage_unavailable(self) -> None:
        self.write_token(_token_info())

        with (
            patch.object(
                Path,
                "read_text",
                side_effect=PermissionError("SENTINEL_PRIVATE_PATH"),
            ),
            self.assertRaises(CredentialStorageError) as caught,
        ):
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "storage_failure")
        self.assertNotIn("SENTINEL_PRIVATE_PATH", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_refresh_failure_is_sanitized(self) -> None:
        self.write_token(_token_info())
        creds = Mock(
            valid=False,
            expired=True,
            refresh_token="synthetic-refresh-value",
        )
        creds.refresh.side_effect = RuntimeError("SENTINEL_PRIVATE_RESPONSE")

        with (
            patch(
                "helpers.services.chat_services.UserCreds.from_authorized_user_info",
                return_value=creds,
            ),
            self.assertRaises(CredentialRefreshError) as caught,
        ):
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "refresh_failure")
        self.assertNotIn("SENTINEL_PRIVATE_RESPONSE", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_revoked_refresh_token_requires_reauthorization(self) -> None:
        self.write_token(_token_info())
        creds = Mock(
            valid=False,
            expired=True,
            refresh_token="synthetic-refresh-value",
        )
        creds.refresh.side_effect = RefreshError(
            "invalid_grant: SENTINEL_PRIVATE_RESPONSE",
            {
                "error": "invalid_grant",
                "error_description": "SENTINEL_PRIVATE_RESPONSE",
            },
        )

        with (
            patch(
                "helpers.services.chat_services.UserCreds.from_authorized_user_info",
                return_value=creds,
            ),
            self.assertRaises(CredentialReauthorizationRequiredError) as caught,
        ):
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "reauthorize_required")
        self.assertNotIn("SENTINEL_PRIVATE_RESPONSE", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_refresh_serialization_failure_is_sanitized(self) -> None:
        self.write_token(_token_info())
        creds = Mock(
            valid=False,
            expired=True,
            refresh_token="synthetic-refresh-value",
            granted_scopes=USER_SCOPES,
        )

        def refresh(_request: object) -> None:
            creds.valid = True

        creds.refresh.side_effect = refresh
        creds.to_json.side_effect = ValueError("SENTINEL_PRIVATE_TOKEN")

        with (
            patch(
                "helpers.services.chat_services.UserCreds.from_authorized_user_info",
                return_value=creds,
            ),
            self.assertRaises(CredentialStorageError) as caught,
        ):
            self.service.get_user_creds()

        self.assertEqual(caught.exception.code, "storage_failure")
        self.assertNotIn("SENTINEL_PRIVATE_TOKEN", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_refresh_response_with_partial_grant_fails_closed(self) -> None:
        self.write_token(_token_info())
        original = self.token_file.read_bytes()
        creds = Mock(
            valid=False,
            expired=True,
            refresh_token="synthetic-refresh-value",
            granted_scopes=DRIVE_SCOPES,
        )

        def refresh(_request: object) -> None:
            creds.valid = True

        creds.refresh.side_effect = refresh

        with (
            patch(
                "helpers.services.chat_services.UserCreds.from_authorized_user_info",
                return_value=creds,
            ),
            self.assertRaises(CredentialMissingGrantedScopesError) as caught,
        ):
            self.service.get_user_creds()

        self.assertEqual(caught.exception.missing_scopes, (CHAT_MESSAGES_SCOPE,))
        self.assertEqual(self.token_file.read_bytes(), original)
        creds.to_json.assert_not_called()

    def test_successful_refresh_is_persisted_with_mode_0600(self) -> None:
        token_info = _token_info()
        token_info["expiry"] = "2000-01-01T00:00:00Z"
        self.write_token(token_info)
        self.token_file.chmod(0o644)

        refreshed_info = _token_info()
        creds = Mock(
            valid=False,
            expired=True,
            refresh_token="synthetic-refresh-value",
            granted_scopes=USER_SCOPES,
        )

        def refresh(_request: object) -> None:
            creds.valid = True

        creds.refresh.side_effect = refresh
        creds.to_json.return_value = json.dumps(refreshed_info)

        with patch(
            "helpers.services.chat_services.UserCreds.from_authorized_user_info",
            return_value=creds,
        ):
            self.assertIs(self.service.get_user_creds(), creds)

        creds.refresh.assert_called_once()
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.token_file.read_text()), refreshed_info)

    def test_bot_loader_distinguishes_invalid_file_and_refresh_failure(self) -> None:
        self.bot_file.write_text("{}", encoding="utf-8")

        with (
            patch(
                "helpers.services.chat_services.service_account.Credentials.from_service_account_file",
                side_effect=ValueError("SENTINEL_PRIVATE_KEY"),
            ),
            self.assertRaises(CredentialFileInvalidError) as invalid,
        ):
            self.service.get_bot_creds()
        self.assertNotIn("SENTINEL_PRIVATE_KEY", str(invalid.exception))

        bot_creds = Mock()
        bot_creds.refresh.side_effect = RuntimeError("SENTINEL_REMOTE_BODY")
        with (
            patch(
                "helpers.services.chat_services.service_account.Credentials.from_service_account_file",
                return_value=bot_creds,
            ),
            self.assertRaises(CredentialRefreshError) as refresh,
        ):
            self.service.get_bot_creds()
        self.assertNotIn("SENTINEL_REMOTE_BODY", str(refresh.exception))


if __name__ == "__main__":
    unittest.main()
