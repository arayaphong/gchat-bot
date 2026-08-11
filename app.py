from __future__ import annotations

import atexit
import hashlib
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request

from helpers.chat_clear_confirmation import ChatClearCoordinator, ChatClearPresenter
from helpers.chat_clear_store import (
    ChatClearStore,
    ChatClearStoreError,
    ChatWritePacer,
    ClearJobRequest,
    JobStatus,
)
from helpers.chat_events import (
    ChatEventKind,
    ChatEventValidationError,
    normalize_chat_event,
)
from helpers.chat_gateway import ChatGateway
from helpers.chat_history_client import ChatHistoryClient, ChatHistoryClientError
from helpers.chat_history_delete import (
    BotChatDeleteClient,
    ChatHistoryDeleteExecutor,
    UserChatDeleteClient,
)
from helpers.chat_history_service import ChatHistoryService
from helpers.chat_history_settings import (
    ChatHistorySettings,
    ChatHistorySettingsError,
    default_chat_history_state_dir,
)
from helpers.chat_history_time import (
    HistoryCommandError,
    HistoryCommandKind,
    parse_history_command,
    recognize_history_command,
)
from helpers.chat_history_worker import ChatHistoryWorkerSupervisor
from helpers.chat_target_store import (
    ChatTargetConflictError,
    ChatTargetError,
    FixedChatTargetStore,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.google_scopes import BOT_SCOPES, USER_SCOPES
from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import format_outbound_attachment_failure
from helpers.outbound_attachment_watcher import (
    AttachmentSubmissionDisposition,
    DeliveryDisposition,
    FinalDeliveryFailure,
    OutboundAttachment,
    OutboundAttachmentConfig,
    OutboundAttachmentService,
    OutboundDeliveryResult,
)
from helpers.processing_gate import ProcessingGate, ProcessingGateError
from helpers.providers import OpenClawClient, ProviderSettings
from helpers.services import (
    AttachmentService,
    CardPresenter,
    ChatAuthSettings,
    ChatAuthVerifier,
    CredentialService,
)
from helpers.session_manager import SessionManager
from helpers.session_trajectory_watcher import (
    AssistantTrajectoryMessage,
    SessionTrajectoryWatcher,
)

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
BOT_CRED = Path(os.environ.get("GCHAT_BOT_CRED", str(BASE_DIR / "credentials.json")))
TOKEN_FILE = Path(os.environ.get("GCHAT_TOKEN_FILE", str(BASE_DIR / "token.json")))
DOWNLOAD_DIR = Path.home() / ".openclaw" / "workspace" / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTBOUND_UPLOAD_DIR = Path.home() / ".openclaw" / "workspace" / "uploads"
DRIVE_UPLOAD_FOLDER_ID = os.environ.get("DRIVE_UPLOAD_FOLDER_ID")

MAX_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "8"))
MAX_OUTBOUND_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_OUTBOUND_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)

SESSION_KEY_FILE = BASE_DIR / "session_key"
CHAT_IN_LOG_FILE = BASE_DIR / "chat-in.jsonl"
CHAT_OUT_LOG_FILE = BASE_DIR / "chat-out.jsonl"
OUTBOUND_STATE_DIR = Path(
    os.environ.get(
        "JINX_OUTBOUND_STATE_DIR",
        str(Path.home() / ".openclaw" / "state" / "jinx-gchat"),
    )
).expanduser()
CHAT_TARGET_FILE = Path(
    os.environ.get(
        "GCHAT_OUTBOUND_TARGET_FILE",
        str(OUTBOUND_STATE_DIR / "target.json"),
    )
).expanduser()
OUTBOUND_ATTACHMENT_CONFIG = OutboundAttachmentConfig(
    # MEDIA: directives may reference any file under the home directory or /tmp;
    # only the uploads directory is auto-watched for new files.
    source_dirs=(Path.home(), Path("/tmp")),
    watched_source_dirs=(OUTBOUND_UPLOAD_DIR,),
    state_dir=OUTBOUND_STATE_DIR / "attachments",
    max_file_bytes=MAX_OUTBOUND_ATTACHMENT_BYTES,
    # Never allow MEDIA: to exfiltrate the bot's own credentials/session secrets.
    blocked_files=(BOT_CRED, TOKEN_FILE, SESSION_KEY_FILE),
)
OUTBOUND_STAGING_DIR = OUTBOUND_ATTACHMENT_CONFIG.state_dir / "staging"
PROCESSING_GATE_FILE = OUTBOUND_STATE_DIR / "processing.lock"

