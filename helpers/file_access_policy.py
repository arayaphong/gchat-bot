from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def is_relative_to_any(path: Path, roots: Iterable[Path]) -> bool:
    """Return True if path is contained within any of roots.

    Shared by every file-serving path (inbound Drive/local sends and
    outbound MEDIA: attachments) that needs to check a resolved path against
    a set of allowed root directories.
    """
    return any(path.is_relative_to(root) for root in roots)


class SendableFilePolicy:
    def __init__(self, allowed_roots: list[Path]) -> None:
        self._allowed_roots = [root.resolve() for root in allowed_roots]

    def is_allowed(self, path: Path) -> bool:
        return is_relative_to_any(path, self._allowed_roots)
