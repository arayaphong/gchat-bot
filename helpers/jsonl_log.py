from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any


def _secure_open_append(path: Path) -> int:
    """Open a log for append without following any path-component symlink."""

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("secure JSONL logging is unsupported on this platform")

    candidate = Path(path)
    parts = candidate.parts
    if not parts or not candidate.name or candidate.name in {".", ".."}:
        raise OSError("JSONL log path must name a regular file")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK

    if candidate.is_absolute():
        directory_fd = os.open(candidate.anchor, directory_flags)
        parent_parts = parts[1:-1]
    else:
        directory_fd = os.open(".", directory_flags)
        parent_parts = parts[:-1]

    try:
        for component in parent_parts:
            if component in {"", "."}:
                continue
            if component == "..":
                raise OSError("JSONL log path cannot contain parent traversal")
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(
            candidate.name,
            file_flags,
            0o600,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)


def append_jsonl(path: Path, body: dict[str, Any]) -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    line = json.dumps({"timeStamp": ts, "body": body}, ensure_ascii=False)
    descriptor = _secure_open_append(path)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("JSONL log path is not a regular file")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(f"{line}\n".encode())
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("failed to append JSONL record")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)