auth_settings = ChatAuthSettings.from_env()
processing_gate = ProcessingGate(PROCESSING_GATE_FILE)
target_store = FixedChatTargetStore(
    state_file=CHAT_TARGET_FILE,
    configured_space=os.environ.get("GCHAT_OUTBOUND_SPACE", ""),
    configured_thread=os.environ.get("GCHAT_OUTBOUND_THREAD", ""),
)

credential_service = CredentialService(
    bot_cred=BOT_CRED,
    token_file=TOKEN_FILE,
    scopes_bot=BOT_SCOPES,
    scopes_user=USER_SCOPES,
)
auth_verifier = ChatAuthVerifier(auth_settings)
try:
    history_settings: ChatHistorySettings | None = ChatHistorySettings.from_env()
except ChatHistorySettingsError as error:
    history_settings = None
    print(f"❌ [chat-history] configuration disabled: {type(error).__name__}")
attachment_service = AttachmentService(
    download_dir=DOWNLOAD_DIR,
    max_attachment_bytes=MAX_ATTACHMENT_BYTES,
    credential_service=credential_service,
)
card_presenter = CardPresenter()

send_file_policy = SendableFilePolicy(allowed_roots=[OUTBOUND_STAGING_DIR])
history_state_dir = (
    history_settings.state_dir
    if history_settings is not None
    else default_chat_history_state_dir()
)
chat_clear_store = ChatClearStore(history_state_dir)
chat_write_pacer = ChatWritePacer(history_state_dir)
gateway = ChatGateway(
    credential_service=credential_service,
    card_presenter=card_presenter,
    chat_in_log=CHAT_IN_LOG_FILE,
    chat_out_log=CHAT_OUT_LOG_FILE,
    file_policy=send_file_policy,
    drive_folder_id=DRIVE_UPLOAD_FOLDER_ID,
    write_pacer=chat_write_pacer,
)
history_service: ChatHistoryService | None = None
history_client: ChatHistoryClient | None = None
if (
    history_settings is not None
    and history_settings.enabled
    and history_settings.allowed_user is not None
    and history_settings.allowed_space is not None
):
    history_client = ChatHistoryClient(
        credential_service,
        allowed_user=history_settings.allowed_user,
        allowed_space=history_settings.allowed_space,
    )
    history_service = ChatHistoryService(history_client, gateway)
history_clear_presenter = ChatClearPresenter()
history_clear_coordinator: ChatClearCoordinator | None = None
history_delete_executor: ChatHistoryDeleteExecutor | None = None
history_delete_ready = False
if (
    history_settings is not None
    and history_settings.enabled
    and history_settings.delete_enabled
):
    try:
        chat_clear_store.preflight(delete_execution=True)
        history_delete_ready = True
    except ChatClearStoreError as error:
        print(
            "❌ [chat-history] delete execution disabled: "
            f"{type(error).__name__}"
        )
if (
    history_settings is not None
    and history_settings.enabled
    and history_settings.delete_enabled
    and history_delete_ready
    and history_settings.card_action_url is not None
    and history_client is not None
):
    history_clear_coordinator = ChatClearCoordinator(
        chat_clear_store,
        history_client,
        gateway,
        action_url=history_settings.card_action_url,
        ttl_seconds=history_settings.confirmation_ttl_seconds,
        presenter=history_clear_presenter,
    )
    user_delete_client = UserChatDeleteClient(
        credential_service,
        allowed_space=history_settings.allowed_space,
    )
    bot_delete_client = BotChatDeleteClient(
        credential_service,
        allowed_space=history_settings.allowed_space,
    )
    history_delete_executor = ChatHistoryDeleteExecutor(
        chat_clear_store,
        user_delete_client,
        bot_delete_client,
        gateway,
        chat_write_pacer,
        delete_enabled=lambda: bool(
            history_settings is not None
            and history_settings.enabled
            and history_settings.delete_enabled
            and history_delete_ready
        ),
    )
