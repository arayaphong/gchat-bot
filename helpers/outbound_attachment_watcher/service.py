from __future__ import annotations

import hashlib
import os
import queue
import sqlite3
import stat
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from helpers.session_keys import (
    ChatSessionContext,
    normalize_space_name,
    normalize_thread_name,
)
from helpers.thread_uploads import thread_upload_directory

from .capture import (
    StagingCapture,
    ensure_real_directory,
    identity_from_stat,
    regular_identity_unrestricted,
)
from .config import OutboundAttachmentConfig
from .inotify import (
    IN_CLOSE_WRITE,
    IN_DELETE_SELF,
    IN_IGNORED,
    IN_ISDIR,
    IN_MOVE_SELF,
    IN_MOVED_TO,
    IN_Q_OVERFLOW,
    WATCH_MASK,
    InotifyEvent,
    InotifyFactory,
    InotifyHandle,
    _inotify_simple_factory,
)
from .ledger import _Ledger
from .locks import _SingletonProcessLock, _StagingTransactionLock
from .models import (
    AttachmentSubmissionDisposition,
    AttachmentSubmissionResult,
    DeliveryCallback,
    DeliveryDisposition,
    FinalDeliveryFailure,
    FinalFailureCallback,
    OutboundDeliveryResult,
    _CandidateJob,
    _DeliveryRecord,
    _FileIdentity,
    _RescanJob,
    _ThreadUploadRoute,
    _WatchBinding,
)


