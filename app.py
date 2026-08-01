from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request

from helpers.jsonl_log import append_jsonl
from helpers.providers import ProviderSettings, ask_provider
from helpers.providers.openclaw_provider import NO_RESPONSE_TEXT
from helpers.services import (
    AttachmentService,
    CardPresenter,
    ChatAuthSettings,
    ChatAuthVerifier,
    CredentialService,
)

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
BOT_CRED = Path(os.environ.get("GCHAT_BOT_CRED", str(BASE_DIR / "credentials.json")))
TOKEN_FILE = Path(os.environ.get("GCHAT_TOKEN_FILE", str(BASE_DIR / "token.json")))
UPLOAD_DIR = Path("/home/arme/.openclaw/workspace/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SCOPES_USER = ["https://www.googleapis.com/auth/drive.readonly"]
SCOPES_BOT = ["https://www.googleapis.com/auth/chat.bot"]

MAX_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "8"))

SESSION_KEY_FILE = BASE_DIR / "session_key"
CHAT_IN_LOG_FILE = BASE_DIR / "chat-in.jsonl"
CHAT_OUT_LOG_FILE = BASE_DIR / "chat-out.jsonl"


def _save_incoming_request(raw_body: str) -> None:
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body = {"raw": raw_body}
    append_jsonl(CHAT_IN_LOG_FILE, body)


def _save_outgoing_response(body: dict[str, Any]) -> None:
    append_jsonl(CHAT_OUT_LOG_FILE, body)


def _read_session_key_file() -> str:
    if not SESSION_KEY_FILE.exists():
        return ""
    return SESSION_KEY_FILE.read_text(encoding="utf-8").strip()


def _write_session_key_file(session_key: str) -> None:
    tmp = SESSION_KEY_FILE.with_name(f".{SESSION_KEY_FILE.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(session_key, encoding="utf-8")
    os.replace(tmp, SESSION_KEY_FILE)


def _generate_session_key(agent: str) -> str:
    short_uuid = uuid.uuid4().hex[:12]
    return f"agent:{agent}:cli:default:gchat:{short_uuid}"

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
provider_settings = ProviderSettings.from_env()
if saved_session_key := _read_session_key_file():
    provider_settings = replace(
        provider_settings,
        openclaw_session_key=saved_session_key,
    )
session_key_lock = threading.Lock()

def send_followup(
    space: str, thread: str, text: str, provider: str = "openclaw"
) -> None:
    if not space and "/threads/" in thread:
        space = thread.split("/threads/")[0]

    try:
        token = credential_service.get_bot_token()
        body = card_presenter.build_card(text, provider)["hostAppDataAction"][
            "chatDataAction"
        ]["createMessageAction"]["message"]

        if thread:
            body["thread"] = {"name": thread}

        _save_outgoing_response(body)

        url = f"https://chat.googleapis.com/v1/{space}/messages"
        requests.post(
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=15,
        )
    except Exception:  # noqa: BLE001, S110
        pass


def _notify_step(space: str, thread: str, text: str) -> None:
    print(text)
    if auth_settings.auth_debug:
        send_followup(space, thread, text, "jinx_system")


def process_message(
    space: str,
    thread: str,
    user: str,
    text: str,
    attachments: list[dict[str, Any]],
    settings: ProviderSettings,
) -> None:
    try:
        files: list[dict[str, Any]] = []
        if attachments:
            _notify_step(
                space,
                thread,
                f"📎 [attachment-in] downloading {len(attachments)} file(s) "
                f"(space={space}, thread={thread})",
            )
            files = attachment_service.download_with_meta(attachments)

        _notify_step(
            space,
            thread,
            f"🤖 [openclaw-out] sending request (space={space}, thread={thread})",
        )
        reply_text, provider_used = ask_provider(text, user, files, settings)

        if reply_text.strip() == NO_RESPONSE_TEXT:
            _notify_step(
                space,
                thread,
                f"⏭️ [openclaw-skip] no response, skipping reply "
                f"(space={space}, thread={thread})",
            )
            return

        print(f"📤 [chat-out] delivering reply (space={space}, thread={thread})")
        send_followup(space, thread, reply_text, provider_used)
    except Exception as e:  # noqa: BLE001
        if auth_settings.auth_debug:
            print(f"❌ [error] {e} (space={space}, thread={thread})")
        # router already formats the user-facing secretary message
        send_followup(space, thread, str(e), "jinx_system")


@app.route("/chat", methods=["POST"])
def chat():
    global provider_settings

    raw_body = request.get_data(cache=True, as_text=True)
    _save_incoming_request(raw_body)

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

    if text == "/new":
        new_session_key = _generate_session_key(provider_settings.openclaw_agent)
        with session_key_lock:
            _write_session_key_file(new_session_key)
            provider_settings = replace(
                provider_settings,
                openclaw_session_key=new_session_key,
            )

    stickers = [
        {**gif, "isSticker": True} for gif in (msg.get("attachedGifs", []) or [])
    ]
    attachments = ((msg.get("attachment", []) or []) + stickers)[
        :MAX_ATTACHMENTS_PER_MESSAGE
    ]

    step1_text = f"✅ [chat-in] accepted request (space={space}, thread={thread})"
    print(step1_text)

    threading.Thread(
        target=process_message,
        args=(space, thread, user, text, attachments, provider_settings),
        daemon=True,
    ).start()

    response_body = (
        {}
        if not auth_settings.auth_debug
        else card_presenter.build_card(step1_text, provider="jinx_system")
    )
    _save_outgoing_response(response_body)
    return jsonify(response_body), 200


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
