from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, request

from helpers.chat_gateway import ChatGateway
from helpers.file_access_policy import SendableFilePolicy
from helpers.message_orchestrator import MessageOrchestrator
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
UPLOAD_DIR = Path("/home/arme/.openclaw/workspace/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# WARNING: covers the entire home directory, including token.json,
# credentials.json, and .ssh — expanded from a narrow sandbox at the user's
# explicit request, after being told this reopens the arbitrary-file-send
# exposure that ALLOWED_SEND_ROOTS was originally created to close.
ALLOWED_SEND_ROOTS = [Path.home(), Path("/tmp")]
DRIVE_UPLOAD_FOLDER_ID = os.environ.get(
    "DRIVE_UPLOAD_FOLDER_ID", "1iiD0C2hVwoDyo0wQQPG1sWd5cJLiUWlP"
)

SCOPES_USER = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/chat.messages",
]
SCOPES_BOT = ["https://www.googleapis.com/auth/chat.bot"]

MAX_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "8"))

SESSION_KEY_FILE = BASE_DIR / "session_key"
CHAT_IN_LOG_FILE = BASE_DIR / "chat-in.jsonl"
CHAT_OUT_LOG_FILE = BASE_DIR / "chat-out.jsonl"

auth_settings = ChatAuthSettings.from_env()

credential_service = CredentialService(
    bot_cred=BOT_CRED,
    token_file=TOKEN_FILE,
    scopes_bot=SCOPES_BOT,
    scopes_user=SCOPES_USER,
)
auth_verifier = ChatAuthVerifier(auth_settings)
attachment_service = AttachmentService(
    upload_dir=UPLOAD_DIR,
    max_attachment_bytes=MAX_ATTACHMENT_BYTES,
    credential_service=credential_service,
)
card_presenter = CardPresenter()

send_file_policy = SendableFilePolicy(allowed_roots=ALLOWED_SEND_ROOTS)
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
)


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
    attachments = ((msg.get("attachment", []) or []) + stickers)[
        :MAX_ATTACHMENTS_PER_MESSAGE
    ]

    quoted_snapshot = (
        msg.get("quotedMessageMetadata", {}).get("quotedMessageSnapshot", {}) or {}
    )
    quoted_message = (
        {"sender": quoted_snapshot.get("sender", ""), "text": quoted_snapshot.get("text", "")}
        if quoted_snapshot.get("text")
        else None
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
    app.run(host="0.0.0.0", port=8080, debug=auth_settings.auth_debug)