history_worker = (
    ChatHistoryWorkerSupervisor(
        chat_clear_store,
        preview_handler=(
            history_clear_coordinator.handle_preview
            if history_clear_coordinator is not None
            else None
        ),
        confirmation_cleanup_handler=(
            history_clear_coordinator.handle_confirmation_cleanup
            if history_clear_coordinator is not None
            else None
        ),
        delete_handler=(
            history_delete_executor.handle_delete
            if history_delete_executor is not None
            else None
        ),
        delete_enabled=lambda: bool(
            history_settings is not None
            and history_settings.enabled
            and history_settings.delete_enabled
            and history_delete_ready
        ),
        final_notification_handler=(
            history_delete_executor.handle_final_notification
            if history_delete_executor is not None
            else None
        ),
    )
    if history_settings is not None and history_settings.enabled
    else None
)
provider_settings = ProviderSettings.from_env()
openclaw_client = OpenClawClient(
    agent=provider_settings.openclaw_agent,
    base_url=provider_settings.openclaw_base_url,
    model=provider_settings.openclaw_model,
)
session_manager = SessionManager(
    session_key_file=SESSION_KEY_FILE,
    initial_settings=provider_settings,
    openclaw_client=openclaw_client,
)


def _deliver_session_message(message: AssistantTrajectoryMessage) -> bool:
    try:
        target = target_store.get()
    except ChatTargetError as error:
        print(
            f"❌ [session-watch] cannot load fixed Chat target: {type(error).__name__}"
        )
        return False
    if target is None:
        return False
    if message.text and not gateway.send_followup(
        target.space,
        target.thread,
        message.text,
        "openclaw",
        request_id=message.delivery_id,
    ):
        return False

    for ordinal, media_path in enumerate(message.media_paths):
        try:
            submission = outbound_attachment_service.submit_explicit(
                media_path,
                idempotency_key=(f"jinx-session-media:{message.delivery_id}:{ordinal}"),
            )
        except Exception as error:  # noqa: BLE001
            print(
                "❌ [session-watch] MEDIA submission failed: "
                f"{type(error).__name__} ordinal={ordinal}"
            )
            return False
        if submission.disposition is AttachmentSubmissionDisposition.UNAVAILABLE:
            print(
                "❌ [session-watch] MEDIA ingress unavailable "
                f"ordinal={ordinal} category={submission.error_category!r}"
            )
            return False
        if submission.disposition is AttachmentSubmissionDisposition.REJECTED:
            print(
                "🚫 [session-watch] MEDIA path rejected "
                f"ordinal={ordinal} category={submission.error_category!r}"
            )
            rejection_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (f"jinx-session-media-rejected:{message.delivery_id}:{ordinal}"),
                )
            )
            if not gateway.send_followup(
                target.space,
                target.thread,
                format_outbound_attachment_failure(
                    "ไฟล์ที่ OpenClaw ระบุ",
                    0,
                    submission.error_category,
                ),
                "jinx_system",
                request_id=rejection_id,
            ):
                return False
    return True


session_message_watcher = SessionTrajectoryWatcher(
    delivery_callback=_deliver_session_message,
)
orchestrator = MessageOrchestrator(
    gateway=gateway,
    session_manager=session_manager,
    attachment_service=attachment_service,
    openclaw_client=openclaw_client,
    max_attachments_per_message=MAX_ATTACHMENTS_PER_MESSAGE,
    processing_gate=processing_gate,
    session_watcher=session_message_watcher,
)


def _stop_session_message_watcher() -> None:
    session_message_watcher.stop()


atexit.register(_stop_session_message_watcher)


