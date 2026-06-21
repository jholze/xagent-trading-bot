#!/usr/bin/env python3
"""Print agent task brief for a pending Telegram /ask (used by hooks / manual dispatch)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from services.telegram_ask_bridge import (
        build_agent_task_brief,
        get_latest_pending_notification,
        list_pending_questions,
        read_agent_inbox,
    )

    inbox = read_agent_inbox()
    pending = list_pending_questions()
    if not pending:
        print("No pending /ask questions.")
        return 0

    item = pending[0]
    note = get_latest_pending_notification() or inbox or {}
    payload = {
        "id": item.get("id"),
        "question": item.get("question"),
        "created_at": item.get("created_at"),
        "context": item.get("context") or {},
        "response_mode": note.get("response_mode", "cursor_only"),
    }
    brief = (inbox or {}).get("task_brief") or build_agent_task_brief(payload)
    print(brief)
    if "--json" in sys.argv:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())