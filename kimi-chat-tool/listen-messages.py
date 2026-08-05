#!/usr/bin/env python3
"""Listen for new assistant messages in a Kimi chat and print them as they arrive.

Only role=assistant messages are printed; once a message finishes streaming
(reaches a terminal status), its full raw JSON is printed below the text.

Usage:
    ./listen-messages.py                     # listen to the default (bridge) chat
    ./listen-messages.py --chat-id <uuid>    # another room
    ./listen-messages.py --interval 3        # poll every 3s (default 2)

Requires KIMI_TOKEN env var. Stop with Ctrl+C.

Note: realtime Subscribe is bot-token only (rejects user JWT), so this polls
ListMessages instead — latency is roughly --interval.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://www.kimi.com/apiv2/kimi.gateway.im.v1.IMService"
DEFAULT_CHAT_ID = "19ec0b57-e362-8944-8000-092b3b0f50ef"


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
        print(f"warn: {method} failed ({e.reason}); retrying", file=sys.stderr)
        return None


ASSISTANT_ROLE_VALUES = {"assistant", "role_assistant", "3"}


def is_assistant(role):
    return str(role).strip().lower() in ASSISTANT_ROLE_VALUES


def describe_message(wrapper, chat_id):
    cm = wrapper.get("message") or {}
    role = cm.get("role")
    sender = wrapper.get("senderName") or wrapper.get("sender_name") or "?"
    parts = []
    for block in cm.get("blocks", []):
        if block.get("text", {}).get("content"):
            parts.append(block["text"]["content"])
        elif block.get("file"):
            f = block["file"]
            name = (f.get("meta") or {}).get("name", f.get("id", "?"))
            parts.append(f"[file: {name}]")
    status = str(cm.get("status", "")).removeprefix("MESSAGE_STATUS_")
    return {
        "id": cm.get("id"),
        "role": role,
        "sender": sender,
        "status": status,
        "text": "".join(parts),
        "raw": wrapper,
    }


def print_raw_json(raw):
    print(json.dumps(raw, ensure_ascii=False, indent=2), flush=True)


def fetch_recent(token, chat_id, page_size=20):
    resp = rpc("ListMessages", {
        "chat_id": chat_id,
        "page_size": page_size,
        "direction": "DIRECTION_BACKWARD",
    }, token)
    if resp is None:
        return None
    return [describe_message(w, chat_id) for w in resp.get("messages", [])]


def main():
    parser = argparse.ArgumentParser(description="Listen for new assistant messages in a Kimi chat.")
    parser.add_argument("--chat-id", default=os.environ.get("KIMI_CHAT_ID", DEFAULT_CHAT_ID))
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval seconds (default 2)")
    parser.add_argument("--stream", action="store_true",
                        help="Print assistant text as it generates (default: print only when COMPLETED)")
    parser.add_argument("--include-existing", action="store_true",
                        help="Also print the most recent messages on start")
    args = parser.parse_args()

    token = os.environ.get("KIMI_TOKEN")
    if not token:
        print("error: KIMI_TOKEN env var is not set", file=sys.stderr)
        sys.exit(1)

    # state[id] = {"printed": chars already printed, "done": terminal status reached}
    state = {}
    first = fetch_recent(token, args.chat_id)
    if first is None:
        first = []
    if args.include_existing:
        for m in reversed(first):
            if is_assistant(m["role"]):
                print(f"[assistant] {m['sender']}: {m['text']}", flush=True)
                print_raw_json(m["raw"])
    for m in first:
        if m["id"]:
            state[m["id"]] = {"printed": len(m["text"]) if args.include_existing else 0,
                              "done": not args.include_existing}
    print(f"[listening chat_id={args.chat_id} every {args.interval}s — Ctrl+C to stop]", file=sys.stderr)

    def render(m):
        st = state.get(m["id"])
        if st is None:
            st = {"printed": 0, "done": False}
            state[m["id"]] = st
        if st["done"]:
            return
        terminal = m["status"] in ("COMPLETED", "CANCELLED", "TRUNCATED", "ERROR")
        if not is_assistant(m["role"]):
            if terminal:
                st["done"] = True
            return
        if args.stream:
            if len(m["text"]) > st["printed"]:
                if st["printed"] == 0:
                    sys.stdout.write(f"[assistant] {m['sender']}: ")
                sys.stdout.write(m["text"][st["printed"]:])
                sys.stdout.flush()
                st["printed"] = len(m["text"])
            if terminal:
                sys.stdout.write("\n")
                sys.stdout.flush()
                print_raw_json(m["raw"])
                st["done"] = True
        elif terminal:
            print(f"[assistant] {m['sender']}: {m['text']}", flush=True)
            print_raw_json(m["raw"])
            st["done"] = True

    try:
        while True:
            time.sleep(args.interval)
            msgs = fetch_recent(token, args.chat_id)
            if msgs is None:
                continue
            for m in reversed(msgs):  # oldest first
                if m["id"]:
                    render(m)
    except KeyboardInterrupt:
        print("\n[stopped]", file=sys.stderr)


if __name__ == "__main__":
    main()
