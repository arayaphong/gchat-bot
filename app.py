import os, json, re, io, base64, mimetypes, threading, logging, uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from flask import Flask, request, jsonify
from google.oauth2 import service_account, id_token as google_id_token
from google.oauth2.credentials import Credentials as UserCreds
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI
from md_to_gchat import markdown_to_gchat_widgets

log = logging.getLogger(__name__)

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_CRED = os.environ.get('GCHAT_BOT_CRED', os.path.join(BASE_DIR, 'credentials.json'))
TOKEN_FILE = os.environ.get('GCHAT_TOKEN_FILE', os.path.join(BASE_DIR, 'token.json'))
UPLOAD_DIR = '/home/arme/.openclaw/workspace/uploads'
os.makedirs(UPLOAD_DIR, exist_ok=True)
SCOPES_USER = ['https://www.googleapis.com/auth/drive.readonly']
SCOPES_BOT = ['https://www.googleapis.com/auth/chat.bot']
CHAT_ISSUER = 'chat@system.gserviceaccount.com'
CHAT_PROJECT_NUMBER = os.environ.get('GCHAT_PROJECT_NUMBER')
kimi_executor = ThreadPoolExecutor(max_workers=4)

def verify_chat_request(req):
    if not CHAT_PROJECT_NUMBER:
        log.error("GCHAT_PROJECT_NUMBER not set; rejecting request")
        return False
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    token = auth_header[len('Bearer '):]
    try:
        claims = google_id_token.verify_oauth2_token(token, Request(), audience=CHAT_PROJECT_NUMBER)
    except Exception as e:
        log.warning("Chat token verification failed: %s", e)
        return False
    return claims.get('iss') == CHAT_ISSUER or claims.get('email') == CHAT_ISSUER

def get_user_creds():
    creds = UserCreds.from_authorized_user_file(TOKEN_FILE, SCOPES_USER)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        open(TOKEN_FILE,'w').write(creds.to_json())
    return creds

def get_bot_token():
    creds = service_account.Credentials.from_service_account_file(BOT_CRED, scopes=SCOPES_BOT)
    creds.refresh(Request())
    return creds.token

MAX_ATTACHMENT_BYTES = int(os.environ.get('MAX_ATTACHMENT_BYTES', 20 * 1024 * 1024))
MAX_IMAGE_EMBED_BYTES = int(os.environ.get('MAX_IMAGE_EMBED_BYTES', 8 * 1024 * 1024))

def download_with_meta(atts):
    results=[]
    if not atts: return results
    creds=get_user_creds()
    drive=build('drive','v3',credentials=creds)
    for att in atts:
        meta={
            "contentName": att.get('contentName','unknown'),
            "contentType": att.get('contentType',''),
            "size": att.get('size',''),
            "driveFileId": att.get('driveDataRef',{}).get('driveFileId',''),
        }
        try:
            safe=re.sub(r'[^a-zA-Z0-9._-]','_',meta['contentName'])[:120]
            unique=re.sub(r'[^a-zA-Z0-9]','_', meta['driveFileId']) or uuid.uuid4().hex
            fp=os.path.join(UPLOAD_DIR,f"{unique}_{safe}")
            if 'driveDataRef' in att:
                fid=meta['driveFileId']
                ctype=meta['contentType']
                if 'spreadsheet' in ctype or 'ritz' in ctype:
                    fp+='.xlsx'
                    req=drive.files().export_media(fileId=fid,mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                else:
                    req=drive.files().get_media(fileId=fid)
                fh=io.FileIO(fp,'wb')
                dl=MediaIoBaseDownload(fh,req)
                done=False
                too_big=False
                while not done:
                    _,done=dl.next_chunk()
                    if fh.tell() > MAX_ATTACHMENT_BYTES:
                        too_big=True
                        break
                fh.close()
                if too_big:
                    os.remove(fp)
                    meta['error']=f"attachment exceeds {MAX_ATTACHMENT_BYTES} byte limit"
                    results.append({"fp":None,"meta":meta})
                    continue
                if os.path.exists(fp):
                    meta['localPath']=fp
                    meta['savedSize']=os.path.getsize(fp)
                    results.append({"fp":fp,"meta":meta})
        except Exception as e:
            results.append({"fp":None,"meta":{**meta,"error":str(e)}})
    return results

def ask_kimi_direct(text, user, files_with_meta):
    client=OpenAI(api_key=os.environ.get("MOONSHOT_API_KEY"), base_url="https://api.moonshot.ai/v1")
    content_blocks=[]
    for item in files_with_meta:
        fp=item['fp']
        m=item['meta']
        if not fp or not os.path.exists(fp): 
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
            if os.path.getsize(fp) > MAX_IMAGE_EMBED_BYTES:
                content_blocks.append({"type":"text","text":f"[ไฟล์ {m.get('contentName')} ใหญ่เกิน {MAX_IMAGE_EMBED_BYTES} bytes จึงไม่แนบรูปภาพ]"})
            else:
                with open(fp,'rb') as f:
                    b64=base64.b64encode(f.read()).decode('utf-8')
                mime=mimetypes.guess_type(fp)[0] or m.get('contentType') or 'image/png'
                data_url=f"data:{mime};base64,{b64}"
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
    files=download_with_meta(msg.get("attachment",[]) or [])
    fut=kimi_executor.submit(ask_kimi_direct, text, user, files)
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
def ok(): return "ok",200

def print_startup_notice():
    print("gchat-bot Copyright (C) 2026 Arayaphong Traisopon")
    print("This program comes with ABSOLUTELY NO WARRANTY.")
    print("This is free software, and you are welcome to redistribute it")
    print("under certain conditions; see the LICENSE file for details.")

if __name__=='__main__':
    print_startup_notice()
    app.run(host='0.0.0.0', port=8080)