def _deliver_outbound_attachment(
    attachment: OutboundAttachment,
) -> OutboundDeliveryResult | DeliveryDisposition:
    try:
        processing_lease = processing_gate.try_acquire()
    except ProcessingGateError as error:
        print(
            f"❌ [attachment-out] cannot acquire processing gate: "
            f"{type(error).__name__}"
        )
        return DeliveryDisposition.DEFERRED
    if processing_lease is None:
        return DeliveryDisposition.DEFERRED

    with processing_lease:
        try:
            target = target_store.get()
        except ChatTargetError as error:
            print(
                f"❌ [attachment-out] cannot load fixed Chat target: "
                f"{type(error).__name__}"
            )
            return DeliveryDisposition.DEFERRED
        if target is None:
            return DeliveryDisposition.DEFERRED
        if attachment.staged_path is None and not attachment.web_view_link:
            return DeliveryDisposition.FAILED
        delivery_path = attachment.staged_path or attachment.source_path

        result = gateway.send_file(
            target.space,
            target.thread,
            delivery_path,
            filename=attachment.display_name,
            request_id=attachment.delivery_id or None,
            drive_file_id=attachment.drive_file_id,
            web_view_link=attachment.web_view_link,
        )
        if not result.success:
            error_name = type(result.error).__name__ if result.error else "UnknownError"
            print(
                "❌ [attachment-out] delivery attempt failed "
                f"name={attachment.display_name!r} error={error_name}"
            )
            return OutboundDeliveryResult(
                DeliveryDisposition.FAILED,
                drive_file_id=result.drive_file_id,
                web_view_link=result.web_view_link,
            )

        print(
            "✅ [attachment-out] delivered file "
            f"name={attachment.display_name!r} thread={target.thread}"
        )
        return OutboundDeliveryResult(
            DeliveryDisposition.DELIVERED,
            drive_file_id=result.drive_file_id,
            web_view_link=result.web_view_link,
        )


def _notify_outbound_attachment_failure(failure: FinalDeliveryFailure) -> None:
    try:
        processing_lease = processing_gate.try_acquire()
    except ProcessingGateError as error:
        raise RuntimeError("shared processing gate is unavailable") from error
    if processing_lease is None:
        raise RuntimeError("shared processing gate is busy")

    with processing_lease:
        target = target_store.get()
        if target is None:
            raise RuntimeError("fixed Chat target is not available")
        message = format_outbound_attachment_failure(
            failure.attachment.display_name,
            failure.attempts,
            failure.error_category,
        )
        request_id = (
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"jinx-outbound-failure:{failure.attachment.delivery_id}",
                )
            )
            if failure.attachment.delivery_id
            else None
        )
        if not gateway.send_followup(
            target.space,
            target.thread,
            message,
            "jinx_system",
            request_id=request_id,
        ):
            raise RuntimeError("Google Chat rejected the failure notification")


outbound_attachment_service = OutboundAttachmentService(
    delivery_callback=_deliver_outbound_attachment,
    final_failure_callback=_notify_outbound_attachment_failure,
    config=OUTBOUND_ATTACHMENT_CONFIG,
)

_outbound_start_lock = threading.Lock()
_outbound_start_initialized = False
_outbound_start_error: Exception | None = None


def _start_outbound_attachment_service() -> bool:
    global _outbound_start_error, _outbound_start_initialized

    with _outbound_start_lock:
        if not _outbound_start_initialized:
            try:
                outbound_attachment_service.start()
                outbound_attachment_service.wait_until_active(timeout=5)
            except Exception as error:  # noqa: BLE001
                _outbound_start_error = error
            finally:
                _outbound_start_initialized = True

    error = _outbound_start_error or outbound_attachment_service.last_start_error
    return outbound_attachment_service.is_active or error is None


def _stop_outbound_attachment_service() -> None:
    try:
        # One in-flight attempt can spend up to 60s in Drive and 15s in Chat.
        outbound_attachment_service.stop(timeout=90)
    except Exception as error:  # noqa: BLE001
        print(f"❌ [attachment-out] watcher shutdown failed: {type(error).__name__}")


atexit.register(_stop_outbound_attachment_service)


def _start_chat_history_worker() -> bool:
    if history_worker is None:
        return True
    try:
        history_worker.start()
        return history_worker.wait_until_active(timeout=5)
    except Exception as error:  # noqa: BLE001
        print(f"❌ [chat-history] worker startup failed: {type(error).__name__}")
        return False


def _stop_chat_history_worker() -> None:
    if history_worker is None:
        return
    try:
        history_worker.stop(timeout=10)
    except Exception as error:  # noqa: BLE001
        print(f"❌ [chat-history] worker shutdown failed: {type(error).__name__}")


