from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_DELIVERY_ID_NAMESPACE = uuid.UUID("20f3495c-a130-4e3b-a6b5-cda5dc0ef596")


class DeliveryDisposition(Enum):
    """Result returned by the injected, one-file delivery callback."""

    DELIVERED = "delivered"
    FAILED = "failed"
    DEFERRED = "deferred"


class AttachmentSubmissionDisposition(Enum):
    """Durable-ingress result for an explicitly referenced local file."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class OutboundAttachment:
    source_path: Path
    source_root: Path
    staged_path: Path | None
    display_name: str
    size: int
    sha256: str
    delivery_id: str = ""
    drive_file_id: str = ""
    web_view_link: str = ""
    destination_space: str = ""
    destination_thread: str = ""


@dataclass(frozen=True)
class OutboundDeliveryResult:
    """Delivery disposition plus a reusable remote-upload receipt."""

    disposition: DeliveryDisposition
    drive_file_id: str = ""
    web_view_link: str = ""


@dataclass(frozen=True)
class AttachmentSubmissionResult:
    disposition: AttachmentSubmissionDisposition
    error_category: str = ""


@dataclass(frozen=True)
class FinalDeliveryFailure:
    attachment: OutboundAttachment
    attempts: int
    error_category: str


DeliveryCallback = Callable[
    [OutboundAttachment], OutboundDeliveryResult | DeliveryDisposition | bool
]
FinalFailureCallback = Callable[[FinalDeliveryFailure], None]


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _DeliveryRecord:
    record_id: int
    attachment: OutboundAttachment
    attempts: int
    last_error_category: str


@dataclass(frozen=True)
class _ThreadUploadRoute:
    directory_name: str
    session_key: str
    source_root: Path
    destination_space: str
    destination_thread: str


@dataclass(frozen=True)
class _WatchBinding:
    source_root: Path
    destination_space: str = ""
    destination_thread: str = ""
    session_key: str = ""


@dataclass(frozen=True)
class _CandidateJob:
    binding: _WatchBinding
    path: Path
    promote_baseline: bool


@dataclass(frozen=True)
class _RescanJob:
    binding: _WatchBinding | None = None
