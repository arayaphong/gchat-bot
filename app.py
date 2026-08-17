from __future__ import annotations

import atexit
import os
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, g, jsonify, request

from helpers.chat_gateway import ChatGateway
from helpers.chat_target_store import (
    ChatTargetConflictError,
    ChatTargetError,
    FixedChatTargetStore,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.inbound_message_store import InboundMessageStore
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
from helpers.session_keys import ChatSessionContext
from helpers.session_manager import SessionManager
from helpers.session_trajectory_watcher import (
    AssistantTrajectoryMessage,
    SessionTrajectoryWatcher,
)
from helpers.token_tools.oauth_config import USER_OAUTH_SCOPES

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
BOT_CRED = Path(os.environ.get("GCHAT_BOT_CRED", str(BASE_DIR / "credentials.json")))
TOKEN_FILE = Path(os.environ.get("GCHAT_TOKEN_FILE", str(BASE_DIR / "token.json")))
DOWNLOAD_DIR = Path.home() / ".openclaw" / "workspace" / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTBOUND_UPLOAD_DIR = Path.home() / ".openclaw" / "workspace" / "uploads"
DRIVE_UPLOAD_FOLDER_ID = os.environ.get("DRIVE_UPLOAD_FOLDER_ID")

SCOPES_USER = list(USER_OAUTH_SCOPES)
SCOPES_BOT = ["https://www.googleapis.com/auth/chat.bot"]

MAX_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "8"))
MAX_OUTBOUND_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_OUTBOUND_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)
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
    # These roots constrain automatic discovery only. Explicit MEDIA: paths may
    # reference any absolute regular file that this sandboxed process can read.
    # Automatic output is accepted only through a registered thread directory.
    source_dirs=(Path.home(), Path("/tmp")),
    watched_source_dirs=(),
    thread_upload_root=OUTBOUND_UPLOAD_DIR,
    state_dir=OUTBOUND_STATE_DIR / "attachments",
    max_file_bytes=MAX_OUTBOUND_ATTACHMENT_BYTES,
)
OUTBOUND_STAGING_DIR = OUTBOUND_ATTACHMENT_CONFIG.state_dir / "staging"
PROCESSING_GATE_FILE = OUTBOUND_STATE_DIR / "processing.lock"
INBOUND_MESSAGE_STATE_DIR = OUTBOUND_STATE_DIR / "inbound-messages"

auth_settings = ChatAuthSettings.from_env()
processing_gate = ProcessingGate(PROCESSING_GATE_FILE)
inbound_message_store = InboundMessageStore(INBOUND_MESSAGE_STATE_DIR)
target_store = FixedChatTargetStore(
    state_file=CHAT_TARGET_FILE,
    configured_space=os.environ.get("GCHAT_OUTBOUND_SPACE", ""),
    configured_thread=os.environ.get("GCHAT_OUTBOUND_THREAD", ""),
)

credential_service = CredentialService(
    bot_cred=BOT_CRED,
    token_file=TOKEN_FILE,
    scopes_bot=SCOPES_BOT,
    scopes_user=SCOPES_USER,
)
auth_verifier = ChatAuthVerifier(auth_settings)
attachment_service = AttachmentService(
    download_dir=DOWNLOAD_DIR,
    max_attachment_bytes=MAX_ATTACHMENT_BYTES,
    credential_service=credential_service,
)
card_presenter = CardPresenter()

send_file_policy = SendableFilePolicy(allowed_roots=[OUTBOUND_STAGING_DIR])
gateway = ChatGateway(
    credential_service=credential_service,
    card_presenter=card_presenter,
    chat_in_log=CHAT_IN_LOG_FILE,
    chat_out_log=CHAT_OUT_LOG_FILE,
    file_policy=send_file_policy,
    drive_folder_id=DRIVE_UPLOAD_FOLDER_ID,
)
provider_settings = ProviderSettings.from_env()
openclaw_client = OpenClawClient(
    agent=provider_settings.openclaw_agent,
    base_url=provider_settings.openclaw_base_url,
    model=provider_settings.openclaw_model,
    outbound_upload_root=OUTBOUND_UPLOAD_DIR,
)
session_manager = SessionManager(
    openclaw_client=openclaw_client,
)