class OutboundAttachmentService:
    """Capture allowed bot outputs and durably deliver each file once.

    ``start`` is intentionally non-blocking.  A process that cannot acquire the
    singleton lock remains a standby and retries until the active owner exits.
    ``wait_until_active`` is available for startup health checks and tests.
    Explicit submissions and thread-outbox registrations are persisted through
    SQLite and therefore also work when called by a standby process.
    """

    def __init__(
        self,
        *,
        delivery_callback: DeliveryCallback,
        final_failure_callback: FinalFailureCallback,
        config: OutboundAttachmentConfig | None = None,
        inotify_factory: InotifyFactory = _inotify_simple_factory,
    ) -> None:
        self._config = config or OutboundAttachmentConfig.default()
        self._delivery_callback = delivery_callback
        self._final_failure_callback = final_failure_callback
        self._inotify_factory = inotify_factory

        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._active_event = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._active_stop: threading.Event | None = None
        self._inotify: InotifyHandle | None = None
        self._ledger: _Ledger | None = None
        self._jobs: queue.Queue[_CandidateJob | _RescanJob] | None = None
        self._watch_roots: dict[int, _WatchBinding] = {}
        self._watch_descriptors: dict[Path, int] = {}
        self._watch_bindings_lock = threading.RLock()
        self._last_start_error: Exception | None = None
        self._process_lock = _SingletonProcessLock(
            self._config.state_dir / "watcher.lock"
        )
        self._staging_capture = StagingCapture(
            self._config,
            self._should_stop_active,
            self._wait_active_stop,
        )

    @property
    def is_active(self) -> bool:
        return self._active_event.is_set()

    @property
    def last_start_error(self) -> Exception | None:
        return self._last_start_error

    def status_counts(self) -> dict[str, int]:
        ledger = self._ledger
        return ledger.status_counts() if ledger is not None else {}

    def prepare_thread_upload(self, context: ChatSessionContext) -> Path:
        """Create and durably bind the outbox for one deterministic Chat thread."""

        if not isinstance(context, ChatSessionContext):
            raise TypeError("context must be a ChatSessionContext")
        decoded = ChatSessionContext.from_session_key(context.session_key)
        if (
            decoded.space != context.space
            or decoded.thread != context.thread
            or context.reply_thread != context.thread
        ):
            raise ValueError("context identity does not match its session key")
        upload_root = self._config.thread_upload_root
        if upload_root is None:
            raise RuntimeError("thread-scoped upload directories are not configured")

        source_root = thread_upload_directory(upload_root, context.session_key)
        route = _ThreadUploadRoute(
            directory_name=source_root.name,
            session_key=context.session_key,
            source_root=source_root,
            destination_space=context.space,
            destination_thread=context.reply_thread,
        )
        ledger: _Ledger | None = None
        self._ensure_state_directories()
        with _StagingTransactionLock(
            self._config.state_dir / "thread-upload-routes.lock"
        ):
            self._ensure_thread_upload_root()
            self._ensure_thread_route_directory(source_root)
            try:
                ledger = _Ledger(self._config.state_dir / "ledger.sqlite3")
                if not ledger.has_thread_upload_route(route):
                    baseline_entries = self._thread_route_baseline_entries(route)
                    ledger.register_thread_upload_route(route, baseline_entries)
            finally:
                if ledger is not None:
                    ledger.close()
        return source_root

    def submit_explicit(
        self,
        path: str | Path,
        *,
        idempotency_key: str,
        destination_space: str = "",
        destination_thread: str = "",
    ) -> AttachmentSubmissionResult:
        """Durably stage an explicit MEDIA-referenced file before acknowledging it."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        destination_space, destination_thread = self._normalize_destination(
            destination_space,
            destination_thread,
        )

        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "invalid_path",
            )
        if not candidate.is_absolute():
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "path_not_absolute",
            )
        try:
            candidate = Path(os.path.abspath(candidate))
        except (OSError, ValueError):
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "invalid_path",
            )
        try:
            resolved_candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.REJECTED,
                "invalid_path",
            )

        # Explicit MEDIA: paths come from OpenClaw's isolated sandbox, so they
        # intentionally bypass the automatic-discovery allowlist. Resolve once
        # up front so final and intermediate symlinks are valid references, then
        # retain the original path as delivery metadata and the display name.
        source_root = Path(resolved_candidate.anchor)

        signature = self._explicit_signature(idempotency_key.strip())
        ledger: _Ledger | None = None
        staged_path: Path | None = None
        try:
            self._ensure_state_directories()
            with _StagingTransactionLock(self._config.state_dir / "staging.lock"):
                ledger = _Ledger(self._config.state_dir / "ledger.sqlite3")
                thread_upload_root = self._config.thread_upload_root
                if (
                    thread_upload_root is not None
                    and resolved_candidate.parent.parent == thread_upload_root
                    and ledger.has_thread_upload_directory(
                        resolved_candidate.parent.name
                    )
                ):
                    # The registered watcher owns this file. Treat an optional
                    # MEDIA reference as a successful no-op so the unchanged
                    # directive contract neither duplicates the delivery nor
                    # emits a false rejection notice.
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED,
                    )
                if ledger.signature_status(signature) is not None:
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                identity, last_identity, readiness_error = (
                    self._staging_capture.wait_for_explicit_identity(
                        resolved_candidate, source_root
                    )
                )
                if identity is None:
                    if readiness_error in {
                        "invalid_path",
                        "not_regular_file",
                        "source_root_unavailable",
                        "symlink_not_allowed",
                    }:
                        return AttachmentSubmissionResult(
                            AttachmentSubmissionDisposition.REJECTED,
                            readiness_error,
                        )
                    ledger.register_validation_failure(
                        signature=signature,
                        source_root=source_root,
                        source_path=candidate,
                        identity=last_identity
                        or _FileIdentity(
                            device=0,
                            inode=0,
                            size=0,
                            mtime_ns=0,
                            ctime_ns=0,
                        ),
                        error_category=readiness_error,
                        promote_baseline=False,
                        destination_space=destination_space,
                        destination_thread=destination_thread,
                    )
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                validation_error = (
                    "empty_file"
                    if identity.size == 0
                    else "file_too_large"
                    if identity.size > self._config.max_file_bytes
                    else ""
                )
                if validation_error:
                    ledger.register_validation_failure(
                        signature=signature,
                        source_root=source_root,
                        source_path=candidate,
                        identity=identity,
                        error_category=validation_error,
                        promote_baseline=False,
                        destination_space=destination_space,
                        destination_thread=destination_thread,
                    )
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                capture_error = "staging_failed"
                sha256 = ""
                for capture_attempt in range(
                    len(self._config.capture_retry_delays_seconds) + 1
                ):
                    try:
                        captured = self._staging_capture.capture_explicit_to_staging(
                            source_root,
                            resolved_candidate,
                            identity,
                        )
                    except (OSError, ValueError):
                        captured = None
                    if captured is not None:
                        staged_path, sha256 = captured
                        break

                    current = regular_identity_unrestricted(resolved_candidate)
                    if current is None:
                        capture_error = "source_unavailable"
                    else:
                        identity = current
                        capture_error = (
                            "empty_file"
                            if identity.size == 0
                            else "file_too_large"
                            if identity.size > self._config.max_file_bytes
                            else "staging_failed"
                        )
                    if capture_attempt < len(self._config.capture_retry_delays_seconds):
                        time.sleep(
                            self._config.capture_retry_delays_seconds[capture_attempt]
                        )

                if staged_path is None:
                    ledger.register_validation_failure(
                        signature=signature,
                        source_root=source_root,
                        source_path=candidate,
                        identity=identity,
                        error_category=capture_error,
                        promote_baseline=False,
                        destination_space=destination_space,
                        destination_thread=destination_thread,
                    )
                    return AttachmentSubmissionResult(
                        AttachmentSubmissionDisposition.ACCEPTED
                    )

                registered = ledger.register_staged(
                    signature=signature,
                    source_root=source_root,
                    source_path=candidate,
                    identity=identity,
                    sha256=sha256,
                    staged_path=staged_path,
                    promote_baseline=False,
                    destination_space=destination_space,
                    destination_thread=destination_thread,
                )
                if not registered:
                    self._unlink_quietly(staged_path)
                return AttachmentSubmissionResult(
                    AttachmentSubmissionDisposition.ACCEPTED
                )
        except (OSError, RuntimeError, sqlite3.Error):
            if staged_path is not None:
                self._unlink_quietly(staged_path)
            return AttachmentSubmissionResult(
                AttachmentSubmissionDisposition.UNAVAILABLE,
                "durable_ingress_unavailable",
            )
        finally:
            if ledger is not None:
                ledger.close()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._supervisor_thread and self._supervisor_thread.is_alive():
                return
            self._stop_event.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervise,
                name="outbound-attachment-supervisor",
                daemon=True,
            )
            self._supervisor_thread.start()

    def wait_until_active(self, timeout: float | None = None) -> bool:
        return self._active_event.wait(timeout)

    def stop(self, timeout: float | None = 10.0) -> None:
        with self._lifecycle_lock:
            supervisor = self._supervisor_thread
            if supervisor is None:
                return
            self._stop_event.set()
            active_stop = self._active_stop
            if active_stop is not None:
                active_stop.set()
            inotify = self._inotify
            if inotify is not None:
                with suppress(OSError):
                    inotify.close()

        supervisor.join(timeout)
        if supervisor.is_alive():
            raise TimeoutError("outbound attachment service did not stop in time")
        with self._lifecycle_lock:
            self._supervisor_thread = None

    def _supervise(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    acquired = self._process_lock.try_acquire()
                except OSError as error:
                    self._last_start_error = error
                    self._stop_event.wait(self._config.lock_retry_seconds)
                    continue
                if not acquired:
                    self._stop_event.wait(self._config.lock_retry_seconds)
                    continue
                try:
                    self._activate()
                    while not self._stop_event.is_set():
                        active_stop = self._active_stop
                        reader = self._reader_thread
                        worker = self._worker_thread
                        if (
                            active_stop is None
                            or active_stop.is_set()
                            or reader is None
                            or worker is None
                            or not reader.is_alive()
                            or not worker.is_alive()
                        ):
                            break
                        self._stop_event.wait(0.1)
                except Exception as error:  # noqa: BLE001
                    self._last_start_error = error
                finally:
                    self._deactivate()
                    self._process_lock.release()
                if not self._stop_event.is_set():
                    self._stop_event.wait(self._config.lock_retry_seconds)
        finally:
            self._deactivate()
            self._process_lock.release()

    def _activate(self) -> None:
        self._ensure_state_directories()
        for source_dir in self._config.watched_source_dirs:
            source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._config.thread_upload_root is not None:
            self._ensure_thread_upload_root()

        self._active_stop = threading.Event()
        self._jobs = queue.Queue()
        self._watch_roots = {}
        self._watch_descriptors = {}
        self._ledger = _Ledger(self._config.state_dir / "ledger.sqlite3")
        self._ledger.recover_interrupted()
        with _StagingTransactionLock(self._config.state_dir / "staging.lock"):
            self._cleanup_sent_staging()
            self._cleanup_orphaned_staging()

        static_bindings = tuple(
            _WatchBinding(source_root=source_dir)
            for source_dir in self._config.watched_source_dirs
        )
        baseline_state = "complete"
        baseline_cutover_ns: int | None = None
        baseline_snapshot: set[Path] | None = None
        if static_bindings:
            baseline_state = self._ledger.baseline_state()
            if baseline_state == "new":
                # Snapshot the pre-existing entries by name before persisting
                # the boundary. The ctime comparison against the cutover alone
                # assumes the process clock and the filesystem timestamp clock
                # agree; in containers/VMs where they can skew, a file created
                # during this start could look older than the cutover and be
                # silently baselined (never delivered). The name snapshot does
                # not depend on any clock.
                baseline_snapshot = {
                    path
                    for _binding, path in self._iter_source_entries(static_bindings)
                }
                # Persist the boundary before installing watches. If this process
                # dies anywhere after this commit, the next owner can still tell
                # pre-existing files from output created during the failed start.
                baseline_cutover_ns = self._ledger.begin_baseline()
                baseline_state = "started"
            elif baseline_state == "started":
                baseline_cutover_ns = self._ledger.baseline_cutover_ns()
                if baseline_cutover_ns is None:
                    # Compatibility for an interrupted ledger written before the
                    # durable cutover marker existed. Its original boundary is
                    # unknowable, so establish a conservative new one rather than
                    # bulk-sending every file already present.
                    baseline_cutover_ns = (
                        self._ledger.repair_missing_baseline_cutover()
                    )

        watches_enabled = bool(
            static_bindings or self._config.thread_upload_root is not None
        )
        if watches_enabled:
            self._inotify = self._inotify_factory()

        if static_bindings:
            for binding in static_bindings:
                self._add_watch(binding)
            if baseline_state == "complete":
                for binding in static_bindings:
                    self._schedule_reconciliation(binding)
            else:
                if baseline_cutover_ns is None:
                    raise RuntimeError("first baseline has no durable cutover")
                self._baseline_existing(
                    baseline_cutover_ns,
                    static_bindings,
                    baseline_snapshot,
                )

        if self._config.thread_upload_root is not None:
            self._refresh_thread_upload_routes()

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="outbound-attachment-worker",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="outbound-attachment-inotify",
            daemon=True,
        )
        self._worker_thread.start()
        self._reader_thread.start()
        self._last_start_error = None
        self._active_event.set()

    def _deactivate(self) -> None:
        self._active_event.clear()
        active_stop = self._active_stop
        if active_stop is not None:
            active_stop.set()
        inotify, self._inotify = self._inotify, None
        if inotify is not None:
            with suppress(OSError):
                inotify.close()
        for thread in (self._reader_thread, self._worker_thread):
            if thread is not None and thread is not threading.current_thread():
                # Never close the ledger or release the singleton lock while a
                # callback is still using them.  ``stop(timeout=...)`` may
                # report a timeout to its caller, but shutdown continues safely
                # and ownership transfers only after both threads have exited.
                thread.join()
        self._reader_thread = None
        self._worker_thread = None
        ledger, self._ledger = self._ledger, None
        if ledger is not None:
            ledger.close()
        self._jobs = None
        self._watch_roots = {}
        self._watch_descriptors = {}
        self._active_stop = None

    def _reader_loop(self) -> None:
        try:
            if (
                not self._config.watched_source_dirs
                and self._config.thread_upload_root is None
            ):
                active_stop = self._active_stop
                if active_stop is None:
                    return
                while not self._should_stop_active():
                    active_stop.wait(self._config.worker_poll_seconds)
                return
            while not self._should_stop_active():
                if self._config.thread_upload_root is not None:
                    self._refresh_thread_upload_routes()
                inotify = self._inotify
                if inotify is None:
                    return
                try:
                    events = inotify.read(timeout=self._config.inotify_read_timeout_ms)
                except (OSError, ValueError):
                    if self._should_stop_active():
                        return
                    raise
                for event in events:
                    self._handle_inotify_event(event)
        except Exception as error:  # noqa: BLE001
            self._last_start_error = error
            if self._active_stop is not None:
                self._active_stop.set()

    def _handle_inotify_event(self, event: InotifyEvent) -> None:
        if event.mask & IN_Q_OVERFLOW:
            self._schedule_reconciliation()
            return

        binding = self._watch_roots.get(event.wd)
        if binding is None:
            return
        if event.mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED):
            self._repair_watch(event.wd, binding)
            return
        if not event.name or event.mask & IN_ISDIR:
            return
        if self._should_ignore_name(event.name):
            return
        if event.mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
            self._schedule_candidate(
                binding,
                binding.source_root / event.name,
                promote_baseline=True,
            )

    def _repair_watch(self, old_wd: int, binding: _WatchBinding) -> None:
        with self._watch_bindings_lock:
            self._watch_roots.pop(old_wd, None)
            self._watch_descriptors.pop(binding.source_root, None)
        if binding.session_key:
            self._ensure_thread_route_directory(binding.source_root)
        else:
            binding.source_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._add_watch(binding)
        self._schedule_reconciliation(binding)

    def _add_watch(self, binding: _WatchBinding) -> None:
        inotify = self._inotify
        if inotify is None:
            raise RuntimeError("inotify is not initialized")
        with self._watch_bindings_lock:
            if binding.source_root in self._watch_descriptors:
                return
            wd = inotify.add_watch(str(binding.source_root), WATCH_MASK)
            self._watch_roots[wd] = binding
            self._watch_descriptors[binding.source_root] = wd

    def _refresh_thread_upload_routes(self) -> None:
        upload_root = self._config.thread_upload_root
        if upload_root is None:
            return
        for directory_name, session_key, destination_space, destination_thread in (
            self._require_ledger().thread_upload_routes()
        ):
            context = ChatSessionContext.from_session_key(session_key)
            expected_root = thread_upload_directory(upload_root, session_key)
            if (
                directory_name != expected_root.name
                or destination_space != context.space
                or destination_thread != context.reply_thread
            ):
                raise RuntimeError("stored thread upload route is invalid")
            binding = _WatchBinding(
                source_root=expected_root,
                destination_space=context.space,
                destination_thread=context.reply_thread,
                session_key=context.session_key,
            )
            with self._watch_bindings_lock:
                already_watched = expected_root in self._watch_descriptors
            if already_watched:
                continue
            self._ensure_thread_route_directory(expected_root)
            self._add_watch(binding)
            # Add the watch before scanning. The scan catches output written
            # before installation, and inotify catches output written after it.
            self._schedule_reconciliation(binding)

    def _worker_loop(self) -> None:
        try:
            while not self._should_stop_active():
                jobs = self._jobs
                if jobs is None:
                    return
                try:
                    job = jobs.get(timeout=self._config.worker_poll_seconds)
                except queue.Empty:
                    job = None

                if isinstance(job, _RescanJob):
                    self._scan_and_schedule(job.binding)
                elif isinstance(job, _CandidateJob):
                    self._stage_candidate(job)

                self._deliver_all_due()
                self._notify_all_due_failures()
        except Exception as error:  # noqa: BLE001
            self._last_start_error = error
            if self._active_stop is not None:
                self._active_stop.set()

    def _schedule_candidate(
        self, binding: _WatchBinding, path: Path, *, promote_baseline: bool
    ) -> None:
        jobs = self._jobs
        if jobs is None:
            return
        jobs.put(
            _CandidateJob(
                binding=binding,
                path=path,
                promote_baseline=promote_baseline,
            )
        )

    def _schedule_reconciliation(
        self, binding: _WatchBinding | None = None
    ) -> None:
        jobs = self._jobs
        if jobs is not None:
            jobs.put(_RescanJob(binding))

    def _baseline_existing(
        self,
        cutover_ns: int,
        bindings: Sequence[_WatchBinding],
        snapshot: set[Path] | None = None,
    ) -> None:
        entries: list[tuple[str, Path, Path, _FileIdentity]] = []
        post_cutover_entries: list[tuple[_WatchBinding, Path]] = []
        for binding, path in self._iter_source_entries(bindings):
            source_root = binding.source_root
            identity = self._staging_capture.regular_identity(path)
            if identity is None:
                continue
            # A path absent from the pre-baseline name snapshot was created
            # during this start, regardless of how its ctime compares to the
            # cutover under a skewed filesystem clock. The ctime check remains
            # for restarts recovering an interrupted baseline, where no
            # snapshot could be taken.
            if (snapshot is not None and path not in snapshot) or (
                identity.ctime_ns >= cutover_ns
            ):
                post_cutover_entries.append((binding, path))
                continue
            entries.append(
                (
                    self._signature(source_root, path, identity),
                    source_root,
                    path,
                    identity,
                )
            )
        self._require_ledger().finish_baseline(entries)
        # These files appeared after the persisted boundary, possibly before
        # watches were installed or while the initial scan was running. Queue
        # them explicitly so correctness does not depend on the corresponding
        # inotify event surviving an overflow.
        for binding, path in post_cutover_entries:
            self._schedule_candidate(binding, path, promote_baseline=True)

    def _scan_and_schedule(self, binding: _WatchBinding | None = None) -> None:
        bindings = (binding,) if binding is not None else None
        for current_binding, path in self._iter_source_entries(bindings):
            self._schedule_candidate(
                current_binding,
                path,
                promote_baseline=False,
            )

    def _iter_source_entries(
        self,
        bindings: Sequence[_WatchBinding] | None = None,
    ) -> Sequence[tuple[_WatchBinding, Path]]:
        entries: list[tuple[_WatchBinding, Path]] = []
        if bindings is None:
            with self._watch_bindings_lock:
                bindings = tuple(self._watch_roots.values())
        for binding in dict.fromkeys(bindings):
            source_root = binding.source_root
            try:
                entries.extend(
                    (binding, path)
                    for path in source_root.iterdir()
                    if not self._should_ignore_name(path.name)
                )
            except FileNotFoundError:
                if binding.session_key:
                    self._ensure_thread_route_directory(source_root)
                else:
                    source_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return entries

    def _stage_candidate(self, job: _CandidateJob) -> None:
        path = job.path
        promote_baseline = job.promote_baseline
        # The watch loop always schedules jobs against the exact
        # watched_source_dirs entry the file was found under; re-deriving it
        # here via _source_root_for would pick the broadest matching
        # source_dir (e.g. the home directory) instead, changing the
        # dedup signature and causing already-delivered files to be
        # re-captured on every restart.
        binding = job.binding
        source_root = binding.source_root

        identity = self._staging_capture.wait_for_stable_identity(path)
        if identity is None:
            if self._should_stop_active():
                return
            current = self._staging_capture.regular_identity(path)
            if current is not None:
                self._register_capture_failure(
                    binding,
                    path,
                    current,
                    "file_unstable",
                    promote_baseline,
                )
            return
        signature = self._signature(source_root, path, identity)
        ledger = self._require_ledger()
        status = ledger.signature_status(signature)
        if status is not None and not (status == "baseline" and promote_baseline):
            return

        validation_error = ""
        if identity.size == 0:
            validation_error = "empty_file"
        elif identity.size > self._config.max_file_bytes:
            validation_error = "file_too_large"
        if validation_error:
            ledger.register_validation_failure(
                signature=signature,
                source_root=source_root,
                source_path=path,
                identity=identity,
                error_category=validation_error,
                promote_baseline=promote_baseline,
                destination_space=binding.destination_space,
                destination_thread=binding.destination_thread,
            )
            return

        captured: tuple[Path, str] | None = None
        capture_error = "staging_failed"
        capture_delays = self._config.capture_retry_delays_seconds
        for capture_attempt in range(len(capture_delays) + 1):
            try:
                captured = (
                    self._staging_capture.capture_explicit_to_staging(
                        source_root,
                        path,
                        identity,
                    )
                    if binding.session_key
                    else self._staging_capture.capture_to_staging(path, identity)
                )
            except OSError:
                captured = None
            if captured is not None:
                break

            current = self._staging_capture.regular_identity(path)
            if current is None:
                capture_error = "source_unavailable"
            else:
                identity = current
                signature = self._signature(source_root, path, identity)
                capture_error = (
                    "empty_file"
                    if identity.size == 0
                    else "file_too_large"
                    if identity.size > self._config.max_file_bytes
                    else "staging_failed"
                )
            if capture_attempt < len(capture_delays):
                active_stop = self._active_stop
                if active_stop is None or active_stop.wait(
                    capture_delays[capture_attempt]
                ):
                    return

        if captured is None:
            self._register_capture_failure(
                binding,
                path,
                identity,
                capture_error,
                promote_baseline,
            )
            return
        staged_path, sha256 = captured
        registered = self._require_ledger().register_staged(
            signature=signature,
            source_root=source_root,
            source_path=path,
            identity=identity,
            sha256=sha256,
            staged_path=staged_path,
            promote_baseline=promote_baseline,
            destination_space=binding.destination_space,
            destination_thread=binding.destination_thread,
        )
        if not registered:
            self._unlink_quietly(staged_path)

    def _register_capture_failure(
        self,
        binding: _WatchBinding,
        path: Path,
        identity: _FileIdentity,
        error_category: str,
        promote_baseline: bool,
    ) -> None:
        self._require_ledger().register_validation_failure(
            signature=self._signature(binding.source_root, path, identity),
            source_root=binding.source_root,
            source_path=path,
            identity=identity,
            error_category=error_category,
            promote_baseline=promote_baseline,
            destination_space=binding.destination_space,
            destination_thread=binding.destination_thread,
        )

    def _wait_active_stop(self, seconds: float) -> bool:
        active_stop = self._active_stop
        return bool(active_stop and active_stop.wait(seconds))

    def _deliver_all_due(self) -> None:
        ledger = self._require_ledger()
        while not self._should_stop_active():
            record = ledger.claim_due()
            if record is None:
                return
            staged_path = record.attachment.staged_path
            has_remote_receipt = bool(record.attachment.web_view_link.strip())
            if (
                staged_path is None or not staged_path.is_file()
            ) and not has_remote_receipt:
                self._handle_delivery_failure(record, "staging_unavailable")
                continue

            try:
                callback_result = self._delivery_callback(record.attachment)
                delivery_result = self._normalize_delivery_result(callback_result)
            except Exception:  # noqa: BLE001
                delivery_result = OutboundDeliveryResult(DeliveryDisposition.FAILED)

            ledger.store_drive_receipt(
                record.record_id,
                delivery_result.drive_file_id,
                delivery_result.web_view_link,
            )
            disposition = delivery_result.disposition

            if disposition is DeliveryDisposition.DELIVERED:
                ledger.mark_delivered(record.record_id)
                if staged_path is not None:
                    self._unlink_quietly(staged_path)
                if staged_path is None or not staged_path.exists():
                    ledger.clear_staged_path(record.record_id)
            elif disposition is DeliveryDisposition.DEFERRED:
                ledger.mark_deferred(
                    record.record_id, self._config.deferral_delay_seconds
                )
            else:
                self._handle_delivery_failure(record, "delivery_failed")

    def _handle_delivery_failure(
        self, record: _DeliveryRecord, error_category: str
    ) -> None:
        next_attempt_number = record.attempts + 1
        retry_delay = self._retry_delay(next_attempt_number)
        self._require_ledger().mark_failed_attempt(
            record.record_id,
            error_category=error_category,
            max_attempts=self._config.max_delivery_attempts,
            retry_delay=retry_delay,
        )

    def _notify_all_due_failures(self) -> None:
        ledger = self._require_ledger()
        while not self._should_stop_active():
            record = ledger.next_failure_notification()
            if record is None:
                return
            failure = FinalDeliveryFailure(
                attachment=record.attachment,
                attempts=record.attempts,
                error_category=record.last_error_category,
            )
            try:
                self._final_failure_callback(failure)
            except Exception:  # noqa: BLE001
                ledger.defer_failure_notification(
                    record.record_id,
                    self._config.notification_retry_delay_seconds,
                )
                continue
            ledger.mark_failure_notified(record.record_id)
            staged_path = record.attachment.staged_path
            if staged_path is not None:
                self._unlink_quietly(staged_path)
                if not staged_path.exists():
                    ledger.clear_staged_path(record.record_id)

    def _retry_delay(self, attempt_number: int) -> float:
        delays = self._config.retry_delays_seconds
        if not delays:
            return 0
        return delays[min(max(attempt_number - 1, 0), len(delays) - 1)]

    def _cleanup_sent_staging(self) -> None:
        ledger = self._require_ledger()
        for record_id, staged_path in ledger.sent_staging_rows():
            self._unlink_quietly(staged_path)
            if not staged_path.exists():
                ledger.clear_staged_path(record_id)

    def _cleanup_orphaned_staging(self) -> None:
        staging_dir = self._config.state_dir / "staging"
        referenced = {
            path.resolve(strict=False)
            for path in self._require_ledger().referenced_staging_paths()
        }
        try:
            entries = list(staging_dir.iterdir())
        except FileNotFoundError:
            return
        for path in entries:
            if path.is_file() and path.resolve(strict=False) not in referenced:
                self._unlink_quietly(path)

    @staticmethod
    def _normalize_destination(space: str, thread: str) -> tuple[str, str]:
        if not isinstance(space, str) or not isinstance(thread, str):
            raise TypeError("explicit MEDIA destination names must be strings")
        normalized_space = space.strip()
        normalized_thread = thread.strip()
        if normalized_thread and not normalized_space:
            raise ValueError(
                "destination_space is required when destination_thread is provided"
            )
        if not normalized_space:
            return "", ""
        canonical_space = normalize_space_name(normalized_space)
        canonical_thread = (
            normalize_thread_name(canonical_space, normalized_thread)
            if normalized_thread
            else ""
        )
        return canonical_space, canonical_thread

    def _ensure_state_directories(self) -> None:
        self._config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._config.state_dir.chmod(0o700)
        staging_dir = self._config.state_dir / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_dir.chmod(0o700)

    def _ensure_thread_upload_root(self) -> None:
        upload_root = self._config.thread_upload_root
        if upload_root is None:
            raise RuntimeError("thread-scoped upload directories are not configured")
        ensure_real_directory(upload_root, label="thread upload root")

    def _ensure_thread_route_directory(self, source_root: Path) -> None:
        upload_root = self._config.thread_upload_root
        if upload_root is None or source_root.parent != upload_root:
            raise RuntimeError("thread upload directory is outside its configured root")
        self._ensure_thread_upload_root()
        ensure_real_directory(source_root, label="thread upload directory")

    def _thread_route_baseline_entries(
        self,
        route: _ThreadUploadRoute,
    ) -> list[tuple[str, Path, Path, _FileIdentity]]:
        entries: list[tuple[str, Path, Path, _FileIdentity]] = []
        try:
            children = tuple(route.source_root.iterdir())
        except FileNotFoundError:
            return entries
        for path in children:
            if self._should_ignore_name(path.name):
                continue
            try:
                file_stat = path.lstat()
            except (OSError, ValueError):
                continue
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                continue
            identity = identity_from_stat(file_stat)
            entries.append(
                (
                    self._signature(route.source_root, path, identity),
                    route.source_root,
                    path,
                    identity,
                )
            )
        return entries

    @staticmethod
    def _should_ignore_name(name: str) -> bool:
        lowered = name.lower()
        if not name or name.startswith((".", "~$")):
            return True
        if name.endswith("~"):
            return True
        temporary_markers = (
            ".tmp",
            ".part",
            ".partial",
            ".swp",
            ".swx",
            ".crdownload",
        )
        return any(
            lowered.endswith(marker) or f"{marker}." in lowered
            for marker in temporary_markers
        )

    @staticmethod
    def _signature(source_root: Path, path: Path, identity: _FileIdentity) -> str:
        payload = "\0".join(
            (
                str(source_root),
                path.name,
                str(identity.device),
                str(identity.inode),
                str(identity.size),
                str(identity.mtime_ns),
                str(identity.ctime_ns),
            )
        ).encode("utf-8", errors="surrogateescape")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _explicit_signature(idempotency_key: str) -> str:
        return hashlib.sha256(
            f"explicit-media-v1\0{idempotency_key}".encode()
        ).hexdigest()

    @staticmethod
    def _normalize_delivery_result(
        value: OutboundDeliveryResult | DeliveryDisposition | bool,
    ) -> OutboundDeliveryResult:
        if isinstance(value, OutboundDeliveryResult):
            return value
        if isinstance(value, DeliveryDisposition):
            return OutboundDeliveryResult(value)
        if value is True:
            return OutboundDeliveryResult(DeliveryDisposition.DELIVERED)
        if value is False:
            return OutboundDeliveryResult(DeliveryDisposition.FAILED)
        raise TypeError(
            "delivery callback must return OutboundDeliveryResult, "
            "DeliveryDisposition, or bool"
        )

    def _require_ledger(self) -> _Ledger:
        ledger = self._ledger
        if ledger is None:
            raise RuntimeError("outbound attachment ledger is not active")
        return ledger

    def _should_stop_active(self) -> bool:
        active_stop = self._active_stop
        return self._stop_event.is_set() or active_stop is None or active_stop.is_set()

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        with suppress(OSError):
            path.unlink(missing_ok=True)
