from __future__ import annotations

import atexit
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

from helpers.chat_gateway import ChatGateway
from helpers.chat_target_store import (
    ChatTargetConflictError,
    ChatTargetError,
    FixedChatTargetStore,
)
from helpers.file_access_policy import SendableFilePolicy
from helpers.message_orchestrator import MessageOrchestrator
from helpers.orchestrator_messages import format_outbound_attachment_failure
from helpers.outbound_attachment_watcher import (
    DeliveryDisposition,
    FinalDeliveryFailure,
    OutboundAttachment,
    OutboundAttachmentConfig,
    OutboundAttachmentService,
    OutboundDeliveryResult,
)
from helpers.processing_gate import ProcessingGate, ProcessingGateError
from helpers.providers import ProviderSettings
from helpers.services import (
    AttachmentService,
    CardPresenter,
    ChatAuthSettings,
    ChatAuthVerifier,
    CredentialService,
)
from helpers.session_manager import SessionManager

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
BOT_CRED = Path(os.environ.get("GCHAT_BOT_CRED", str(BASE_DIR / "credentials.json")))
TOKEN_FILE = Path(os.environ.get("GCHAT_TOKEN_FILE", str(BASE_DIR / "token.json")))
DOWNLOAD_DIR = Path("/home/arme/.openclaw/workspace/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTBOUND_UPLOAD_DIR = Path.home() / ".openclaw" / "workspace" / "uploads"
OUTBOUND_IMAGE_DIR = Path.home() / ".openclaw" / "media" / "tool-image-generation"
DRIVE_UPLOAD_FOLDER_ID = os.environ.get(
    "DRIVE_UPLOAD_FOLDER_ID", "1iiD0C2hVwoDyo0wQQPG1sWd5cJLiUWlP"
)

SCOPES_USER = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
SCOPES_BOT = ["https://www.googleapis.com/auth/chat.bot"]

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
    source_dirs=(OUTBOUND_UPLOAD_DIR, OUTBOUND_IMAGE_DIR),
    state_dir=OUTBOUND_STATE_DIR / "attachments",
    max_file_bytes=MAX_OUTBOUND_ATTACHMENT_BYTES,
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
session_manager = SessionManager(
    session_key_file=SESSION_KEY_FILE,
    initial_settings=ProviderSettings.from_env(),
)
orchestrator = MessageOrchestrator(
    gateway=gateway,
    session_manager=session_manager,
    attachment_service=attachment_service,
    max_attachments_per_message=MAX_ATTACHMENTS_PER_MESSAGE,
    processing_gate=processing_gate,
)


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


@app.route("/chat", methods=["POST"])
def chat():
    raw_body = request.get_data(cache=True, as_text=True)
    gateway.record_incoming(raw_body)

    if not auth_verifier.verify(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid JSON body"}), 400

    payload = data.get("chat", {}).get("messagePayload", {})
    msg = data.get("message", {}) or payload.get("message", {}) or {}
    space = (
        data.get("space", {}) or payload.get("space", {}) or msg.get("space", {}) or {}
    ).get("name", "")
    thread = msg.get("thread", {}).get("name", "")
    user = (
        data.get("user", {})
        or data.get("chat", {}).get("user", {})
        or msg.get("sender", {})
        or {}
    ).get("displayName", "User")
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
        target_store.remember(space, thread)
    except ChatTargetConflictError:
        print(
            "🚫 [attachment-out] rejecting request outside the fixed Chat thread "
            f"(space={space}, thread={thread})"
        )
        gateway.send_followup(
            space,
            thread,
            "❌ เธรดนี้ไม่ใช่ปลายทางที่กำหนดไว้สำหรับ Jinx",
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
    app.run(host="0.0.0.0", port=8080, debug=auth_settings.auth_debug)
