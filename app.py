from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

import requests
from flask import Flask, jsonify, request

from helpers.providers import ProviderSettings, ask_with_provider_fallback
from helpers.services import (
    AttachmentService,
    BalanceService,
    CardPresenter,
    ChatAuthSettings,
    ChatAuthVerifier,
    CredentialService,
)

log = logging.getLogger(__name__)
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

BALANCE_API_URL = os.environ.get(
    "MOONSHOT_BALANCE_API_URL", "https://api.moonshot.ai/v1/users/me/balance"
)
BALANCE_CACHE_TTL_SECONDS = int(os.environ.get("BALANCE_CACHE_TTL_SECONDS", "45"))
SESSION_KEY_FILE = BASE_DIR / "session_key"
REQUESTS_LOG_FILE = BASE_DIR / "requests-log.jsonl"


def _save_incoming_request(raw_body: str) -> None:
    if not REQUESTS_LOG_FILE.exists():
        REQUESTS_LOG_FILE.touch()
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body = {"raw": raw_body}
    line = json.dumps({"timeStamp": ts, "body": body}, ensure_ascii=False)
    with REQUESTS_LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{line}\n")


def _read_session_key_file() -> str:
    if not SESSION_KEY_FILE.exists():
        return ""
    return SESSION_KEY_FILE.read_text(encoding="utf-8").strip()


def _write_session_key_file(session_key: str) -> None:
    tmp = SESSION_KEY_FILE.with_name(f".{SESSION_KEY_FILE.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(session_key, encoding="utf-8")
    os.replace(tmp, SESSION_KEY_FILE)


def _generate_session_key(agent: str) -> str:
    return f"agent:{agent}:cli:default:gchat:{uuid.uuid4().hex}"

auth_settings = ChatAuthSettings.from_env()
if auth_settings.auth_debug:
    logging.basicConfig(level=logging.INFO)
    log.setLevel(logging.INFO)

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
balance_service = BalanceService(
    api_url=BALANCE_API_URL,
    cache_ttl_seconds=BALANCE_CACHE_TTL_SECONDS,
    auth_debug=auth_settings.auth_debug,
)
card_presenter = CardPresenter()
provider_settings = ProviderSettings.from_env()
if saved_session_key := _read_session_key_file():
    provider_settings = replace(
        provider_settings,
        openclaw_session_key=saved_session_key,
    )
provider_executor = ThreadPoolExecutor(max_workers=4)
session_key_lock = threading.Lock()

def send_followup(
    space: str, thread: str, text: str, provider: str = "openclaw"
) -> None:
    if not space and "/threads/" in thread:
        space = thread.split("/threads/")[0]

    try:
        token = credential_service.get_bot_token()
        balance = balance_service.get_cached()
        body = card_presenter.build_card(text, balance, provider)["hostAppDataAction"][
            "chatDataAction"
        ]["createMessageAction"]["message"]

        if thread:
            body["thread"] = {"name": thread}

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
    except Exception as e:  # noqa: BLE001
        log.error("send_followup failed (space=%s, thread=%s): %s", space, thread, e)


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
        return jsonify(card_presenter.build_card("เริ่มเซสชันใหม่แล้ว")), 200

    attachments = (msg.get("attachment", []) or [])[:MAX_ATTACHMENTS_PER_MESSAGE]
    files = attachment_service.download_with_meta(attachments)

    ask_task = partial(
        ask_with_provider_fallback,
        text,
        user,
        files,
        provider_settings,
        auth_debug=auth_settings.auth_debug,
    )
    fut = provider_executor.submit(ask_task)

    try:
        reply, provider_used = fut.result(timeout=7)
        log.debug("provider_used=%s reply=%r", provider_used, reply)
        balance = balance_service.get_cached()
        return jsonify(card_presenter.build_card(reply, balance, provider_used))
    except FutureTimeout:

        def deliver() -> None:
            try:
                reply, provider_used = fut.result()
                log.debug("provider_used=%s reply=%r", provider_used, reply)
                send_followup(space, thread, reply, provider_used)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "provider call failed (space=%s, thread=%s): %s", space, thread, e
                )
                send_followup(space, thread, f"⚠️ เกิดข้อผิดพลาด: {e}")

        threading.Thread(target=deliver, daemon=True).start()
        return (
            jsonify(card_presenter.build_card("💬 รับเรื่องแล้ว จะตอบกลับในไม่ช้า...")),
            200,
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "provider immediate failure (space=%s, thread=%s): %s", space, thread, e
        )
        return jsonify(card_presenter.build_card(f"⚠️ เกิดข้อผิดพลาด: {e}")), 502


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
