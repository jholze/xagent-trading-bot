#!/usr/bin/env python3
"""Auto-answer a pending /ask via Grok Build headless (no xAI API, no manual chat)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _grok_bin() -> str:
    custom = os.getenv("GROK_BIN", "").strip()
    if custom:
        return custom
    found = shutil.which("grok")
    return found or "grok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless Grok Build dispatch for Telegram /ask")
    parser.add_argument("question_id", help="Pending question id")
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()

    from services.telegram_ask_bridge import build_agent_task_brief, get_question

    item = get_question(args.question_id)
    if not item:
        print(f"Question #{args.question_id} not found", file=sys.stderr)
        return 1
    if item.get("status") != "pending":
        print(f"Question #{args.question_id} is {item.get('status')}, skip")
        return 0

    payload = {
        "id": item.get("id"),
        "question": item.get("question"),
        "context": item.get("context") or {},
        "response_mode": "cursor_only",
    }
    prompt = build_agent_task_brief(payload)
    grok = _grok_bin()
    cmd = [
        grok,
        "-p",
        prompt,
        "--cwd",
        str(ROOT),
        "--yolo",
        "--max-turns",
        str(args.max_turns),
        "--disallowed-tools",
        "Agent",
        "--allow",
        "Bash(python3 scripts/ask_bridge_reply.py*)",
        "--allow",
        "Bash(python3 scripts/ask_bridge_handle_pending.py*)",
        "--allow",
        "Read",
        "--allow",
        "Grep",
    ]

    print(f"Headless dispatch #{args.question_id} via {grok}", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, timeout=300)
    except subprocess.TimeoutExpired:
        print(f"Headless dispatch timeout for #{args.question_id}", file=sys.stderr)
        return 1

    item = get_question(args.question_id)
    if item and item.get("status") in ("answered", "delivered"):
        print(f"Headless dispatch OK #{args.question_id} -> {item.get('status')}")
        return 0 if proc.returncode == 0 else 0

    print(f"Headless dispatch finished but #{args.question_id} still pending", file=sys.stderr)
    return 1 if proc.returncode != 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())