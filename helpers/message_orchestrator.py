from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from typing import Any

from helpers.chat_gateway import ChatGateway
from helpers.model_commands import is_model_command, parse_model_key
from helpers.orchestrator_messages import (
    ABORT_FAILURE_TEMPLATE,
    ABORT_SUCCESS_TEXT,
    BUSY_TEXT,
    MODEL_COMMAND_USAGE_TEXT,
    MODELS_FAILURE_TEMPLATE,
    format_attachment_busy,
    format_attachment_cleanup,
    format_attachment_command_ignored,
    format_attachment_download_failure,
    format_attachment_download_result,
    format_attachment_limit,
    format_attachment_remote_unavailable,
    format_model_not_found,
    format_model_session_failure,
    format_model_session_success,
    format_model_unavailable,
    format_model_validation_failure,
    format_models_summary,
    format_new_session_failure,
    format_new_session_success,
)
from helpers.processing_gate import ProcessingGate, ProcessingGateError, ProcessingLease
from helpers.providers import OpenClawClient, ProviderSettings
from helpers.services import AttachmentService
from helpers.session_manager import SessionManager
from helpers.session_trajectory_watcher import SessionTrajectoryWatcher


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
        self._session_transition_lock = threading.Lock()
        # Retain the original private lock alias for existing command/test
        # integrations while all production acquisitions go through the gate.
        self._processing_lock = self._processing_gate.local_lock
        self._bypass_commands: dict[str, Callable[[str, str], None]] = {
            "/abort": self._handle_abort,
            "/models": self._handle_models,
            "/new": self._handle_new_session,
        }
        self._locked_commands: dict[str, Callable[[str, str], None]] = {}

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
    ) -> None:
        bypass_command = self._bypass_commands.get(text)
        if bypass_command:
            if attachments:
                self._notify_ignored_attachments(space, thread, text, attachments)
            threading.Thread(
                target=bypass_command, args=(space, thread), daemon=True
            ).start()
            return

        print(f"✅ [chat-in] accepted request (space={space}, thread={thread})")

        try:
            processing_lease = self._processing_gate.try_acquire()
        except ProcessingGateError as error:
            print(
                f"❌ [busy] shared processing gate failed: {type(error).__name__} "
                f"(space={space}, thread={thread})"
            )
            self._gateway.send_followup(space, thread, BUSY_TEXT, "jinx_system")
            return

        if processing_lease is None:
            print(
                f"🚫 [busy] rejecting concurrent request (space={space}, thread={thread})"
            )
            busy_text = (
                format_attachment_busy(len(attachments)) if attachments else BUSY_TEXT
            )
            self._gateway.send_followup(space, thread, busy_text, "jinx_system")
            return

        locked_command = self._locked_commands.get(text)
        if locked_command:
            self._start_processing_thread(
                self._run_locked_command,
                (locked_command, space, thread, processing_lease),
                processing_lease,
            )
            return

        settings = self._session_manager.settings
        self._start_processing_thread(
            self._handle_message,
            (
                space,
                thread,
                user,
                text,
                attachments,
                settings,
                quoted_message,
                processing_lease,
            ),
            processing_lease,
        )

    @staticmethod
    def _start_processing_thread(
        target: Callable[..., None],
        args: tuple[Any, ...],
        processing_lease: ProcessingLease,
    ) -> None:
        try:
            threading.Thread(target=target, args=args, daemon=True).start()
        except BaseException:
            processing_lease.release()
            raise

    @staticmethod
    def _run_locked_command(
        command: Callable[[str, str], None],
        space: str,
        thread: str,
        processing_lease: ProcessingLease,
    ) -> None:
        try:
            command(space, thread)
        finally:
            processing_lease.release()

    def _handle_message(
        self,
        space: str,
        thread: str,
        user: str,
        text: str,
        attachments: list[dict[str, Any]],
        settings: ProviderSettings,
        quoted_message: dict[str, str] | None = None,
        processing_lease: ProcessingLease | None = None,
    ) -> None:
        files: list[dict[str, Any]] = []
        try:
            if is_model_command(text):
                self._handle_model_command(space, thread, text, attachments)
                return
            elif attachments:
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

            print(f"🤖 [provider-out] sending request (space={space}, thread={thread})")
            if self._session_watcher is not None:
                self._session_watcher.start()
                self._session_watcher.prepare_session(settings.openclaw_session_key)
            self._openclaw_client.send_turn(
                text,
                user,
                files,
                settings.openclaw_session_key,
                quoted_message,
            )
            print(
                f"✅ [provider-out] request completed; response body ignored "
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
                self._cleanup_attachments(space, thread, files)
            finally:
                if processing_lease is not None:
                    processing_lease.release()
                else:
                    # Direct private-method tests historically acquire the
                    # in-process lock themselves.
                    self._processing_lock.release()

    def _handle_model_command(
        self,
        space: str,
        thread: str,
        text: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        with self._session_transition_lock:
            if attachments:
                self._notify_ignored_attachments(space, thread, text, attachments)
            model_key = self._validate_model_command(space, thread, text)
            if model_key is None:
                return

            print(
                f"🆕 [model-session] creating model={model_key!r} "
                f"(space={space}, thread={thread})"
            )
            try:
                new_settings = self._session_manager.rotate_with_model(model_key)
            except FileNotFoundError:
                reason = "ไม่พบคำสั่ง openclaw"
            except subprocess.TimeoutExpired:
                reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
            except Exception as e:  # noqa: BLE001
                reason = str(e)
            else:
                self._prepare_session_watcher_best_effort(
                    new_settings.openclaw_session_key
                )
                print(
                    f"✅ [model-session] created model={model_key!r} "
                    f"session={new_settings.openclaw_session_key!r} "
                    f"(space={space}, thread={thread})"
                )
                self._gateway.send_followup(
                    space,
                    thread,
                    format_model_session_success(model_key),
                    "jinx_system",
                )
                return

            print(
                f"❌ [model-session] creation failed: {reason} "
                f"(space={space}, thread={thread})"
            )
            self._gateway.send_followup(
                space,
                thread,
                format_model_session_failure(model_key, reason),
                "jinx_system",
            )

    @staticmethod
    def _attachment_names(attachments: list[dict[str, Any]]) -> list[str]:
        return [
            str(attachment.get("contentName") or "ไฟล์ไม่ทราบชื่อ")
            for attachment in attachments
        ]

    def _prepare_session_watcher_best_effort(self, session_key: str) -> None:
        if self._session_watcher is None:
            return
        try:
            self._session_watcher.start()
            self._session_watcher.prepare_session(session_key)
        except Exception as error:  # noqa: BLE001
            print(
                "❌ [session-watch] cannot switch watched session: "
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

    def _cleanup_attachments(
        self,
        space: str,
        thread: str,
        files: list[dict[str, Any]],
    ) -> None:
        if not files:
            return
        try:
            report = self._attachment_service.cleanup(files)
            cleaned = int(report.get("removed", 0))
            raw_failures = report.get("failed", [])
            failures = [
                (
                    str(item.get("name") or "ไฟล์ชั่วคราว"),
                    str(item.get("error") or "ลบไม่สำเร็จ"),
                )
                for item in raw_failures
                if isinstance(item, dict)
            ]
            if failures:
                self._gateway.send_followup(
                    space,
                    thread,
                    format_attachment_cleanup(cleaned, failures),
                    "jinx_system",
                )
        except Exception as error:  # noqa: BLE001
            print(f"❌ [attachment-cleanup] {error} (space={space}, thread={thread})")
            names = [
                str(item.get("meta", {}).get("contentName") or "ไฟล์ชั่วคราว")
                for item in files
                if item.get("fp")
            ]
            failures = [(name, "ลบไม่สำเร็จ") for name in names] or [
                ("ไฟล์ชั่วคราว", "ลบไม่สำเร็จ")
            ]
            self._gateway.send_followup(
                space,
                thread,
                format_attachment_cleanup(0, failures),
                "jinx_system",
            )

    def _validate_model_command(self, space: str, thread: str, text: str) -> str | None:
        model_key = parse_model_key(text)
        if model_key is None:
            self._gateway.send_followup(
                space, thread, MODEL_COMMAND_USAGE_TEXT, "jinx_system"
            )
            return None

        print(
            f"🔎 [model] validating model={model_key!r} "
            f"(space={space}, thread={thread})"
        )
        try:
            models = self._openclaw_client.list_models()
            matches = [
                model
                for model in models
                if isinstance(model, dict)
                and isinstance(model.get("key"), str)
                and model["key"] == model_key
            ]
            if len(matches) > 1:
                raise TypeError(f"พบ model key ซ้ำกัน: {model_key}")
        except FileNotFoundError:
            reason = "ไม่พบคำสั่ง openclaw"
        except subprocess.TimeoutExpired:
            reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
        except Exception as e:  # noqa: BLE001
            reason = str(e)
        else:
            if not matches:
                self._gateway.send_followup(
                    space,
                    thread,
                    format_model_not_found(model_key),
                    "jinx_system",
                )
                return None

            model = matches[0]
            if model.get("available") is not True or model.get("missing") is True:
                self._gateway.send_followup(
                    space,
                    thread,
                    format_model_unavailable(model_key),
                    "jinx_system",
                )
                return None

            print(
                f"✅ [model] validated model={model_key!r} "
                f"(space={space}, thread={thread})"
            )
            return model_key

        print(
            f"❌ [model] validation failed: {reason} (space={space}, thread={thread})"
        )
        self._gateway.send_followup(
            space,
            thread,
            format_model_validation_failure(reason),
            "jinx_system",
        )
        return None

    def _handle_abort(self, space: str, thread: str) -> None:
        print(f"🛑 [abort] triggering session abort (space={space}, thread={thread})")
        ok, reason = self._session_manager.abort_current(space, thread)
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

    def _handle_new_session(self, space: str, thread: str) -> None:
        with self._session_transition_lock:
            print(
                f"🆕 [new-session] triggering session reset "
                f"(space={space}, thread={thread})"
            )
            try:
                current_session_key = (
                    self._session_manager.settings.openclaw_session_key
                )
                model_key = self._openclaw_client.get_model_selection(
                    current_session_key
                ).effective_model
                self._session_manager.abort_current(space, thread)
                new_settings = self._session_manager.rotate_with_model(model_key)
            except FileNotFoundError:
                reason = "ไม่พบคำสั่ง openclaw"
            except subprocess.TimeoutExpired:
                reason = "คำสั่ง openclaw ใช้เวลานานเกินกำหนด"
            except Exception as e:  # noqa: BLE001
                reason = str(e)
            else:
                self._prepare_session_watcher_best_effort(
                    new_settings.openclaw_session_key
                )
                print(
                    f"✅ [new-session] created model={model_key!r} "
                    f"session={new_settings.openclaw_session_key!r} "
                    f"(space={space}, thread={thread})"
                )
                self._gateway.send_followup(
                    space,
                    thread,
                    format_new_session_success(model_key),
                    "jinx_system",
                )
                return

            print(
                f"❌ [new-session] creation failed: {reason} "
                f"(space={space}, thread={thread})"
            )
            self._gateway.send_followup(
                space,
                thread,
                format_new_session_failure(reason),
                "jinx_system",
            )

    def _handle_models(self, space: str, thread: str) -> None:
        print(f"📚 [models] listing configured models (space={space}, thread={thread})")
        try:
            session_key = self._session_manager.settings.openclaw_session_key
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
