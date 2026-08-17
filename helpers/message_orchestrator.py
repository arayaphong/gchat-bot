from __future__ import annotations

import re
import subprocess
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from helpers.chat_gateway import ChatGateway
from helpers.inbound_message_store import InboundMessageLease, InboundMessageStore
from helpers.orchestrator_messages import (
    ABORT_FAILURE_TEMPLATE,
    ABORT_SUCCESS_TEXT,
    BUSY_TEXT,
    MODELS_FAILURE_TEMPLATE,
    NEW_THREAD_PREPARING_TEXT,
    NEW_THREAD_REDIRECT_TEXT,
    format_attachment_busy,
    format_attachment_command_ignored,
    format_attachment_download_failure,
    format_attachment_download_result,
    format_attachment_limit,
    format_attachment_remote_unavailable,
    format_models_summary,
    format_new_dm_session_failure,
    format_new_dm_session_success,
    format_new_session_failure,
    format_new_session_model_not_found,
    format_new_session_model_unavailable,
    format_new_session_model_validation_failure,
    format_new_session_success,
    format_schedule_add_success,
    format_schedule_cancel_success,
    format_schedule_failure,
    format_schedule_list,
)
from helpers.processing_gate import ProcessingGate, ProcessingGateError, ProcessingLease
from helpers.providers import OpenClawClient, OpenclawRunCancelled
from helpers.schedule_commands import (
    USAGE_TEXT as SCHEDULE_USAGE_TEXT,
    ScheduleCommandError,
    add_session_job,
    list_session_jobs,
    parse_schedule_args,
    remove_session_job,
    resolve_session_job,
)
from helpers.services import AttachmentService
from helpers.session_keys import ChatSessionContext
from helpers.session_manager import SessionManager
from helpers.session_trajectory_watcher import SessionTrajectoryWatcher

_NEW_COMMAND_RE = re.compile(r"^/new(?:[ \t]+(?P<model_key>\S+))?[ \t]*$")
_SCHEDULE_COMMAND_RE = re.compile(r"^/schedule(?:[ \t]+(?P<args>[\s\S]*))?[ \t]*$")


def _new_session_request_id(command_id: str, purpose: str = "") -> str | None:
    normalized_command_id = command_id.strip()
    if not normalized_command_id:
        return None
    name = f"gchat-bot:/new:{normalized_command_id}"
    if purpose:
        name = f"{name}:{purpose}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


