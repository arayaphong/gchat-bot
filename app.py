from __future__ import annotations

import base64
import io
import logging
import mimetypes
import os
import re
import threading
import uuid
from functools import partial
from pathlib import Path
from typing import Any, TypeVar
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from flask import Flask, request, jsonify
from google.oauth2 import service_account, id_token as google_id_token
from google.oauth2.credentials import Credentials as UserCreds
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI
from helpers.md_to_gchat import markdown_to_gchat_widgets

log = logging.getLogger(__name__)

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
BOT_CRED = Path(os.environ.get("GCHAT_BOT_CRED", str(BASE_DIR / "credentials.json")))
TOKEN_FILE = Path(os.environ.get("GCHAT_TOKEN_FILE", str(BASE_DIR / "token.json")))
UPLOAD_DIR = Path("/home/arme/.openclaw/workspace/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SCOPES_USER = ['https://www.googleapis.com/auth/drive.readonly']
SCOPES_BOT = ['https://www.googleapis.com/auth/chat.bot']
CHAT_ISSUERS = {'https://accounts.google.com', 'accounts.google.com'}
CHAT_PROJECT_NUMBER = os.environ.get('GCHAT_PROJECT_NUMBER')
CHAT_AUDIENCE = os.environ.get('GCHAT_AUDIENCE', '').strip()
CHAT_AUDIENCES = {a.strip() for a in CHAT_AUDIENCE.split(',') if a.strip()}
if CHAT_PROJECT_NUMBER:
    CHAT_AUDIENCES.add(CHAT_PROJECT_NUMBER)
CHAT_TRUSTED_EMAILS = {e.strip() for e in os.environ.get('GCHAT_TRUSTED_EMAILS', '').split(',') if e.strip()}
if CHAT_PROJECT_NUMBER:
    CHAT_TRUSTED_EMAILS.add(f"service-{CHAT_PROJECT_NUMBER}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com")
CHAT_AUTH_DEBUG = os.environ.get('GCHAT_AUTH_DEBUG', '').lower() in {'1', 'true', 'yes', 'on'}
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
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    os.replace(tmp, target)

def verify_chat_request(req) -> bool:
    if not CHAT_AUDIENCES:
        log.error("No chat audience configured; set GCHAT_AUDIENCE (preferred) or GCHAT_PROJECT_NUMBER")
        return False
    auth_header = req.headers.get('Authorization', '')
    if CHAT_AUTH_DEBUG:
        log.info("Auth header present=%s", bool(auth_header))
    if not auth_header.startswith('Bearer '):
        if CHAT_AUTH_DEBUG:
            log.warning("Authorization header missing or not Bearer")
        return False
    token = auth_header[len('Bearer '):]
    if CHAT_AUTH_DEBUG and not token:
        log.warning("Bearer token is empty")
    try:
        claims = google_id_token.verify_oauth2_token(token, Request(), audience=list(CHAT_AUDIENCES))
    except Exception as e:
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
        or bool(re.match(r'^service-\d+@gcp-sa-gsuiteaddons\.iam\.gserviceaccount\.com$', email))
    )
    if CHAT_AUTH_DEBUG and not (issuer_ok or email_ok):
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
    creds = service_account.Credentials.from_service_account_file(str(BOT_CRED), scopes=SCOPES_BOT)
    creds.refresh(Request())
    return creds.token

