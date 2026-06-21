#!/usr/bin/env python3
"""Re-queue agent work while Telegram /ask questions are still pending."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    hook_in = {}
    if not sys.stdin.isatty():
        try:
            hook_in = json.load(sys.stdin)
        except json.JSONDecodeError:
            hook_in = {}

    out: dict = {}
    loop_count = int(hook_in.get("loop_count", 0) or 0)
    if loop_count >= 4:
        print("{}")
        return

    from services.telegram_ask_bridge import build_agent_task_brief, list_pending_questions

    pending = list_pending_questions()
    if not pending:
        print("{}")
        return

    item = pending[0]
    payload = {
        "id": item.get("id"),
        "question": item.get("question"),
        "context": item.get("context") or {},
        "response_mode": "cursor_only",
    }
    out["followup_message"] = build_agent_task_brief(payload)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()