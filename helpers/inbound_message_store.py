from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import threading
import uuid
from contextlib import suppress
from pathlib import Path

_COMPLETION_VERSION = b"gchat-inbound-message-v1\x00"
_IN_PROGRESS_VERSION = 1
_IDEMPOTENCY_PREFIX = "gchat-bot:google-chat-message:"


class InboundMessageStoreError(RuntimeError):
    """Raised when the inbound-message state cannot be used safely."""


class InboundMessageLease:
    """An exclusive claim for one Google Chat message resource.

    Before provider dispatch begins, release permits a clean retry.  Once
    ``begin_dispatch`` persists the stable provider identity, a later process
    recovers the same expected run ID rather than treating the delivery as
    new.  Completing persists a tombstone, so later deliveries are ignored
    even after the application restarts.
    """

    def __init__(
        self,
        store: InboundMessageStore,
        descriptor: int,
        digest: str,
        marker: bytes,
        idempotency_key: str,
        *,
        recovered: bool,
        dispatch_started: bool,
        run_id: str | None,
    ) -> None:
        self._store = store
        self._descriptor: int | None = descriptor
        self._digest = digest
        self._marker = marker
        self._guard = threading.RLock()
        self._completed = False
        self._released = False
        self.idempotency_key = idempotency_key
        self.recovered = recovered
        self.dispatch_started = dispatch_started
        self.run_id = run_id

    def begin_dispatch(self) -> None:
        """Durably mark the provider-dispatch boundary before sending.

        OpenClaw uses the supplied idempotency key as the run ID.  Persisting
        that expected identity before any request bytes are sent closes the
        acceptance/ACK crash gap: a later process can wait on the same run and
        must not submit another agent request.
        """

        with self._guard:
            self._require_active()
            if self.dispatch_started:
                return
            expected_run_id = self.idempotency_key
            self._store._write_in_progress(
                self._digest,
                self._store._encode_in_progress(
                    marker=self._marker,
                    idempotency_key=self.idempotency_key,
                    run_id=expected_run_id,
                ),
            )
            self.dispatch_started = True
            # Keep run_id hidden from the fresh caller: it must submit the
            # first agent request.  A reclaimed lease reads the persisted ID
            # and resumes with agent.wait only.

    def record_run_id(self, run_id: str) -> None:
        """Durably associate an accepted OpenClaw run with this delivery."""

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a nonblank string")
        normalized_run_id = run_id.strip()
        if normalized_run_id != run_id:
            raise ValueError("run_id must not contain surrounding whitespace")

        with self._guard:
            self._require_active()
            if not self.dispatch_started:
                raise InboundMessageStoreError(
                    "cannot record a run before provider dispatch begins"
                )
            expected_run_id = self.run_id or self.idempotency_key
            if expected_run_id != normalized_run_id:
                raise InboundMessageStoreError(
                    "inbound message is already associated with another run"
                )
            if self.run_id == normalized_run_id:
                return
            # begin_dispatch already persisted this expected run ID before
            # sending.  Assigning it locally is enough after the ACK.
            self.run_id = normalized_run_id

    def complete(self) -> None:
        """Durably record successful handling while retaining the claim."""

        with self._guard:
            self._require_active()
            if self._completed:
                return
            self._store._write_completion(self._digest, self._marker)
            self._store._remove_in_progress(self._digest)
            self._completed = True

    def release(self) -> None:
        """Release the in-flight claim; safe to call more than once."""

        with self._guard:
            if self._released:
                return
            self._released = True
            descriptor, self._descriptor = self._descriptor, None

        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> InboundMessageLease:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()

    def __del__(self) -> None:
        with suppress(Exception):
            self.release()

    def _require_active(self) -> None:
        if self._released:
            raise InboundMessageStoreError(
                "cannot update a released inbound-message lease"
            )


