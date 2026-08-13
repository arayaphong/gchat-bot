from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UPLOAD_DIR = Path("~/.openclaw/workspace/uploads").expanduser()
# These roots constrain automatic file discovery only. Explicit MEDIA: paths
# may reference any absolute regular file that the Jinx process can read.
# Automatic files are watched only in registered thread children beneath
# DEFAULT_UPLOAD_DIR. This mirrors the production configuration in app.py.
DEFAULT_SOURCE_DIRS = (Path.home(), Path("/tmp"))


@dataclass(frozen=True)
class OutboundAttachmentConfig:
    source_dirs: tuple[Path, ...]
    watched_source_dirs: tuple[Path, ...]
    state_dir: Path
    max_file_bytes: int = 20 * 1024 * 1024
    stability_checks: int = 2
    stability_interval_seconds: float = 0.25
    readiness_timeout_seconds: float = 30.0
    max_delivery_attempts: int = 3
    retry_delays_seconds: tuple[float, ...] = (1.0, 5.0)
    capture_retry_delays_seconds: tuple[float, ...] = (0.25, 1.0)
    deferral_delay_seconds: float = 1.0
    notification_retry_delay_seconds: float = 5.0
    worker_poll_seconds: float = 0.1
    inotify_read_timeout_ms: int = 200
    lock_retry_seconds: float = 0.5
    thread_upload_root: Path | None = None

    def __post_init__(self) -> None:
        normalized_sources = tuple(
            path.expanduser().resolve(strict=False) for path in self.source_dirs
        )
        if not normalized_sources:
            raise ValueError("source_dirs must contain at least one path")
        # Distinct entries are not required: e.g. under HOME=/tmp, Path.home()
        # and Path("/tmp") legitimately resolve to the same directory, and
        # that overlap is harmless - every check below matches by
        # containment, not by index.
        object.__setattr__(self, "source_dirs", normalized_sources)
        normalized_watched_sources = tuple(
            path.expanduser().resolve(strict=False) for path in self.watched_source_dirs
        )
        if (
            len(set(normalized_watched_sources)) != len(normalized_watched_sources)
            or any(
                not any(
                    watched.is_relative_to(source) for source in normalized_sources
                )
                for watched in normalized_watched_sources
            )
        ):
            raise ValueError(
                "watched_source_dirs must be a distinct set of paths within "
                "source_dirs"
            )
        object.__setattr__(self, "watched_source_dirs", normalized_watched_sources)
        normalized_state = self.state_dir.expanduser().resolve(strict=False)
        object.__setattr__(self, "state_dir", normalized_state)
        if any(
            normalized_state.is_relative_to(watched)
            or watched.is_relative_to(normalized_state)
            for watched in normalized_watched_sources
        ):
            raise ValueError(
                "state_dir and watched_source_dirs must not contain one another"
            )

        normalized_thread_upload_root = (
            self.thread_upload_root.expanduser().resolve(strict=False)
            if self.thread_upload_root is not None
            else None
        )
        if normalized_thread_upload_root is not None:
            if not any(
                normalized_thread_upload_root == source
                or normalized_thread_upload_root.is_relative_to(source)
                for source in normalized_sources
            ):
                raise ValueError("thread_upload_root must be within source_dirs")
            if normalized_state.is_relative_to(
                normalized_thread_upload_root
            ) or normalized_thread_upload_root.is_relative_to(normalized_state):
                raise ValueError(
                    "state_dir and thread_upload_root must not contain one another"
                )
        object.__setattr__(
            self,
            "thread_upload_root",
            normalized_thread_upload_root,
        )

        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if self.stability_checks < 1:
            raise ValueError("stability_checks must be positive")
        if self.stability_interval_seconds < 0:
            raise ValueError("stability_interval_seconds cannot be negative")
        if self.readiness_timeout_seconds <= 0:
            raise ValueError("readiness_timeout_seconds must be positive")
        if self.max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if not self.retry_delays_seconds and self.max_delivery_attempts > 1:
            raise ValueError(
                "retry_delays_seconds is required when retries are enabled"
            )
        if any(delay < 0 for delay in self.retry_delays_seconds):
            raise ValueError("retry delays cannot be negative")
        if any(delay < 0 for delay in self.capture_retry_delays_seconds):
            raise ValueError("capture retry delays cannot be negative")
        if self.deferral_delay_seconds <= 0:
            raise ValueError("deferral_delay_seconds must be positive")
        if self.notification_retry_delay_seconds <= 0:
            raise ValueError("notification_retry_delay_seconds must be positive")
        if self.worker_poll_seconds <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        if self.inotify_read_timeout_ms < 1:
            raise ValueError("inotify_read_timeout_ms must be positive")
        if self.lock_retry_seconds <= 0:
            raise ValueError("lock_retry_seconds must be positive")

    @classmethod
    def default(cls, state_dir: Path | None = None) -> OutboundAttachmentConfig:
        if state_dir is None:
            xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
            state_root = (
                Path(xdg_state).expanduser()
                if xdg_state
                else Path("~/.local/state").expanduser()
            )
            state_dir = state_root / "gchat-bot" / "outbound-attachments"
        return cls(
            source_dirs=DEFAULT_SOURCE_DIRS,
            watched_source_dirs=(),
            state_dir=state_dir,
            thread_upload_root=DEFAULT_UPLOAD_DIR,
        )
