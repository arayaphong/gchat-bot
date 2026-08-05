#!/usr/bin/env python3
"""Manage Kimi IM rooms via the IMService RPC (same API the kimi.com app uses).

Usage:
    ./kimi-rooms.py list [--page-size 20]
    ./kimi-rooms.py create "room name" [--instruction "..."] [--bot-id <id>]
    ./kimi-rooms.py delete <room_id>

Requires KIMI_TOKEN env var (user JWT from the kimi.com web session).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://www.kimi.com/apiv2/kimi.gateway.im.v1.IMService"
DEFAULT_BOT_ID = "19ec0b57-e362-8944-8000-00003b0f50ef"  # Jinx bot

ROOM_TYPES = {0: "UNSPECIFIED", 1: "GROUP", 2: "DIRECT"}


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


def room_type_name(room):
    t = room.get("type", 0)
    if isinstance(t, int):
        return ROOM_TYPES.get(t, str(t))
    return str(t).removeprefix("ROOM_TYPE_")


def cmd_list(args, token):
    page_token = None
    total = 0
    while True:
        payload = {"page_size": args.page_size}
        if page_token:
            payload["page_token"] = page_token
        resp = rpc("ListRooms", payload, token)
        for room in resp.get("rooms", []):
            total += 1
            name = room.get("name", "")
            rtype = room_type_name(room)
            members = room.get("memberCount", room.get("member_count", "?"))
            print(f"{room.get('id')}  {rtype:<8}  members={members}  {name}")
        page_token = resp.get("nextPageToken", resp.get("next_page_token"))
        if not page_token:
            break
    print(f"-- {total} room(s)", file=sys.stderr)


def cmd_create(args, token):
    payload = {"name": args.name}
    if args.instruction:
        payload["instruction"] = args.instruction
    if args.bot_id:
        payload["bot_ids"] = [args.bot_id]
    resp = rpc("CreateRoom", payload, token)
    room = resp.get("room", {})
    print(f"{room.get('id')}  {room_type_name(room):<8}  {room.get('name', '')}")


def cmd_delete(args, token):
    rpc("DeleteRoom", {"room_id": args.room_id}, token)
    print(f"deleted {args.room_id}")


def main():
    parser = argparse.ArgumentParser(description="List, create, and delete Kimi IM rooms.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List rooms")
    p_list.add_argument("--page-size", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="Create a room")
    p_create.add_argument("name")
    p_create.add_argument("--instruction", default="")
    p_create.add_argument("--bot-id", default=DEFAULT_BOT_ID,
                          help="Bot to add to the room (default: Jinx; use '' for none)")
    p_create.set_defaults(func=cmd_create)

    p_delete = sub.add_parser("delete", help="Delete a room by id")
    p_delete.add_argument("room_id")
    p_delete.set_defaults(func=cmd_delete)

    args = parser.parse_args()

    token = os.environ.get("KIMI_TOKEN")
    if not token:
        print("error: KIMI_TOKEN env var is not set", file=sys.stderr)
        sys.exit(1)

    args.func(args, token)


if __name__ == "__main__":
    main()
