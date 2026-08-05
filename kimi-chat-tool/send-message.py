#!/usr/bin/env python3
"""Send a message to a Kimi bot chat (as the web app does) and stream the reply.

Usage:
    ./send-message.py "your message"
    echo "your message" | ./send-message.py
    ./send-message.py --chat-id <uuid> --timeout 900 "your message"
    ./send-message.py --file ./report.pdf "summarize this file"

Requires KIMI_TOKEN env var (user JWT from the kimi.com web session).
For --file, the bot token is read from KIMI_BOT_TOKEN env or ~/.openclaw/openclaw.json.
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.request
import urllib.error
import uuid

API_BASE = "https://www.kimi.com/apiv2/kimi.gateway.im.v1.IMService"
UPLOAD_URL = "https://www.kimi.com/api-claw/files:upload"
OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")
DEFAULT_CHAT_ID = "19ec0b57-e362-8944-8000-092b3b0f50ef"
POLL_INTERVAL_S = 1.0

TERMINAL_STATUSES = {"COMPLETED", "CANCELLED", "TRUNCATED", "ERROR"}


def get_bot_token():
    token = os.environ.get("KIMI_BOT_TOKEN")
    if token:
        return token
    try:
        with open(OPENCLAW_CONFIG) as f:
            cfg = json.load(f)
        return cfg["plugins"]["entries"]["kimi-claw"]["config"]["bridge"]["token"]
    except Exception as e:
        print(f"error: cannot resolve bot token (set KIMI_BOT_TOKEN): {e}", file=sys.stderr)
        sys.exit(1)


def upload_file(path, bot_token):
    """Upload a file as the bot; returns the file id."""
    file_name = os.path.basename(path)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        data = f.read()
    boundary = f"----sendmsg{uuid.uuid4().hex}"
    body = b"\r\n".join([
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"'.encode(),
        f"Content-Type: {content_type}".encode(),
        b"",
        data,
        f"--{boundary}--".encode(),
        b"",
    ])
    req = urllib.request.Request(
        UPLOAD_URL,
        data=body,
        headers={
            "x-kimi-bot-token": bot_token,
            "content-type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"error: upload HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    file_id = (result.get("file") or {}).get("id")
    if not file_id:
        print(f"error: upload returned no file id: {result}", file=sys.stderr)
        sys.exit(1)
    return file_id


def normalize_status(status):
    """Map proto enum JSON forms (e.g. 'MESSAGE_STATUS_COMPLETED', 2) to a plain name."""
    if isinstance(status, int):
        return {0: "UNSPECIFIED", 1: "GENERATING", 2: "COMPLETED", 3: "CANCELLED", 4: "TRUNCATED", 5: "ERROR"}.get(status, str(status))
    return str(status).removeprefix("MESSAGE_STATUS_")


def rpc(method, payload, token):
    req = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=json.dumps(payload).encode(),
        headers={
            "authorization": f"Bearer {token}",
            "connect-protocol-version": "1",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"error: {method} HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"error: {method} failed: {e.reason}", file=sys.stderr)
        sys.exit(1)


def get_field(obj, camel, snake=None):
    """Accept both camelCase and snake_case JSON field names."""
    if obj is None:
        return None
    return obj.get(camel, obj.get(snake or camel))


def message_text(chat_message):
    parts = []
    for block in chat_message.get("blocks", []):
        text = block.get("text")
        if text and text.get("content"):
            parts.append(text["content"])
    return "".join(parts)


def message_status(chat_message):
    return normalize_status(chat_message.get("status", "UNSPECIFIED"))


def fetch_assistant_after(token, chat_id, anchor_id):
    """Return the latest assistant ChatMessage after anchor_id, or None."""
    resp = rpc("ListMessages", {
        "chat_id": chat_id,
        "page_size": 10,
        "direction": "DIRECTION_FORWARD",
        "start_message_id": anchor_id,
        "include_start_message": False,
    }, token)
    latest = None
    for wrapper in resp.get("messages", []):
        cm = get_field(wrapper, "message")
        if not cm:
            continue
        role = cm.get("role")
        if role in ("assistant", 3):
            latest = cm
    return latest


def main():
    parser = argparse.ArgumentParser(description="Send a message to a Kimi bot chat and stream the reply.")
    parser.add_argument("message", nargs="?", help="Message text (reads stdin if omitted)")
    parser.add_argument("--chat-id", default=os.environ.get("KIMI_CHAT_ID", DEFAULT_CHAT_ID))
    parser.add_argument("--timeout", type=int, default=600, help="Max seconds to wait for the reply (default 600)")
    parser.add_argument("--file", action="append", default=[], metavar="PATH",
                        help="Attach a file (repeatable); uploaded as the bot via /api-claw/files:upload")
    args = parser.parse_args()

    token = os.environ.get("KIMI_TOKEN")
    if not token:
        print("error: KIMI_TOKEN env var is not set", file=sys.stderr)
        sys.exit(1)

    text = args.message if args.message is not None else sys.stdin.read().strip()
    if not text:
        print("error: empty message", file=sys.stderr)
        sys.exit(1)

    blocks = [{"message_id": "", "text": {"content": text}}]
    if args.file:
        bot_token = get_bot_token()
        for path in args.file:
            file_id = upload_file(path, bot_token)
            blocks.append({"file": {"id": file_id}})
            print(f"[attached {os.path.basename(path)} file_id={file_id}]", file=sys.stderr)

    sent = rpc("SendMessage", {
        "chat_id": args.chat_id,
        "blocks": blocks,
    }, token)
    sent_id = get_field(sent, "messageId", "message_id")
    if not sent_id:
        print(f"error: SendMessage returned no messageId: {sent}", file=sys.stderr)
        sys.exit(1)
    print(f"[sent message_id={sent_id}]", file=sys.stderr)

    deadline = time.monotonic() + args.timeout
    printed = 0
    while True:
        if time.monotonic() > deadline:
            print(f"\n[timeout after {args.timeout}s — reply may still arrive in the chat]", file=sys.stderr)
            sys.exit(2)
        assistant = fetch_assistant_after(token, args.chat_id, sent_id)
        if assistant:
            content = message_text(assistant)
            if len(content) > printed:
                sys.stdout.write(content[printed:])
                sys.stdout.flush()
                printed = len(content)
            if message_status(assistant) in TERMINAL_STATUSES:
                sys.stdout.write("\n")
                status = message_status(assistant)
                if status != "COMPLETED":
                    print(f"[reply ended with status={status}]", file=sys.stderr)
                    sys.exit(3)
                break
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