class InboundMessageStore:
    """Durable, cross-process deduplication for Google Chat deliveries.

    Each resource name maps to a SHA-256 filename.  An event-specific
    ``flock`` is held for the lifetime of the returned lease, which makes an
    in-flight OS claim disappear automatically if its process exits.  Atomic
    in-progress and completion markers preserve provider reconciliation and
    completed deduplication across process restarts.
    """

    def __init__(self, state_dir: Path | str) -> None:
        raw_path = Path(state_dir).expanduser()
        self._state_dir = Path(os.path.abspath(os.fspath(raw_path)))
        self._state_descriptor = -1
        self._locks_descriptor = -1
        self._in_progress_descriptor = -1
        self._completed_descriptor = -1
        self._initialization_guard = threading.Lock()

    def _ensure_initialized(self) -> None:
        """Open private state directories lazily in the serving process."""

        if self._completed_descriptor >= 0:
            return

        with self._initialization_guard:
            if self._completed_descriptor >= 0:
                return

            try:
                self._reject_existing_symlink_components(self._state_dir)
                self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._state_descriptor = self._open_secure_directory(
                    self._state_dir,
                    label="inbound-message state directory",
                )
                self._locks_descriptor = self._open_child_directory(
                    self._state_descriptor,
                    "locks",
                )
                self._in_progress_descriptor = self._open_child_directory(
                    self._state_descriptor,
                    "in-progress",
                )
                self._completed_descriptor = self._open_child_directory(
                    self._state_descriptor,
                    "completed",
                )
            except InboundMessageStoreError:
                self._close_descriptors()
                raise
            except (OSError, RuntimeError, ValueError) as error:
                self._close_descriptors()
                raise InboundMessageStoreError(
                    f"cannot initialize inbound-message state: {type(error).__name__}"
                ) from error

    def try_claim(self, message_name: str) -> InboundMessageLease | None:
        """Claim an exact nonblank message resource name without blocking."""

        if not isinstance(message_name, str):
            raise TypeError("message_name must be a string")
        if not message_name.strip():
            raise ValueError("message_name must not be blank")
        self._ensure_initialized()

        encoded_name = message_name.encode("utf-8")
        digest = hashlib.sha256(encoded_name).hexdigest()
        marker = _COMPLETION_VERSION + encoded_name
        descriptor: int | None = None
        locked = False

        try:
            descriptor = os.open(
                digest,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=self._locks_descriptor,
            )
            self._secure_regular_file(descriptor, label="inbound-message lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                return None
            locked = True

            if self._completion_exists(digest, marker):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                return None

            idempotency_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{_IDEMPOTENCY_PREFIX}{message_name}",
                )
            )
            dispatch_started, run_id = self._read_in_progress(
                digest,
                expected_marker=marker,
                expected_idempotency_key=idempotency_key,
            )
            return InboundMessageLease(
                self,
                descriptor,
                digest,
                marker,
                idempotency_key,
                recovered=dispatch_started,
                dispatch_started=dispatch_started,
                run_id=run_id,
            )
        except OSError as error:
            if descriptor is not None:
                if locked:
                    with suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)
            raise InboundMessageStoreError(
                f"cannot claim inbound message: {type(error).__name__}"
            ) from error
        except BaseException:
            if descriptor is not None:
                if locked:
                    with suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)
            raise

    @staticmethod
    def _reject_existing_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                component_stat = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InboundMessageStoreError(
                    "cannot inspect inbound-message state path"
                ) from error
            if stat.S_ISLNK(component_stat.st_mode):
                raise InboundMessageStoreError(
                    "inbound-message state path cannot traverse a symlink"
                )
            if current != path and not stat.S_ISDIR(component_stat.st_mode):
                raise InboundMessageStoreError(
                    "inbound-message state parent must be a directory"
                )

    @staticmethod
    def _open_secure_directory(path: Path, *, label: str) -> int:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise InboundMessageStoreError(
                f"{label} must be a real directory"
            ) from error
        try:
            InboundMessageStore._secure_directory_descriptor(descriptor, label=label)
            if path.resolve(strict=True) != path:
                raise InboundMessageStoreError(f"{label} cannot traverse a symlink")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_child_directory(parent_descriptor: int, name: str) -> int:
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise InboundMessageStoreError(
                f"inbound-message {name} directory is unsafe"
            ) from error
        try:
            InboundMessageStore._secure_directory_descriptor(
                descriptor,
                label=f"inbound-message {name} directory",
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _secure_directory_descriptor(descriptor: int, *, label: str) -> None:
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise InboundMessageStoreError(f"{label} must be a directory")
        if directory_stat.st_uid != os.geteuid():
            raise InboundMessageStoreError(f"{label} must be owned by this user")
        os.fchmod(descriptor, 0o700)

    @staticmethod
    def _secure_regular_file(descriptor: int, *, label: str) -> None:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise InboundMessageStoreError(f"{label} must be a private regular file")
        if file_stat.st_uid != os.geteuid():
            raise InboundMessageStoreError(f"{label} must be owned by this user")
        os.fchmod(descriptor, 0o600)

    def _completion_exists(self, digest: str, expected_marker: bytes) -> bool:
        content = self._read_private_file(
            self._completed_descriptor,
            digest,
            label="inbound-message completion marker",
            missing_ok=True,
            maximum_bytes=len(expected_marker) + 1,
        )
        if content is None:
            return False
        if content != expected_marker:
            raise InboundMessageStoreError(
                "inbound-message completion marker does not match its key"
            )
        return True

    @staticmethod
    def _encode_in_progress(
        *,
        marker: bytes,
        idempotency_key: str,
        run_id: str | None,
    ) -> bytes:
        message_name = marker.removeprefix(_COMPLETION_VERSION).decode("utf-8")
        return json.dumps(
            {
                "version": _IN_PROGRESS_VERSION,
                "message_name": message_name,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _read_in_progress(
        self,
        digest: str,
        *,
        expected_marker: bytes,
        expected_idempotency_key: str,
    ) -> tuple[bool, str | None]:
        encoded = self._read_private_file(
            self._in_progress_descriptor,
            digest,
            label="inbound-message in-progress marker",
            missing_ok=True,
            maximum_bytes=64 * 1024,
        )
        if encoded is None:
            return False, None
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InboundMessageStoreError(
                "inbound-message in-progress marker is corrupt"
            ) from error
        if not isinstance(payload, dict):
            raise InboundMessageStoreError(
                "inbound-message in-progress marker is corrupt"
            )

        expected_message_name = expected_marker.removeprefix(
            _COMPLETION_VERSION
        ).decode("utf-8")
        if (
            payload.get("version") != _IN_PROGRESS_VERSION
            or payload.get("message_name") != expected_message_name
            or payload.get("idempotency_key") != expected_idempotency_key
        ):
            raise InboundMessageStoreError(
                "inbound-message in-progress marker does not match its key"
            )

        run_id = payload.get("run_id")
        if run_id != expected_idempotency_key:
            raise InboundMessageStoreError(
                "inbound-message in-progress run ID does not match its "
                "provider identity"
            )
        return True, run_id

    def _read_private_file(
        self,
        directory_descriptor: int,
        name: str,
        *,
        label: str,
        missing_ok: bool,
        maximum_bytes: int,
    ) -> bytes | None:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        except OSError as error:
            raise InboundMessageStoreError(f"cannot inspect {label}") from error

        try:
            marker_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(marker_stat.st_mode)
                or marker_stat.st_nlink != 1
                or marker_stat.st_uid != os.geteuid()
                or stat.S_IMODE(marker_stat.st_mode) != 0o600
            ):
                raise InboundMessageStoreError(f"{label} is unsafe")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if len(content) > maximum_bytes:
                raise InboundMessageStoreError(f"{label} is too large")
            return content
        finally:
            os.close(descriptor)

    def _write_completion(self, digest: str, marker: bytes) -> None:
        self._write_atomic_private_file(
            self._completed_descriptor,
            digest,
            marker,
            label="inbound-message completion",
        )

    def _write_in_progress(self, digest: str, marker: bytes) -> None:
        self._write_atomic_private_file(
            self._in_progress_descriptor,
            digest,
            marker,
            label="inbound-message in-progress state",
        )

    def _write_atomic_private_file(
        self,
        directory_descriptor: int,
        name: str,
        content: bytes,
        *,
        label: str,
    ) -> None:
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            self._secure_regular_file(
                descriptor,
                label=f"temporary {label}",
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short completion-marker write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        except OSError as error:
            raise InboundMessageStoreError(
                f"cannot persist {label}: {type(error).__name__}"
            ) from error
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(FileNotFoundError, OSError):
                os.unlink(temporary, dir_fd=directory_descriptor)

    def _remove_in_progress(self, digest: str) -> None:
        try:
            os.unlink(digest, dir_fd=self._in_progress_descriptor)
            os.fsync(self._in_progress_descriptor)
        except FileNotFoundError:
            return
        except OSError as error:
            raise InboundMessageStoreError(
                f"cannot remove inbound-message in-progress state: "
                f"{type(error).__name__}"
            ) from error

    def _close_descriptors(self) -> None:
        for attribute in (
            "_completed_descriptor",
            "_in_progress_descriptor",
            "_locks_descriptor",
            "_state_descriptor",
        ):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
                setattr(self, attribute, -1)

    def __del__(self) -> None:
        self._close_descriptors()


__all__ = [
    "InboundMessageLease",
    "InboundMessageStore",
    "InboundMessageStoreError",
]
