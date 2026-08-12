from __future__ import annotations

import hashlib
from pathlib import Path

from helpers.session_keys import ChatSessionContext

THREAD_UPLOAD_DIRECTORY_PREFIX = "thread-"


def thread_upload_directory_name(session_key: str) -> str:
    """Return a fixed-size, filesystem-safe directory name for one Chat thread."""

    context = ChatSessionContext.from_session_key(session_key)
    digest = hashlib.sha256(context.session_key.encode("utf-8")).hexdigest()
    return f"{THREAD_UPLOAD_DIRECTORY_PREFIX}{digest}"


def thread_upload_directory(root: Path, session_key: str) -> Path:
    """Return the direct child of ``root`` reserved for one Chat thread."""

    normalized_root = root.expanduser().resolve(strict=False)
    return normalized_root / thread_upload_directory_name(session_key)


__all__ = [
    "THREAD_UPLOAD_DIRECTORY_PREFIX",
    "thread_upload_directory",
    "thread_upload_directory_name",
]
