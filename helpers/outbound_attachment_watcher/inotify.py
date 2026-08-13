from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

# Linux inotify masks.  Keeping the small set used by the service here makes the
# event-processing code importable in tests.  The production factory below is
# still backed by inotify_simple.
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000

WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF


class InotifyEvent(Protocol):
    wd: int
    mask: int
    name: str


class InotifyHandle(Protocol):
    def add_watch(self, path: str, mask: int) -> int: ...

    def read(self, timeout: int | None = None) -> list[InotifyEvent]: ...

    def close(self) -> None: ...


InotifyFactory = Callable[[], InotifyHandle]


def _inotify_simple_factory() -> InotifyHandle:
    try:
        from inotify_simple import INotify
    except ImportError as error:  # pragma: no cover - depends on deployment
        raise RuntimeError(
            "outbound attachment watching requires the inotify-simple package"
        ) from error
    return INotify(nonblocking=True)
