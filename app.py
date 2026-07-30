from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import requests
from flask import Flask, jsonify, request
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCreds
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from helpers.md_to_gchat import MAX_CARD_WIDGETS, markdown_to_gchat_widgets
from helpers.providers import ProviderSettings, ask_with_provider_fallback

log = logging.getLogger(__name__)

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
BOT_CRED = Path(os.environ.get("GCHAT_BOT_CRED", str(BASE_DIR / "credentials.json")))
TOKEN_FILE = Path(os.environ.get("GCHAT_TOKEN_FILE", str(BASE_DIR / "token.json")))
UPLOAD_DIR = Path("/home/arme/.openclaw/workspace/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SCOPES_USER = ["https://www.googleapis.com/auth/drive.readonly"]
SCOPES_BOT = ["https://www.googleapis.com/auth/chat.bot"]
CHAT_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
CHAT_PROJECT_NUMBER = os.environ.get("GCHAT_PROJECT_NUMBER")
CHAT_AUDIENCE = os.environ.get("GCHAT_AUDIENCE", "").strip()
CHAT_AUDIENCES = {a.strip() for a in CHAT_AUDIENCE.split(",") if a.strip()}
if CHAT_PROJECT_NUMBER:
    CHAT_AUDIENCES.add(CHAT_PROJECT_NUMBER)
CHAT_TRUSTED_EMAILS = {
    e.strip()
    for e in os.environ.get("GCHAT_TRUSTED_EMAILS", "").split(",")
    if e.strip()
}
if CHAT_PROJECT_NUMBER:
    CHAT_TRUSTED_EMAILS.add(
        f"service-{CHAT_PROJECT_NUMBER}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
    )
CHAT_SERVICE_EMAIL_RE = (
    re.compile(
        rf"^service-{re.escape(CHAT_PROJECT_NUMBER)}@gcp-sa-gsuiteaddons\.iam\.gserviceaccount\.com$"
    )
    if CHAT_PROJECT_NUMBER
    else None
)
CHAT_AUTH_DEBUG = os.environ.get("GCHAT_AUTH_DEBUG", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if CHAT_AUTH_DEBUG:
    logging.basicConfig(level=logging.INFO)
    log.setLevel(logging.INFO)
kimi_executor = ThreadPoolExecutor(max_workers=4)
T = TypeVar("T")


def _atomic_write_secret(path: Path | str, content: str) -> None:
    target = Path(path)
    d = target.parent
    tmp = d / f".{target.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, target)


def verify_chat_request(req) -> bool:
    if not CHAT_AUDIENCES:
        log.error(
            "No chat audience configured; set GCHAT_AUDIENCE (preferred) or GCHAT_PROJECT_NUMBER"
        )
        return False
    auth_header = req.headers.get("Authorization", "")
    if CHAT_AUTH_DEBUG:
        log.info("Auth header present=%s", bool(auth_header))
    if not auth_header.startswith("Bearer "):
        if CHAT_AUTH_DEBUG:
            log.warning("Authorization header missing or not Bearer")
        return False
    token = auth_header[len("Bearer ") :]
    if CHAT_AUTH_DEBUG and not token:
        log.warning("Bearer token is empty")
    try:
        claims = google_id_token.verify_oauth2_token(
            token, Request(), audience=list(CHAT_AUDIENCES)
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Chat token verification failed: %s", e)
        return False
    if CHAT_AUTH_DEBUG:
        log.info(
            "Chat auth claims: iss=%s email=%s aud=%s",
            claims.get("iss"),
            claims.get("email"),
            claims.get("aud"),
        )
    issuer = claims.get("iss")
    email = claims.get("email")
    issuer_ok = issuer in CHAT_ISSUERS
    email_ok = bool(email) and (
        email in CHAT_TRUSTED_EMAILS
        or (bool(CHAT_SERVICE_EMAIL_RE) and bool(CHAT_SERVICE_EMAIL_RE.match(email)))
    )
    if CHAT_AUTH_DEBUG and not (issuer_ok and email_ok):
        log.warning(
            "Issuer/email mismatch: allowed_issuers=%s got_iss=%s trusted_emails=%s got_email=%s",
            sorted(CHAT_ISSUERS),
            issuer,
            sorted(CHAT_TRUSTED_EMAILS),
            email,
        )
    return issuer_ok and email_ok


def get_user_creds() -> UserCreds:
    creds = UserCreds.from_authorized_user_file(str(TOKEN_FILE), SCOPES_USER)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _atomic_write_secret(TOKEN_FILE, creds.to_json())
    return creds


def get_bot_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        str(BOT_CRED), scopes=SCOPES_BOT
    )
    creds.refresh(Request())
    return creds.token


MAX_ATTACHMENT_BYTES = int(
    os.environ.get("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
)
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "8"))
BALANCE_API_URL = os.environ.get(
    "MOONSHOT_BALANCE_API_URL", "https://api.moonshot.ai/v1/users/me/balance"
)
BALANCE_CACHE_TTL_SECONDS = int(os.environ.get("BALANCE_CACHE_TTL_SECONDS", "45"))
PROVIDER_SETTINGS = ProviderSettings.from_env()
_balance_cache_lock = threading.Lock()
_balance_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _as_float_or_none(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_kimi_balance_cached() -> dict[str, float] | None:
    api_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if not api_key:
        return None

    now = time.time()
    with _balance_cache_lock:
        cached_at = float(_balance_cache.get("at", 0.0) or 0.0)
        cached_value = _balance_cache.get("value")
        if cached_value and (now - cached_at) < max(BALANCE_CACHE_TTL_SECONDS, 1):
            return cached_value

    try:
        resp = requests.get(
            BALANCE_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        parsed = {
            "available_balance": _as_float_or_none(data.get("available_balance"))
            or 0.0,
            "voucher_balance": _as_float_or_none(data.get("voucher_balance")) or 0.0,
            "cash_balance": _as_float_or_none(data.get("cash_balance")) or 0.0,
        }
        with _balance_cache_lock:
            _balance_cache["at"] = now
            _balance_cache["value"] = parsed
        if CHAT_AUTH_DEBUG:
            log.info(
                "Moonshot balance: available=%s voucher=%s cash=%s",
                parsed["available_balance"],
                parsed["voucher_balance"],
                parsed["cash_balance"],
            )
        return parsed
    except Exception as e:  # noqa: BLE001
        log.warning("Balance API call failed: %s", e)
        return None


def balance_subtitle(balance: dict[str, float] | None) -> str:
    if not balance:
        return ""
    available = max(float(balance.get("available_balance", 0.0)), 0.0)
    return f"คงเหลือ ${available:.2f}"


def provider_label(provider: str) -> str:
    return "OpenClaw" if provider == "openclaw" else "Kimi K3"


def card_title(provider: str, balance: dict[str, float] | None) -> str:
    subtitle = balance_subtitle(balance)
    base = f"ใช้โมเดล {provider_label(provider)}"
    return base if not subtitle else f"{base} | {subtitle}"


def cleanup_downloads(files_with_meta: list[dict[str, Any]]) -> None:
    seen = set()
    for item in files_with_meta:
        fp = item.get("fp")
        if not fp or fp in seen:
            continue
        seen.add(fp)
        try:
            p = Path(fp)
            if p.exists():
                p.unlink()
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup failed for %s: %s", fp, e)


def with_cleanup(fn: Callable[..., T], cleanup: Callable[[], None]) -> Callable[..., T]:
    def wrapped(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        finally:
            cleanup()

    return wrapped


def download_with_meta(atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not atts:
        return results
    creds = get_user_creds()
    drive = build("drive", "v3", credentials=creds)
    for att in atts:
        meta = {
            "contentName": att.get("contentName", "unknown"),
            "contentType": att.get("contentType", ""),
            "size": att.get("size", ""),
            "driveFileId": att.get("driveDataRef", {}).get("driveFileId", ""),
        }
        try:
            safe = re.sub(r"[^a-zA-Z0-9._-]", "_", meta["contentName"])[:120]
            unique = (
                re.sub(r"[^a-zA-Z0-9]", "_", meta["driveFileId"]) or uuid.uuid4().hex
            )
            fp = UPLOAD_DIR / f"{unique}_{safe}"
            if "driveDataRef" in att:
                fid = meta["driveFileId"]
                ctype = meta["contentType"]
                if "spreadsheet" in ctype or "ritz" in ctype:
                    fp = Path(f"{fp}.xlsx")
                    req = drive.files().export_media(
                        fileId=fid,
                        mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                else:
                    req = drive.files().get_media(fileId=fid)
                too_big = False
                with io.FileIO(fp, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, req)
                    done = False
                    while not done:
                        _, done = dl.next_chunk()
                        if fh.tell() > MAX_ATTACHMENT_BYTES:
                            too_big = True
                            break
                if too_big:
                    if fp.exists():
                        fp.unlink()
                    meta["error"] = (
                        f"attachment exceeds {MAX_ATTACHMENT_BYTES} byte limit"
                    )
                    results.append({"fp": None, "meta": meta})
                    continue
                if fp.exists():
                    meta["localPath"] = str(fp)
                    meta["savedSize"] = fp.stat().st_size
                    results.append({"fp": str(fp), "meta": meta})
            else:
                meta["error"] = (
                    "attachment has no driveDataRef; skipping non-Drive attachment"
                )
                results.append({"fp": None, "meta": meta})
        except Exception as e:  # noqa: BLE001
            results.append({"fp": None, "meta": {**meta, "error": str(e)}})
    return results


def build_card(
    t: str, balance: dict[str, float] | None = None, provider: str = "openclaw"
):
    try:
        widgets = markdown_to_gchat_widgets(t)
        if not widgets:
            widgets = [{"textParagraph": {"text": t[:4000]}}]
    except Exception as e:  # noqa: BLE001
        widgets = [
            {
                "textParagraph": {
                    "text": f"{t[:3800]}<br><br><font color='#cc0000'>parse err: {e}</font>"
                }
            }
        ]
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {
                        "cardsV2": [
                            {
                                "cardId": "r",
                                "card": {
                                    "header": {
                                        "title": card_title(provider, balance),
                                    },
                                    "sections": [
                                        {"widgets": widgets[:MAX_CARD_WIDGETS]}
                                    ],
                                },
                            }
                        ]
                    }
                }
            }
        }
    }


def send_followup(space, thread, text, provider: str = "openclaw"):
    if not space and "/threads/" in thread:
        space = thread.split("/threads/")[0]
    try:
        token = get_bot_token()

        url = f"https://chat.googleapis.com/v1/{space}/messages"
        balance = get_kimi_balance_cached()
        body = build_card(text, balance, provider)["hostAppDataAction"][
            "chatDataAction"
        ]["createMessageAction"]["message"]
        if thread:
            body["thread"] = {"name": thread}
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
    if not verify_chat_request(request):
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
    attachments = (msg.get("attachment", []) or [])[:MAX_ATTACHMENTS_PER_MESSAGE]
    files = download_with_meta(attachments)
    ask_task = with_cleanup(
        partial(
            ask_with_provider_fallback,
            text,
            user,
            files,
            PROVIDER_SETTINGS,
            auth_debug=CHAT_AUTH_DEBUG,
        ),
        lambda: cleanup_downloads(files),
    )
    fut = kimi_executor.submit(ask_task)
    try:
        reply, provider_used = fut.result(timeout=7)
        balance = get_kimi_balance_cached()
        return jsonify(build_card(reply, balance, provider_used))
    except FutureTimeout:

        def deliver():
            try:
                reply, provider_used = fut.result()
                send_followup(space, thread, reply, provider_used)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "provider call failed (space=%s, thread=%s): %s", space, thread, e
                )
                send_followup(space, thread, f"⚠️ เกิดข้อผิดพลาด: {e}")

        threading.Thread(target=deliver, daemon=True).start()
        return jsonify(build_card("💬 รับเรื่องแล้ว จะตอบกลับในไม่ช้า...")), 200
    except Exception as e:  # noqa: BLE001
        log.error(
            "ask_kimi_direct immediate failure (space=%s, thread=%s): %s",
            space,
            thread,
            e,
        )
        return jsonify(build_card(f"⚠️ เกิดข้อผิดพลาด: {e}")), 502


@app.route("/", methods=["GET"])
def ok() -> tuple[str, int]:
    return "ok", 200


def print_startup_notice():
    print("gchat-bot Copyright (C) 2026 Arayaphong Traisopon")
    print("This program comes with ABSOLUTELY NO WARRANTY.")
    print("This is free software, and you are welcome to redistribute it")
    print("under certain conditions; see the LICENSE file for details.")


if __name__ == "__main__":
    print_startup_notice()
    app.run(host="0.0.0.0", port=8080)
