#!/usr/bin/env python3
"""Watch an OpenClaw agent session and print completed assistant messages.

Only assistant messages are emitted, one JSON object per line (JSONL), after
the message is complete — no incremental streaming:
    {"timestamp": "...", "role": "assistant", "text": "..."}

Usage:
    ./watch-session.py <sessionKey>            # e.g. agent:main:main or agent:main:kimi-claw:kw136g
    ./watch-session.py <sessionKey> --all      # also print existing history first
    ./watch-session.py --file <path.jsonl>     # watch a trajectory file directly

Resolves sessionKey -> trajectory file via
~/.openclaw/agents/main/sessions/sessions.json (re-read every poll, so newly
created sessions are picked up). Polls once per second. Ctrl+C to stop.
"""

import argparse
import json
import os
import sys
import time

SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions")
SESSIONS_INDEX = os.path.join(SESSIONS_DIR, "sessions.json")
POLL_S = 1.0


def resolve_file(key):
    """Map a session key to its trajectory file, or None if not known yet."""
    try:
        with open(SESSIONS_INDEX) as f:
            idx = json.load(f)
    except OSError:
        return None
    except ValueError:
        return None
    sid = (idx.get(key) or {}).get("sessionId")
    return os.path.join(SESSIONS_DIR, f"{sid}.jsonl") if sid else None


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def print_entry(line):
    """Print one completed assistant message as a single JSON line (no streaming)."""
    try:
        d = json.loads(line)
    except ValueError:
        return
    if d.get("type") != "message":
        return
    m = d.get("message") or {}
    if m.get("role") != "assistant":
        return
    text = extract_text(m.get("content")).strip()
    if not text:
        return
    print(json.dumps({"timestamp": d.get("timestamp"), "role": "assistant", "text": text},
                     ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser(description="Watch an OpenClaw agent session trajectory.")
    ap.add_argument("session_key", nargs="?", help="e.g. agent:main:main")
    ap.add_argument("--file", help="watch a trajectory .jsonl file directly")
    ap.add_argument("--all", action="store_true", help="print existing history first")
    args = ap.parse_args()

    if not args.file and not args.session_key:
        ap.error("give a session key or --file")

    file = args.file
    if file is None:
        file = resolve_file(args.session_key)
        while file is None:
            print(f"session '{args.session_key}' not in sessions.json yet — retrying...", file=sys.stderr)
            time.sleep(POLL_S)
            file = resolve_file(args.session_key)

    print(f"[watching {file} — Ctrl+C to stop]", file=sys.stderr)
    seen = 0
    try:
        while True:
            try:
                with open(file) as f:
                    lines = [ln for ln in f.read().splitlines() if ln.strip()]
            except OSError:
                lines = None
            if lines is not None:
                if len(lines) < seen:
                    seen = 0  # file rotated/truncated
                start = seen if (seen > 0 or args.all) else len(lines)
                for line in lines[start:]:
                    print_entry(line)
                seen = len(lines)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        print("\n[stopped]", file=sys.stderr)


if __name__ == "__main__":
    main()