atexit.register(_stop_chat_history_worker)


@app.route("/chat", methods=["POST"])
def chat() -> tuple[Response, int]:
    if not auth_verifier.verify(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        gateway.record_incoming(
            {"eventType": "REJECTED", "errorCategory": "invalid_json"}
        )
        return jsonify({"error": "invalid JSON body"}), 400

    try:
        event = normalize_chat_event(data)
    except ChatEventValidationError as error:
        gateway.record_incoming(
            {
                "eventType": "REJECTED",
                "errorCategory": error.code.value,
                "fieldPath": error.field_path,
            }
        )
        return jsonify({"error": "invalid Chat event"}), 400

    if event.kind is ChatEventKind.UNKNOWN:
        gateway.record_incoming(
            {"eventType": "UNKNOWN", "errorCategory": "unsupported_event"}
        )
        return jsonify(gateway.ack()), 200

    gateway.record_incoming(data)

    if event.kind is ChatEventKind.BUTTON_CLICK:
        if history_clear_coordinator is None:
            print(
                "🛡️ [chat-history] button callback reserved while feature is unavailable"
            )
            return jsonify(gateway.ack()), 200
        handle = event.action_parameters.get("historyActionHandle")
        try:
            result = chat_clear_store.apply_handle(
                token_hash=hashlib.sha256(handle.encode("utf-8")).digest(),
                requester_name=event.actor_name or "",
                space_name=event.space_name or "",
                confirmation_message_name=event.message_name or "",
            )
        except ChatClearStoreError:
            result = None
        status = result.status if result is not None and result.authorized else None
        message = history_clear_presenter.build_status_message(status)
        envelope = (
            history_clear_presenter.addon_update(message)
            if status is not None
            else history_clear_presenter.addon_create(message)
        )
        gateway.record_outgoing(envelope)
        if event.space_name:
            try:
                chat_write_pacer.record_external_write(event.space_name)
            except ChatClearStoreError:
                pass
        if status in {
            JobStatus.DELETE_QUEUED,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
        } and history_worker is not None:
            history_worker.wake()
        return jsonify(envelope), 200

    raw_text = event.text or ""
    history_command = recognize_history_command(raw_text)
    if history_command is not HistoryCommandKind.NOT_HISTORY:
        if (
            history_command is HistoryCommandKind.STATS
            and history_settings is not None
            and history_settings.enabled
            and history_service is not None
        ):
            acknowledgement = gateway.ack()
            history_service.submit_stats(
                actor_name=event.actor_name,
                space_name=event.space_name,
                thread_name=event.thread_name,
                source_message_name=event.message_name,
            )
            return jsonify(acknowledgement), 200

        if (
            history_command is HistoryCommandKind.CLEAR
            and history_settings is not None
            and history_settings.enabled
            and history_settings.delete_enabled
            and history_client is not None
            and history_clear_coordinator is not None
        ):
            try:
                history_client.validate_authority(
                    event.actor_name, event.space_name
                )
                if event.message_name is None or event.event_time is None:
                    raise ValueError("missing durable event identity")
                parsed = parse_history_command(
                    raw_text,
                    event_time=event.event_time,
                )
                if (
                    parsed.reference_time_utc is None
                    or parsed.cutoff_utc is None
                    or parsed.normalized_argument is None
                ):
                    raise ValueError("invalid clear command")
                created = chat_clear_store.create_job(
                    ClearJobRequest(
                        source_message_name=event.message_name,
                        requester_name=event.actor_name or "",
                        space_name=event.space_name or "",
                        source_event_time_utc=event.event_time,
                        reference_time_utc=parsed.reference_time_utc,
                        cutoff_utc=parsed.cutoff_utc,
                        display_timezone=parsed.display_timezone,
                        normalized_argument=parsed.normalized_argument,
                    )
                )
            except (ChatHistoryClientError, HistoryCommandError, ValueError):
                message = history_clear_presenter.build_status_message(None)
                envelope = history_clear_presenter.addon_create(message)
                gateway.record_outgoing(envelope)
                return jsonify(envelope), 200
            except ChatClearStoreError:
                message = history_clear_presenter.build_status_message(
                    JobStatus.FAILED
                )
                envelope = history_clear_presenter.addon_create(message)
                gateway.record_outgoing(envelope)
                return jsonify(envelope), 200

            if event.attachments:
                envelope = card_presenter.build_card(
                    "ℹ️ คำสั่งถูกบันทึกแล้ว แต่ไฟล์แนบถูกข้ามและจะไม่ถูกดาวน์โหลด",
                    "jinx_system",
                )
                gateway.record_outgoing(envelope)
                if event.space_name:
                    try:
                        chat_write_pacer.record_external_write(event.space_name)
                    except ChatClearStoreError:
                        pass
                acknowledgement = envelope
            else:
                acknowledgement = gateway.ack()
            if history_worker is not None:
                history_worker.wake()
            if not created.created:
                print("ℹ️ [chat-history] duplicate clear command acknowledged")
            return jsonify(acknowledgement), 200

        notice = (
            "❌ รูปแบบคำสั่งไม่ถูกต้อง ใช้ `/chat` หรือ `/chat clear <เวลา>`"
            if history_command is HistoryCommandKind.INVALID_HISTORY
            else (
                "ℹ️ คำสั่งล้างประวัติยังไม่เปิดใช้งาน"
                if history_command is HistoryCommandKind.CLEAR
                and history_settings is not None
                and history_settings.enabled
                else "ℹ️ ระบบจัดการประวัติแชทยังไม่เปิดใช้งาน"
            )
        )
        if event.space_name:
            gateway.send_followup(
                event.space_name,
                event.thread_name or "",
                notice,
                "jinx_system",
            )
        return jsonify(gateway.ack()), 200

    space = event.space_name or ""
    thread = event.thread_name or ""
    user = event.display_name or "User"
    text = raw_text.strip()
    attachments = [dict(attachment) for attachment in event.attachments]
    quoted_message = (
        dict(event.quoted_message) if event.quoted_message is not None else None
    )

    try:
        target_store.remember(space, thread)
    except ChatTargetConflictError:
        print(
            "🚫 [attachment-out] rejecting request outside the fixed Chat space "
            f"(space={space}, thread={thread})"
        )
        gateway.send_followup(
            space,
            thread,
            "❌ Space นี้ไม่ใช่ปลายทางที่กำหนดไว้สำหรับ Jinx",
            "jinx_system",
        )
        return jsonify(gateway.ack()), 200
    except (ChatTargetError, TypeError, ValueError) as error:
        print(
            "❌ [attachment-out] cannot establish fixed Chat target: "
            f"{type(error).__name__} (space={space}, thread={thread})"
        )
        if space and thread:
            gateway.send_followup(
                space,
                thread,
                "❌ Jinx ไม่สามารถเตรียมปลายทางสำหรับส่งไฟล์ได้",
                "jinx_system",
            )
        return jsonify(gateway.ack()), 200

    if not _start_outbound_attachment_service():
        error = _outbound_start_error or outbound_attachment_service.last_start_error
        error_name = type(error).__name__ if error else "UnknownError"
        print(f"❌ [attachment-out] watcher failed to start: {error_name}")
        gateway.send_followup(
            space,
            thread,
            "❌ Jinx ไม่สามารถเริ่มระบบตรวจจับไฟล์ได้",
            "jinx_system",
        )

    orchestrator.dispatch(space, thread, user, text, attachments, quoted_message)

    return jsonify(gateway.ack()), 200


@app.route("/", methods=["GET"])
def ok() -> tuple[str, int]:
    return "ok", 200


def print_startup_notice() -> None:
    print("gchat-bot Copyright (C) 2026 Arayaphong Traisopon")
    print("This program comes with ABSOLUTELY NO WARRANTY.")
    print("This is free software, and you are welcome to redistribute it")
    print("under certain conditions; see the LICENSE file for details.")


if __name__ == "__main__":
    print_startup_notice()
    if not auth_settings.auth_debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_outbound_attachment_service()
        _start_chat_history_worker()
    app.run(host="0.0.0.0", port=8080, debug=auth_settings.auth_debug)