MAX_ATTACHMENT_BYTES = int(os.environ.get('MAX_ATTACHMENT_BYTES', 20 * 1024 * 1024))
MAX_IMAGE_EMBED_BYTES = int(os.environ.get('MAX_IMAGE_EMBED_BYTES', 8 * 1024 * 1024))
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get('MAX_ATTACHMENTS_PER_MESSAGE', 8))

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
        except Exception as e:
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
    drive = build('drive', 'v3', credentials=creds)
    for att in atts:
        meta = {
            "contentName": att.get('contentName', 'unknown'),
            "contentType": att.get('contentType', ''),
            "size": att.get('size', ''),
            "driveFileId": att.get('driveDataRef', {}).get('driveFileId', ''),
        }
        try:
            safe = re.sub(r'[^a-zA-Z0-9._-]', '_', meta['contentName'])[:120]
            unique = re.sub(r'[^a-zA-Z0-9]', '_', meta['driveFileId']) or uuid.uuid4().hex
            fp = UPLOAD_DIR / f"{unique}_{safe}"
            if 'driveDataRef' in att:
                fid = meta['driveFileId']
                ctype = meta['contentType']
                if 'spreadsheet' in ctype or 'ritz' in ctype:
                    fp = Path(f"{fp}.xlsx")
                    req = drive.files().export_media(fileId=fid, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                else:
                    req = drive.files().get_media(fileId=fid)
                too_big = False
                with io.FileIO(fp, 'wb') as fh:
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
                    meta['error'] = f"attachment exceeds {MAX_ATTACHMENT_BYTES} byte limit"
                    results.append({"fp": None, "meta": meta})
                    continue
                if fp.exists():
                    meta['localPath'] = str(fp)
                    meta['savedSize'] = fp.stat().st_size
                    results.append({"fp": str(fp), "meta": meta})
            else:
                meta['error'] = 'attachment has no driveDataRef; skipping non-Drive attachment'
                results.append({"fp": None, "meta": meta})
        except Exception as e:
            results.append({"fp": None, "meta": {**meta, "error": str(e)}})
    return results

def ask_kimi_direct(text: str, user: str, files_with_meta: list[dict[str, Any]]) -> str:
    client = OpenAI(api_key=os.environ.get("MOONSHOT_API_KEY"), base_url="https://api.moonshot.ai/v1")
    content_blocks: list[dict[str, Any]] = []
    for item in files_with_meta:
        fp = item['fp']
        m = item['meta']
        if not fp or not Path(fp).exists():
            content_blocks.append({"type":"text","text":f"[ไฟล์ {m.get('contentName')} โหลดไม่สำเร็จ: {m.get('error')}]"})
            continue
        meta_text = f"""[FILE_META]
name: {m.get('contentName')}
mimeType: {m.get('contentType')}
driveFileId: {m.get('driveFileId')}
size: {m.get('savedSize')} bytes
[/FILE_META]"""
        content_blocks.append({"type":"text","text": meta_text})
        if m.get('contentType','').startswith('image/') or fp.lower().endswith(('.png','.jpg','.jpeg','.webp','.gif')):
            if Path(fp).stat().st_size > MAX_IMAGE_EMBED_BYTES:
                content_blocks.append({"type":"text","text":f"[ไฟล์ {m.get('contentName')} ใหญ่เกิน {MAX_IMAGE_EMBED_BYTES} bytes จึงไม่แนบรูปภาพ]"})
            else:
                with open(fp, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode('utf-8')
                mime = mimetypes.guess_type(fp)[0] or m.get('contentType') or 'image/png'
                data_url = f"data:{mime};base64,{b64}"
                content_blocks.append({"type":"image_url","image_url":{"url": data_url}})
    content_blocks.append({"type":"text","text": f"{user}: {text}"})
    completion=client.chat.completions.create(
        model="kimi-k3",
        messages=[
            {"role":"system","content":"You are Kimi K3. เมื่อได้รับ FILE_META ให้ใช้ชื่อไฟล์และ mimeType ประกอบการตอบด้วย"},
            {"role":"user","content": content_blocks}
        ],
    )
    return completion.choices[0].message.content

def build_card(t):
    try:
        widgets = markdown_to_gchat_widgets(t)
        if not widgets:
            widgets = [{"textParagraph": {"text": t[:4000]}}]
    except Exception as e:
        widgets = [{"textParagraph": {"text": f"{t[:3800]}<br><br><font color='#cc0000'>parse err: {e}</font>"}}]
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {
                        "cardsV2": [{
                            "cardId": "r",
                            "card": {
                                "header": {"title": "Kimi K3 + md"},
                                "sections": [{"widgets": widgets[:25]}]
                            }
                        }]
                    }
                }
            }
        }
    }

def send_followup(space, thread, text):
    if not space and "/threads/" in thread: space=thread.split("/threads/")[0]
    try:
        token=get_bot_token()
        import requests
        url=f"https://chat.googleapis.com/v1/{space}/messages"
        body=build_card(text)["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        if thread: body["thread"]={"name":thread}
        requests.post(url, headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"}, json=body, timeout=15)
    except Exception as e:
        log.error("send_followup failed (space=%s, thread=%s): %s", space, thread, e)

@app.route('/chat', methods=['POST'])
def chat():
    if not verify_chat_request(request):
        return jsonify({"error": "unauthorized"}), 401
    data=request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    payload=data.get("chat",{}).get("messagePayload",{})
    msg=data.get("message",{}) or payload.get("message",{}) or {}
    space=(data.get("space",{}) or payload.get("space",{}) or msg.get("space",{}) or {}).get("name","")
    thread=msg.get("thread",{}).get("name","")
    user=(data.get("user",{}) or data.get("chat",{}).get("user",{}) or msg.get("sender",{}) or {}).get("displayName","User")
    text=(msg.get("argumentText") or msg.get("text") or "").strip()
    attachments = (msg.get("attachment",[]) or [])[:MAX_ATTACHMENTS_PER_MESSAGE]
    files=download_with_meta(attachments)
    ask_task = with_cleanup(
        partial(ask_kimi_direct, text, user, files),
        lambda: cleanup_downloads(files),
    )
    fut=kimi_executor.submit(ask_task)
    try:
        reply=fut.result(timeout=7)
        return jsonify(build_card(reply))
    except FutureTimeout:
        def deliver():
            try:
                send_followup(space, thread, fut.result())
            except Exception as e:
                log.error("ask_kimi_direct failed (space=%s, thread=%s): %s", space, thread, e)
                send_followup(space, thread, f"⚠️ เกิดข้อผิดพลาด: {e}")
        threading.Thread(target=deliver, daemon=True).start()
        return jsonify(build_card("⏳ กำลังส่งไฟล์ + meta-data แบบ base64..."))

@app.route('/', methods=['GET'])
def ok() -> tuple[str, int]:
    return "ok", 200

def print_startup_notice():
    print("gchat-bot Copyright (C) 2026 Arayaphong Traisopon")
    print("This program comes with ABSOLUTELY NO WARRANTY.")
    print("This is free software, and you are welcome to redistribute it")
    print("under certain conditions; see the LICENSE file for details.")

if __name__ == '__main__':
    print_startup_notice()
    app.run(host='0.0.0.0', port=8080)