def _deliver_session_message(message: AssistantTrajectoryMessage) -> bool:
    if message.text and not gateway.send_followup(
        message.space,
        message.reply_thread,
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
                destination_space=message.space,
                destination_thread=message.reply_thread,
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
                message.space,
                message.reply_thread,
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
    state_file=OUTBOUND_ATTACHMENT_CONFIG.state_dir / "trajectory-cursors.json",
)
orchestrator = MessageOrchestrator(
    gateway=gateway,
    session_manager=session_manager,
    attachment_service=attachment_service,
    openclaw_client=openclaw_client,
    max_attachments_per_message=MAX_ATTACHMENTS_PER_MESSAGE,
    processing_gate=processing_gate,
    session_watcher=session_message_watcher,
    inbound_message_store=inbound_message_store,
    thread_upload_preparer=lambda context: (
        outbound_attachment_service.prepare_thread_upload(context)
    ),
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
        destination_space = attachment.destination_space
        destination_thread = attachment.destination_thread
        if not destination_space:
            try:
                target = target_store.get()
            except ChatTargetError as error:
                print(
                    f"❌ [attachment-out] cannot load fallback Chat target: "
                    f"{type(error).__name__}"
                )
                return DeliveryDisposition.DEFERRED
            if target is None:
                return DeliveryDisposition.DEFERRED
            destination_space = target.space
            destination_thread = target.thread
        if attachment.staged_path is None and not attachment.web_view_link:
            return DeliveryDisposition.FAILED
        delivery_path = attachment.staged_path or attachment.source_path

        result = gateway.send_file(
            destination_space,
            destination_thread,
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
            f"name={attachment.display_name!r} thread={destination_thread}"
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
        destination_space = failure.attachment.destination_space
        destination_thread = failure.attachment.destination_thread
        if not destination_space:
            target = target_store.get()
            if target is None:
                raise RuntimeError("fallback Chat target is not available")
            destination_space = target.space
            destination_thread = target.thread
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
            destination_space,
            destination_thread,
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


@app.before_request
def _mark_request_start() -> None:
    g.request_started_at = time.monotonic()


@app.after_request
def _log_chat_response_time(response: Response) -> Response:
    # Google Chat retries and shows "not responding" when the webhook answer
    # misses its deadline — log how long every /chat response actually took.
    if request.path == "/chat":
        started_at = getattr(g, "request_started_at", None)
        if started_at is not None:
            elapsed_ms = (time.monotonic() - started_at) * 1000
            print(
                f"⏱️ [chat-in] responded status={response.status_code} "
                f"in {elapsed_ms:.0f}ms"
            )
    return response


@app.route("/chat", methods=["POST"])
def chat() -> tuple[Response, int]:
    raw_body = request.get_data(cache=True, as_text=True)
    gateway.record_incoming(raw_body)

    if not auth_verifier.verify(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid JSON body"}), 400

    chat_data = data.get("chat", {})
    if not isinstance(chat_data, dict):
        chat_data = {}

    # Classic Chat interaction events declare their kind in ``type``. Google
    # Workspace add-on events instead use a union of ``*Payload`` fields under
    # ``chat``. Only message events represent user turns; lifecycle, card,
    # widget, and app-command events must not become empty OpenClaw prompts.
    classic_non_message = "type" in data and data.get("type") != "MESSAGE"
    addon_payload_keys = {
        key for key in chat_data if isinstance(key, str) and key.endswith("Payload")
    }
    addon_non_message = bool(addon_payload_keys) and addon_payload_keys != {
        "messagePayload"
    }
    if classic_non_message or addon_non_message:
        return jsonify(gateway.ack()), 200

    payload = chat_data.get("messagePayload", {})
    if not isinstance(payload, dict):
        payload = {}
    msg = data.get("message", {}) or payload.get("message", {}) or {}
    if not isinstance(msg, dict):
        msg = {}
    space_details = (
        data.get("space", {})
        or payload.get("space", {})
        or chat_data.get("space", {})
        or msg.get("space", {})
        or {}
    )
    if not isinstance(space_details, dict):
        space_details = {}
    thread_details = msg.get("thread", {})
    if not isinstance(thread_details, dict):
        thread_details = {}
    space = space_details.get("name", "")
    thread = thread_details.get("name", "")
    modern_space_type = space_details.get("spaceType")
    is_direct_message = space_details.get("singleUserBotDm") is True or (
        modern_space_type == "DIRECT_MESSAGE"
    ) or (
        modern_space_type in (None, "", "SPACE_TYPE_UNSPECIFIED")
        and space_details.get("type") == "DM"
    )
    user_details = (
        data.get("user", {})
        or chat_data.get("user", {})
        or msg.get("sender", {})
        or {}
    )
    if not isinstance(user_details, dict):
        user_details = {}
    user = str(user_details.get("displayName") or "User")
    text = (msg.get("argumentText") or msg.get("text") or "").strip()

    stickers = [
        {**gif, "isSticker": True} for gif in (msg.get("attachedGifs", []) or [])
    ]
    attachments = (msg.get("attachment", []) or []) + stickers

    quoted_snapshot = (
        msg.get("quotedMessageMetadata", {}).get("quotedMessageSnapshot", {}) or {}
    )
    quoted_message = (
        {
            "sender": quoted_snapshot.get("sender", ""),
            "text": quoted_snapshot.get("text", ""),
        }
        if quoted_snapshot.get("text")
        else None
    )

    try:
        context = ChatSessionContext.from_event(
            space,
            thread,
            is_direct_message=is_direct_message,
            thread_reply=msg.get("threadReply"),
        )
    except (TypeError, ValueError) as error:
        print(
            "❌ [chat-in] cannot derive deterministic session context: "
            f"{type(error).__name__} (space={space}, thread={thread})"
        )
        gateway.send_followup(
            space,
            "",
            "❌ Jinx ไม่สามารถระบุเซสชั่นของข้อความนี้ได้",
            "jinx_system",
        )
        return jsonify(gateway.ack()), 200

    try:
        target_store.remember(context.space, context.thread)
    except ChatTargetConflictError:
        print(
            "⚠️ [attachment-out] keeping the existing fallback target "
            f"while accepting another Space (space={space}, thread={thread})"
        )
    except (ChatTargetError, TypeError, ValueError) as error:
        print(
            "⚠️ [attachment-out] cannot update fallback Chat target: "
            f"{type(error).__name__} (space={space}, thread={thread})"
        )

    if not _start_outbound_attachment_service():
        error = _outbound_start_error or outbound_attachment_service.last_start_error
        error_name = type(error).__name__ if error else "UnknownError"
        print(f"❌ [attachment-out] watcher failed to start: {error_name}")
        gateway.send_followup(
            context.space,
            context.reply_thread,
            "❌ Jinx ไม่สามารถเริ่มระบบตรวจจับไฟล์ได้",
            "jinx_system",
        )

    orchestrator.dispatch(
        context.space,
        context.thread,
        user,
        text,
        attachments,
        quoted_message,
        context=context,
        command_id=str(msg.get("name") or ""),
    )

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
        # Cron-fired OpenClaw turns write into an existing Chat session without
        # any inbound /chat request, so the trajectory watcher must be polling
        # from startup — not only after the first user message.
        session_message_watcher.start()
        # Fetch Google signing certs in the background so the first webhook
        # after a restart verifies JWTs from a warm cache.
        threading.Thread(target=auth_verifier.warm_cache, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, debug=auth_settings.auth_debug)
