from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers.chat_history_settings import (
    HISTORY_STATE_DIR_ENV,
    default_chat_history_state_dir,
)
from helpers.outbound_attachment_watcher import (
    AttachmentSubmissionDisposition,
    OutboundAttachmentConfig,
    OutboundAttachmentService,
)


class ChatHistoryMediaPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.uploads = self.base / "uploads"
        self.outbound_state = self.base / "outbound-state"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_service(
        self,
        *,
        source_dirs: tuple[Path, ...] | None = None,
        watched_source_dirs: tuple[Path, ...] | None = None,
    ) -> OutboundAttachmentService:
        config = OutboundAttachmentConfig(
            source_dirs=source_dirs or (self.base,),
            watched_source_dirs=watched_source_dirs or (self.uploads,),
            state_dir=self.outbound_state,
            stability_checks=1,
            stability_interval_seconds=0,
            readiness_timeout_seconds=0.1,
            capture_retry_delays_seconds=(0,),
        )
        return OutboundAttachmentService(
            delivery_callback=lambda _attachment: True,
            final_failure_callback=lambda _failure: None,
            config=config,
        )

    def assert_history_path_rejected(
        self,
        service: OutboundAttachmentService,
        path: Path,
        *,
        ordinal: int,
    ) -> None:
        result = service.submit_explicit(
            path,
            idempotency_key=f"history-state:{ordinal}",
        )
        self.assertIs(
            result.disposition,
            AttachmentSubmissionDisposition.REJECTED,
        )
        self.assertEqual(result.error_category, "state_dir_not_allowed")

    def test_configured_history_db_wal_shm_locks_and_descendants_are_blocked(
        self,
    ) -> None:
        history_state = self.base / "private" / "chat-history"
        protected_paths = (
            history_state / "history.sqlite3",
            history_state / "history.sqlite3-wal",
            history_state / "history.sqlite3-shm",
            history_state / "worker.lock",
            history_state / "locks" / "space.lock",
        )
        with patch.dict(
            os.environ,
            {HISTORY_STATE_DIR_ENV: str(history_state)},
        ):
            service = self.make_service()

        for ordinal, path in enumerate(protected_paths):
            with self.subTest(path=path):
                self.assert_history_path_rejected(service, path, ordinal=ordinal)

    def test_default_history_state_root_is_blocked_while_feature_is_off(self) -> None:
        default_state = default_chat_history_state_dir().resolve(strict=False)
        with patch.dict(os.environ, {HISTORY_STATE_DIR_ENV: ""}):
            service = self.make_service(
                source_dirs=(Path.home(), self.base),
            )

        self.assert_history_path_rejected(
            service,
            default_state / "history.sqlite3",
            ordinal=0,
        )

    def test_symlink_into_history_state_is_blocked(self) -> None:
        history_state = self.base / "private" / "chat-history"
        history_state.mkdir(parents=True)
        database = history_state / "history.sqlite3"
        database.write_bytes(b"private ledger")
        media_root = self.base / "generated"
        media_root.mkdir()
        history_link = media_root / "history-link"
        history_link.symlink_to(history_state, target_is_directory=True)

        with patch.dict(
            os.environ,
            {HISTORY_STATE_DIR_ENV: str(history_state)},
        ):
            service = self.make_service()

        self.assert_history_path_rejected(
            service,
            history_link / database.name,
            ordinal=0,
        )

    def test_history_state_cannot_overlap_an_auto_watched_directory(self) -> None:
        with (
            patch.dict(
                os.environ,
                {HISTORY_STATE_DIR_ENV: str(self.uploads)},
            ),
            self.assertRaisesRegex(ValueError, "blocked_roots"),
        ):
            self.make_service()

    def test_neighboring_media_file_remains_sendable(self) -> None:
        history_state = self.base / "private" / "chat-history"
        media_root = self.base / "generated"
        media_root.mkdir()
        media_file = media_root / "image.png"
        media_file.write_bytes(b"image")

        with patch.dict(
            os.environ,
            {HISTORY_STATE_DIR_ENV: str(history_state)},
        ):
            service = self.make_service()

        result = service.submit_explicit(
            media_file,
            idempotency_key="ordinary-media:0",
        )
        self.assertIs(
            result.disposition,
            AttachmentSubmissionDisposition.ACCEPTED,
        )


if __name__ == "__main__":
    unittest.main()
