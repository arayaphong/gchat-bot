from __future__ import annotations

from pathlib import Path


class SendableFilePolicy:
    def __init__(self, allowed_roots: list[Path]) -> None:
        self._allowed_roots = [root.resolve() for root in allowed_roots]

    def is_allowed(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self._allowed_roots)
