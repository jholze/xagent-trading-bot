#!/usr/bin/env python3
"""Read the latest pending /ask for the Cursor agent (run after notify)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from services.telegram_ask_bridge import get_latest_pending_notification, list_pending_questions

    pending = list_pending_questions()
    if not pending:
        print("No pending /ask questions.")
        return

    note = get_latest_pending_notification() or {}
    item = pending[0]
    out = {
        "id": item.get("id"),
        "question": item.get("question"),
        "created_at": item.get("created_at"),
        "context": item.get("context") or {},
        "notify_at": note.get("notify_at"),
    }
    from services.telegram_ask_bridge import _response_mode

    out["response_mode"] = _response_mode()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(
        f"\nModus: {out['response_mode']} (keine Grok-API im cursor_only-Modus)\n"
        f"Reply:\n  python3 scripts/ask_bridge_reply.py {out['id']} \"Deine Antwort\"",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()