"""Sanitized preflight, queue diagnostics, checkpoint, and online backup CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from helpers.chat_clear_store import ChatClearStore, ChatClearStoreError
from helpers.chat_history_client import ChatHistoryClient
from helpers.chat_history_operations import (
    backup_history_store,
    restore_history_store,
    run_history_preflight,
)
from helpers.chat_history_settings import (
    ChatHistorySettings,
    ChatHistorySettingsError,
    default_chat_history_state_dir,
)
from helpers.services import CredentialService

_UTC = timezone.utc


def _components() -> tuple[
    ChatHistorySettings | None,
    ChatHistorySettingsError | None,
    ChatClearStore,
    CredentialService,
    ChatHistoryClient | None,
]:
    configuration_error: ChatHistorySettingsError | None = None
    try:
        settings: ChatHistorySettings | None = ChatHistorySettings.from_env()
    except ChatHistorySettingsError as error:
        settings = None
        configuration_error = error
    state_dir = settings.state_dir if settings is not None else default_chat_history_state_dir()
    store = ChatClearStore(state_dir)
    credentials = CredentialService(
        bot_cred=Path(
            os.environ.get("GCHAT_BOT_CRED", str(PROJECT_DIR / "credentials.json"))
        ),
        token_file=Path(
            os.environ.get("GCHAT_TOKEN_FILE", str(PROJECT_DIR / "token.json"))
        ),
    )
    client = None
    if (
        settings is not None
        and settings.enabled
        and settings.allowed_user is not None
        and settings.allowed_space is not None
    ):
        client = ChatHistoryClient(
            credentials,
            allowed_user=settings.allowed_user,
            allowed_space=settings.allowed_space,
        )
    return settings, configuration_error, store, credentials, client


def _heartbeat_fresh(value: str | None, *, max_age_seconds: float = 20.0) -> bool:
    if value is None:
        return False
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (datetime.now(tz=_UTC) - heartbeat).total_seconds()
    return 0 <= age <= max_age_seconds


def _preflight(args: argparse.Namespace) -> int:
    settings, error, store, credentials, client = _components()
    result = run_history_preflight(
        settings,
        configuration_error=error,
        store=store,
        credential_service=credentials,
        client=client,
        remote=args.remote,
    )
    payload = result.as_dict()
    if result.ok and result.enabled and args.require_worker:
        try:
            diagnostics = store.diagnostics()
            worker_ok = bool(
                diagnostics.worker_owner
                and _heartbeat_fresh(diagnostics.worker_heartbeat_at)
            )
        except ChatClearStoreError:
            worker_ok = False
        payload["worker_lease"] = worker_ok
        if not worker_ok:
            payload["ok"] = False
            payload["error_code"] = "worker_lease_unavailable"
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


def _diagnostics(_args: argparse.Namespace) -> int:
    settings, error, store, credentials, client = _components()
    result = run_history_preflight(
        settings,
        configuration_error=error,
        store=store,
        credential_service=credentials,
        client=client,
        remote=False,
    )
    if not result.ok:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 1
    if not result.enabled:
        print(json.dumps({"ok": True, "enabled": False}, sort_keys=True))
        return 0
    diagnostics = store.diagnostics()
    payload = {
        "ok": True,
        "enabled": True,
        "job_counts": dict(diagnostics.job_counts),
        "item_counts": dict(diagnostics.item_counts),
        "oldest_active_age_seconds": diagnostics.oldest_active_age_seconds,
        "worker_lease": bool(
            diagnostics.worker_owner
            and _heartbeat_fresh(diagnostics.worker_heartbeat_at)
        ),
        "partition_failure_counts": dict(diagnostics.partition_failure_counts),
        "pending_final_notifications": diagnostics.pending_final_notifications,
        "wal_bytes": diagnostics.wal_bytes,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _checkpoint(args: argparse.Namespace) -> int:
    _settings, _error, store, _credentials, _client = _components()
    busy, log_frames, checkpointed = store.checkpoint(args.mode)
    print(
        json.dumps(
            {
                "ok": busy == 0,
                "busy": busy,
                "log_frames": log_frames,
                "checkpointed_frames": checkpointed,
            },
            sort_keys=True,
        )
    )
    return 0 if busy == 0 else 1


def _backup(args: argparse.Namespace) -> int:
    _settings, _error, store, _credentials, _client = _components()
    result = backup_history_store(store, args.output)
    print(
        json.dumps(
            {
                "ok": True,
                "backup_file": result.path.name,
                "checkpoint_busy": result.checkpoint_busy,
                "checkpoint_log_frames": result.checkpoint_log_frames,
                "checkpointed_frames": result.checkpointed_frames,
            },
            sort_keys=True,
        )
    )
    return 0


def _restore(args: argparse.Namespace) -> int:
    _settings, _error, store, _credentials, _client = _components()
    result = restore_history_store(
        store,
        args.input,
        service_stopped=args.confirm_service_stopped,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "safety_backup_file": result.safety_backup_path.name,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--remote", action="store_true")
    preflight.add_argument("--require-worker", action="store_true")
    preflight.set_defaults(handler=_preflight)

    diagnostics = commands.add_parser("diagnostics")
    diagnostics.set_defaults(handler=_diagnostics)

    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--mode", choices=("PASSIVE", "TRUNCATE"), default="PASSIVE")
    checkpoint.set_defaults(handler=_checkpoint)

    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    backup.set_defaults(handler=_backup)

    restore = commands.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--confirm-service-stopped", action="store_true")
    restore.set_defaults(handler=_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ChatClearStoreError, OSError, RuntimeError, ValueError):
        print(json.dumps({"ok": False, "error_code": "operation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