class MessageOrchestrator:
    def __init__(
        self,
        gateway: ChatGateway,
        session_manager: SessionManager,
        attachment_service: AttachmentService,
        openclaw_client: OpenClawClient,
        max_attachments_per_message: int = 8,
        processing_gate: ProcessingGate | None = None,
        session_watcher: SessionTrajectoryWatcher | None = None,
        thread_upload_preparer: Callable[[ChatSessionContext], Path] | None = None,
        inbound_message_store: InboundMessageStore | None = None,
    ) -> None:
        if (
            not isinstance(max_attachments_per_message, int)
            or isinstance(max_attachments_per_message, bool)
            or max_attachments_per_message < 1
        ):
            raise ValueError("max_attachments_per_message must be a positive integer")
        self._gateway = gateway
        self._session_manager = session_manager
        self._attachment_service = attachment_service
        self._openclaw_client = openclaw_client
        self._max_attachments_per_message = max_attachments_per_message
        self._processing_gate = processing_gate or ProcessingGate()
        self._session_watcher = session_watcher
        self._thread_upload_preparer = thread_upload_preparer
        self._inbound_message_store = inbound_message_store
        self._session_transition_lock = threading.Lock()
        # Retain the original private lock alias for existing command/test
        # integrations while all production acquisitions go through the gate.
        self._processing_lock = self._processing_gate.local_lock
        self._bypass_commands: dict[str, Callable[[ChatSessionContext], None]] = {
            "/abort": self._handle_abort,
            "/models": self._handle_models,
        }
        # Creating a new deterministic context must never overlap a normal
        # turn. /abort remains a bypass so users can interrupt before retrying.
        self._locked_commands: dict[str, Callable[[ChatSessionContext, str], None]] = {
            "/new": self._handle_new_session,
        }

    @property
    def is_processing(self) -> bool:
        return self._processing_gate.is_locked

    def dispatch(
        self,
        space: str,
        thread: str,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
        quoted_message: dict[str, str] | None = None,
        command_id: str = "",
        *,
        context: ChatSessionContext,
    ) -> None:
        if not isinstance(context, ChatSessionContext):
            raise TypeError("context must be a ChatSessionContext")
        key_context = ChatSessionContext.from_session_key(context.session_key)
        if key_context.space != context.space or key_context.thread != context.thread:
            raise ValueError("context identity does not match its deterministic key")
        if space != context.space or thread != context.thread:
            raise ValueError("raw Google Chat target does not match context")
        active_context = context
        space = active_context.space
        thread = active_context.reply_thread

        inbound_message_lease: InboundMessageLease | None = None
        if self._inbound_message_store is not None:
            if not isinstance(command_id, str) or not command_id.strip():
                raise ValueError(
                    "authenticated Google Chat messages must include message.name"
                )
            if command_id != command_id.strip() or not command_id.startswith(
                f"{space}/messages/"
            ):
                raise ValueError("message.name does not match the authenticated space")
            inbound_message_lease = self._inbound_message_store.try_claim(command_id)

        if self._inbound_message_store is not None and inbound_message_lease is None:
            print(
                f"⏭️ [chat-in] skipped duplicate message={command_id!r} "
                f"(space={space}, thread={thread})"
            )
            return

        bypass_command = self._bypass_commands.get(text)
        if bypass_command:
            try:
                if attachments:
                    self._notify_ignored_attachments(space, thread, text, attachments)
                threading.Thread(
                    target=self._run_bypass_command,
                    args=(bypass_command, active_context, inbound_message_lease),
                    daemon=True,
                ).start()
            except BaseException:
                if inbound_message_lease is not None:
                    inbound_message_lease.release()
                raise
            return

        schedule_command_match = _SCHEDULE_COMMAND_RE.match(text)
        if schedule_command_match:
            # /schedule เรียกแค่ cron CLI ไม่แตะ agent session จึงไม่ต้องยึด
            # processing gate เหมือนคำสั่ง bypass อื่น
            schedule_args = schedule_command_match.group("args") or ""
            try:
                if attachments:
                    self._notify_ignored_attachments(space, thread, text, attachments)
                threading.Thread(
                    target=self._run_schedule_command,
                    args=(self, active_context, schedule_args, inbound_message_lease),
                    daemon=True,
                ).start()
            except BaseException:
                if inbound_message_lease is not None:
                    inbound_message_lease.release()
                raise
            return

        print(f"✅ [chat-in] accepted request (space={space}, thread={thread})")

        try:
            processing_lease = self._processing_gate.try_acquire()
        except ProcessingGateError as error:
            print(
                f"❌ [busy] shared processing gate failed: {type(error).__name__} "
                f"(space={space}, thread={thread})"
            )
            try:
                self._gateway.send_followup(space, thread, BUSY_TEXT, "jinx_system")
            except BaseException:
                if inbound_message_lease is not None:
                    inbound_message_lease.release()
                raise
            self._complete_inbound_message(inbound_message_lease)
            return

        if processing_lease is None:
            print(
                f"🚫 [busy] rejecting concurrent request (space={space}, thread={thread})"
            )
            busy_text = (
                format_attachment_busy(len(attachments)) if attachments else BUSY_TEXT
            )
            try:
                self._gateway.send_followup(space, thread, busy_text, "jinx_system")
            except BaseException:
                if inbound_message_lease is not None:
                    inbound_message_lease.release()
                raise
            self._complete_inbound_message(inbound_message_lease)
            return

        new_command_match = _NEW_COMMAND_RE.match(text)
        locked_command = self._locked_commands.get("/new") if new_command_match else None
        if locked_command:
            requested_model_key = new_command_match.group("model_key")
            if attachments:
                try:
                    self._notify_ignored_attachments(space, thread, text, attachments)
                except BaseException:
                    processing_lease.release()
                    if inbound_message_lease is not None:
                        inbound_message_lease.release()
                    raise
            self._start_processing_thread(
                self._run_locked_command,
                (
                    locked_command,
                    active_context,
                    command_id,
                    requested_model_key,
                    processing_lease,
                    inbound_message_lease,
                ),
                processing_lease,
                inbound_message_lease,
            )
            return

        self._start_processing_thread(
            self._handle_message,
            (
                active_context,
                user,
                text,
                attachments,
                quoted_message,
                processing_lease,
                inbound_message_lease,
            ),
            processing_lease,
            inbound_message_lease,
        )

    @staticmethod
    def _start_processing_thread(
        target: Callable[..., None],
        args: tuple[Any, ...],
        processing_lease: ProcessingLease,
        inbound_message_lease: InboundMessageLease | None = None,
    ) -> None:
        try:
            threading.Thread(target=target, args=args, daemon=True).start()
        except BaseException:
            processing_lease.release()
            if inbound_message_lease is not None:
                inbound_message_lease.release()
            raise

    @staticmethod
    def _run_bypass_command(
        command: Callable[[ChatSessionContext], None],
        context: ChatSessionContext,
        inbound_message_lease: InboundMessageLease | None,
    ) -> None:
        try:
            command(context)
        finally:
            MessageOrchestrator._complete_inbound_message(inbound_message_lease)

    @staticmethod
    def _run_schedule_command(
        orchestrator: MessageOrchestrator,
        context: ChatSessionContext,
        args: str,
        inbound_message_lease: InboundMessageLease | None,
    ) -> None:
        try:
            orchestrator._handle_schedule(context, args)
        finally:
            MessageOrchestrator._complete_inbound_message(inbound_message_lease)

    def _handle_schedule(self, context: ChatSessionContext, args: str) -> None:
        space = context.space
        thread = context.reply_thread
        session_key = context.session_key
        print(f"⏰ [schedule] dispatching /schedule {args!r} (space={space}, thread={thread})")
        try:
            parsed = parse_schedule_args(args)
            if parsed.kind == "usage":
                self._gateway.send_followup(
                    space, thread, SCHEDULE_USAGE_TEXT, "jinx_system"
                )
                return
            if parsed.kind == "list":
                jobs = list_session_jobs(session_key)
                print(
                    f"✅ [schedule] found {len(jobs)} job(s) for session={session_key!r} "
                    f"(space={space}, thread={thread})"
                )
                self._gateway.send_followup(
                    space, thread, format_schedule_list(jobs), "jinx_system"
                )
                return
            if parsed.kind == "cancel":
                job = resolve_session_job(parsed.job_ref, session_key)
                remove_session_job(str(job["id"]))
                print(
                    f"✅ [schedule] cancelled job={job['id']!r} "
                    f"(space={space}, thread={thread})"
                )
                self._gateway.send_followup(
                    space, thread, format_schedule_cancel_success(job), "jinx_system"
                )
                return
            if parsed.spec is None:
                raise TypeError("schedule add command is missing its spec")
            # The cron-fired reply is only delivered if SessionTrajectoryWatcher
            # is already tracking this session; register it before creating
            # the job so a /schedule sent as the thread's first message still
            # gets its reminder delivered.
            self._prepare_session_watcher_best_effort(context)
            job = add_session_job(parsed.spec, session_key)
            print(
                f"✅ [schedule] created job={job['id']!r} session={session_key!r} "
                f"(space={space}, thread={thread})"
            )
            self._gateway.send_followup(
                space,
                thread,
                format_schedule_add_success(job, parsed.spec.message),
                "jinx_system",
            )
        except ScheduleCommandError as error:
            print(f"❌ [schedule] invalid command: {error} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                f"{format_schedule_failure(error)}\n\n{SCHEDULE_USAGE_TEXT}",
                "jinx_system",
            )
        except FileNotFoundError:
            reason = "ไม่พบคำสั่ง openclaw"
            print(f"❌ [schedule] {reason} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space, thread, format_schedule_failure(reason), "jinx_system"
            )
        except subprocess.TimeoutExpired:
            reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
            print(f"❌ [schedule] {reason} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space, thread, format_schedule_failure(reason), "jinx_system"
            )
        except Exception as error:  # noqa: BLE001
            print(f"❌ [schedule] {error} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space, thread, format_schedule_failure(error), "jinx_system"
            )

    @staticmethod
    def _run_locked_command(
        command: Callable[[ChatSessionContext, str, str | None], None],
        context: ChatSessionContext,
        command_id: str,
        requested_model_key: str | None,
        processing_lease: ProcessingLease,
        inbound_message_lease: InboundMessageLease | None = None,
    ) -> None:
        try:
            command(context, command_id, requested_model_key)
        finally:
            try:
                MessageOrchestrator._complete_inbound_message(inbound_message_lease)
            finally:
                processing_lease.release()

    def _handle_message(
        self,
        context: ChatSessionContext,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
        quoted_message: dict[str, str] | None = None,
        processing_lease: ProcessingLease | None = None,
        inbound_message_lease: InboundMessageLease | None = None,
    ) -> None:
        space = context.space
        thread = context.reply_thread
        session_key = context.session_key
        files: list[dict[str, Any]] = []
        try:
            if attachments:
                selected_attachments = attachments[: self._max_attachments_per_message]
                ignored_attachments = attachments[self._max_attachments_per_message :]
                if ignored_attachments:
                    self._gateway.send_followup(
                        space,
                        thread,
                        format_attachment_limit(
                            len(attachments),
                            self._max_attachments_per_message,
                            self._attachment_names(ignored_attachments),
                        ),
                        "jinx_system",
                    )

                print(
                    f"📎 [attachment-in] downloading {len(selected_attachments)} file(s) "
                    f"(space={space}, thread={thread})"
                )
                try:
                    files = self._attachment_service.download_with_meta(
                        selected_attachments
                    )
                except Exception as error:  # noqa: BLE001
                    print(
                        f"❌ [attachment-in] failed to start downloads: "
                        f"{type(error).__name__} "
                        f"(space={space}, thread={thread})"
                    )
                    self._gateway.send_followup(
                        space,
                        thread,
                        format_attachment_download_failure(
                            self._attachment_names(selected_attachments)
                        ),
                        "jinx_system",
                    )
                    return

                succeeded_names, failed_downloads = self._download_outcome(files)
                if failed_downloads:
                    self._gateway.send_followup(
                        space,
                        thread,
                        format_attachment_download_result(
                            len(selected_attachments),
                            failed_downloads,
                        ),
                        "jinx_system",
                    )

                if succeeded_names:
                    try:
                        local_file_access = (
                            self._openclaw_client.has_local_file_access()
                        )
                    except ValueError as error:
                        print(
                            f"❌ [attachment-in] invalid local-file access policy: {error} "
                            f"(space={space}, thread={thread})"
                        )
                        local_file_access = False
                    if not local_file_access:
                        self._gateway.send_followup(
                            space,
                            thread,
                            format_attachment_remote_unavailable(succeeded_names),
                            "jinx_system",
                        )
                        return

            if self._thread_upload_preparer is not None:
                self._thread_upload_preparer(context)
            print(f"🤖 [provider-out] sending request (space={space}, thread={thread})")
            if self._session_watcher is not None:
                self._session_watcher.prepare_session(
                    session_key,
                    space,
                    context.thread,
                    thread,
                )
                # Register/reconcile the immutable route before the poller can
                # deliver any delayed output restored from disk.
                self._session_watcher.start()
            if inbound_message_lease is None:
                self._openclaw_client.send_turn(
                    text,
                    user,
                    files,
                    session_key,
                    quoted_message,
                )
            else:
                inbound_message_lease.begin_dispatch()
                self._openclaw_client.send_turn(
                    text,
                    user,
                    files,
                    session_key,
                    quoted_message,
                    idempotency_key=inbound_message_lease.idempotency_key,
                    resume_run_id=inbound_message_lease.run_id,
                    on_run_accepted=inbound_message_lease.record_run_id,
                )
            print(
                f"✅ [provider-out] run reached terminal success "
                f"(space={space}, thread={thread})"
            )

        except OpenclawRunCancelled as cancelled:
            print(
                f"🛑 [provider-out] run cancelled run={cancelled.run_id!r} "
                f"(space={space}, thread={thread})"
            )
        except Exception as e:  # noqa: BLE001
            print(f"❌ [error] {e} (space={space}, thread={thread})")
            error_text = str(e)
            if not error_text.lstrip().startswith("❌"):
                error_text = f"❌ {error_text}"
            self._gateway.send_followup(space, thread, error_text, "jinx_system")
        finally:
            try:
                self._complete_inbound_message(inbound_message_lease)
            finally:
                if processing_lease is not None:
                    processing_lease.release()
                else:
                    # Direct private-method tests historically acquire the
                    # in-process lock themselves.
                    self._processing_lock.release()

    @staticmethod
    def _complete_inbound_message(
        inbound_message_lease: InboundMessageLease | None,
    ) -> None:
        if inbound_message_lease is None:
            return
        try:
            inbound_message_lease.complete()
        finally:
            inbound_message_lease.release()

    @staticmethod
    def _attachment_names(attachments: list[dict[str, Any]]) -> list[str]:
        return [
            str(attachment.get("contentName") or "ไฟล์ไม่ทราบชื่อ")
            for attachment in attachments
        ]

    def _prepare_session_watcher_best_effort(
        self,
        context: ChatSessionContext,
    ) -> None:
        if self._session_watcher is None:
            return
        try:
            self._session_watcher.prepare_session(
                context.session_key,
                context.space,
                context.thread,
                context.reply_thread,
            )
            self._session_watcher.start()
        except Exception as error:  # noqa: BLE001
            print(
                "❌ [session-watch] cannot register watched session: "
                f"{type(error).__name__}: {error}"
            )

    @staticmethod
    def _download_outcome(
        files: list[dict[str, Any]],
    ) -> tuple[list[str], list[tuple[str, str]]]:
        safe_errors = {
            "too_large": "ไฟล์มีขนาดเกินขีดจำกัดของระบบ",
            "unsupported_reference": "รูปแบบแหล่งไฟล์ไม่รองรับ",
            "missing_after_download": "ไม่พบไฟล์หลังดาวน์โหลด",
            "download_failed": "ดาวน์โหลดไม่สำเร็จ",
        }
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        for item in files:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            name = str(meta.get("contentName") or "ไฟล์ไม่ทราบชื่อ")
            if item.get("fp"):
                succeeded.append(name)
                continue
            error_code = str(meta.get("errorCode") or "download_failed")
            failed.append(
                (name, safe_errors.get(error_code, safe_errors["download_failed"]))
            )
        return succeeded, failed

    def _notify_ignored_attachments(
        self,
        space: str,
        thread: str,
        command: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        self._gateway.send_followup(
            space,
            thread,
            format_attachment_command_ignored(
                command,
                self._attachment_names(attachments),
            ),
            "jinx_system",
        )

    def _handle_abort(self, context: ChatSessionContext) -> None:
        space = context.space
        thread = context.reply_thread
        print(f"🛑 [abort] triggering session abort (space={space}, thread={thread})")
        ok, reason = self._session_manager.abort(
            context.session_key,
            space=space,
            thread=thread,
        )
        if ok:
            self._gateway.send_followup(
                space, thread, ABORT_SUCCESS_TEXT, "jinx_system"
            )
        else:
            self._gateway.send_followup(
                space,
                thread,
                ABORT_FAILURE_TEMPLATE.format(reason=reason),
                "jinx_system",
            )

    def _resolve_requested_model(
        self,
        space: str,
        thread: str,
        requested_model_key: str,
        command_id: str,
    ) -> str | None:
        print(
            f"🔎 [new-session] validating requested model={requested_model_key!r} "
            f"(space={space}, thread={thread})"
        )
        try:
            models = self._openclaw_client.list_models()
            matches = [
                model
                for model in models
                if isinstance(model, dict)
                and isinstance(model.get("key"), str)
                and model["key"] == requested_model_key
            ]
            if len(matches) > 1:
                raise TypeError(f"พบ model key ซ้ำกัน: {requested_model_key}")
        except FileNotFoundError:
            reason = "ไม่พบคำสั่ง openclaw"
        except subprocess.TimeoutExpired:
            reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
        except Exception as e:  # noqa: BLE001
            reason = str(e)
        else:
            if not matches:
                self._send_new_session_notice(
                    space,
                    thread,
                    format_new_session_model_not_found(requested_model_key),
                    command_id,
                    "invalid-model",
                )
                return None

            model = matches[0]
            if model.get("available") is not True or model.get("missing") is True:
                self._send_new_session_notice(
                    space,
                    thread,
                    format_new_session_model_unavailable(requested_model_key),
                    command_id,
                    "invalid-model",
                )
                return None

            print(
                f"✅ [new-session] requested model={requested_model_key!r} validated "
                f"(space={space}, thread={thread})"
            )
            return requested_model_key

        print(
            f"❌ [new-session] model validation failed: {reason} "
            f"(space={space}, thread={thread})"
        )
        self._send_new_session_notice(
            space,
            thread,
            format_new_session_model_validation_failure(reason),
            command_id,
            "invalid-model",
        )
        return None

    def _handle_new_session(
        self,
        context: ChatSessionContext,
        command_id: str = "",
        requested_model_key: str | None = None,
    ) -> None:
        space = context.space
        thread = context.reply_thread
        resolved_model_key: str | None = None
        if requested_model_key is not None:
            resolved_model_key = self._resolve_requested_model(
                space, thread, requested_model_key, command_id
            )
            if resolved_model_key is None:
                return
        with self._session_transition_lock:
            if context.is_direct_message:
                print(
                    f"🆕 [new-session] resetting direct-message session "
                    f"(space={space}, session={context.session_key})"
                )
                try:
                    model_key = (
                        resolved_model_key
                        or self._openclaw_client.get_model_selection(
                            context.session_key
                        ).effective_model
                    )
                    reset_session_key = self._session_manager.reset(
                        context.session_key,
                        model_key,
                    )
                except FileNotFoundError:
                    reason = "ไม่พบคำสั่ง openclaw"
                except subprocess.TimeoutExpired:
                    reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
                except Exception as e:  # noqa: BLE001
                    reason = str(e)
                else:
                    try:
                        effective_model_key = self._openclaw_client.get_model_selection(
                            reset_session_key
                        ).effective_model
                    except Exception as error:  # noqa: BLE001
                        # The reset/create has already succeeded. A follow-up
                        # catalog read must not turn that success into a notice
                        # claiming the previous history is still active.
                        print(
                            "⚠️ [new-session] cannot refresh model after reset: "
                            f"{type(error).__name__}: {error}"
                        )
                        effective_model_key = model_key
                    self._prepare_session_watcher_best_effort(context)
                    print(
                        f"✅ [new-session] reset model={effective_model_key!r} "
                        f"session={reset_session_key!r} "
                        f"(space={space}, thread={thread})"
                    )
                    self._send_new_session_notice(
                        space,
                        thread,
                        format_new_dm_session_success(effective_model_key),
                        command_id,
                        "dm-success",
                    )
                    return

                print(
                    f"❌ [new-session] reset failed: {reason} "
                    f"(space={space}, session={context.session_key})"
                )
                self._send_new_session_notice(
                    space,
                    thread,
                    format_new_dm_session_failure(reason),
                    command_id,
                    "dm-failure",
                )
                return
            print(
                f"🆕 [new-session] triggering session reset "
                f"(space={space}, thread={thread})"
            )
            new_context: ChatSessionContext | None = None
            try:
                model_key = (
                    resolved_model_key
                    or self._openclaw_client.get_model_selection(
                        context.session_key
                    ).effective_model
                )

                root_request_id = _new_session_request_id(command_id) or str(
                    uuid.uuid4()
                )
                new_thread = self._gateway.create_root_thread(
                    space,
                    NEW_THREAD_PREPARING_TEXT,
                    "jinx_system",
                    request_id=root_request_id,
                )
                new_context = ChatSessionContext.for_thread(space, new_thread)
                new_session_key = self._session_manager.ensure_with_model(
                    new_context.session_key,
                    model_key,
                )
                target_model_key = self._openclaw_client.get_model_selection(
                    new_session_key
                ).effective_model
                # `/new` starts a separate deterministic context. Aborting the
                # source only stops a still-running turn; it does not replace
                # or delete that source session.
                self._session_manager.abort(
                    context.session_key,
                    space=space,
                    thread=thread,
                )
            except FileNotFoundError:
                reason = "ไม่พบคำสั่ง openclaw"
            except subprocess.TimeoutExpired:
                reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
            except Exception as e:  # noqa: BLE001
                reason = str(e)
            else:
                self._prepare_session_watcher_best_effort(new_context)
                print(
                    f"✅ [new-session] created model={target_model_key!r} "
                    f"session={new_session_key!r} "
                    f"(space={new_context.space}, thread={new_context.reply_thread})"
                )
                self._send_new_session_notice(
                    new_context.space,
                    new_context.reply_thread,
                    format_new_session_success(target_model_key),
                    command_id,
                    "success",
                )
                self._send_new_session_notice(
                    space,
                    thread,
                    NEW_THREAD_REDIRECT_TEXT,
                    command_id,
                    "redirect",
                )
                return

            print(
                f"❌ [new-session] creation failed: {reason} "
                f"(space={space}, thread={thread})"
            )
            self._send_new_session_notice(
                space,
                thread,
                format_new_session_failure(reason),
                command_id,
                "failure:source",
            )
            if new_context is not None:
                self._send_new_session_notice(
                    new_context.space,
                    new_context.reply_thread,
                    format_new_session_failure(reason),
                    command_id,
                    "failure:target",
                )

    def _send_new_session_notice(
        self,
        space: str,
        thread: str,
        text: str,
        command_id: str,
        purpose: str,
    ) -> bool:
        request_id = _new_session_request_id(command_id, purpose)
        if request_id is None:
            return self._gateway.send_followup(space, thread, text, "jinx_system")
        return self._gateway.send_followup(
            space,
            thread,
            text,
            "jinx_system",
            request_id=request_id,
        )

    def _handle_models(self, context: ChatSessionContext) -> None:
        space = context.space
        thread = context.reply_thread
        print(f"📚 [models] listing configured models (space={space}, thread={thread})")
        try:
            session_key = context.session_key
            models = self._openclaw_client.list_models()
            model_selection = self._openclaw_client.get_model_selection(session_key)

            summary = format_models_summary(
                models,
                default_model=model_selection.default_model,
                current_session_model=model_selection.session_model or "—",
            )
            print(
                f"✅ [models] found {len(models)} model(s) "
                f"for session={session_key!r} (space={space}, thread={thread})"
            )
            self._gateway.send_followup(space, thread, summary, "jinx_system")
        except FileNotFoundError:
            reason = "ไม่พบคำสั่ง openclaw"
            print(f"❌ [models] {reason} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                MODELS_FAILURE_TEMPLATE.format(reason=reason),
                "jinx_system",
            )
        except subprocess.TimeoutExpired:
            reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
            print(f"❌ [models] {reason} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                MODELS_FAILURE_TEMPLATE.format(reason=reason),
                "jinx_system",
            )
        except Exception as e:  # noqa: BLE001
            print(f"❌ [models] {e} (space={space}, thread={thread})")
            self._gateway.send_followup(
                space,
                thread,
                MODELS_FAILURE_TEMPLATE.format(reason=e),
                "jinx_system",
            )
