#!/usr/bin/env python3
"""Notify Cursor when new Telegram /ask questions arrive (cursor_only: no Grok API)."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _emit_notify(payload: dict, *, human: bool = True) -> None:
    from services.telegram_ask_bridge import (
        format_agent_action,
        format_cursor_notify,
        write_agent_inbox,
    )

    write_agent_inbox(payload)
    print(format_agent_action(payload), flush=True)
    line = format_cursor_notify(payload)
    print(line, flush=True)
    if human:
        mode = payload.get("response_mode", "cursor_only")
        if mode == "cursor_only":
            hint = (
                f"\n[Cursor Ask] #{payload.get('id')} — {payload.get('question')}\n"
                "Modus: cursor_only (keine Grok-API). Agent antwortet mit vollem Bot-Kontext.\n"
                f"Reply: {payload.get('reply_cmd', 'python3 scripts/ask_bridge_reply.py …')}\n"
            )
        else:
            priority = payload.get("cursor_priority_sec", 8)
            hint = (
                f"\n[Cursor Ask] #{payload.get('id')} — {payload.get('question')}\n"
                f"Cursor hat Priorität ({priority}s); danach Grok-Fallback.\n"
            )
        print(hint, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Watch Telegram /ask and notify Cursor agent")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--interval", type=float, default=0.5, help="Poll interval seconds")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only emit @@CURSOR_ASK_NOTIFY@@ (no human-readable lines)",
    )
    args = parser.parse_args()

    from services.telegram_ask_bridge import (
        _cfg,
        _response_mode,
        auto_answer_if_pending,
        auto_respond_enabled,
        get_latest_pending_notification,
        list_pending_questions,
        pending_notify_path,
        queue_path,
    )

    notified_at: dict[str, float] = {}
    def _priority_sec() -> float:
        return float(_cfg().get("cursor_priority_sec", 8))

    def _renotify_sec() -> float:
        return float(_cfg().get("cursor_renotify_sec", 30))

    def _try_auto_respond(pending_items: list[dict]) -> bool:
        if not auto_respond_enabled():
            return False
        pending_ids = {str(q.get("id")) for q in pending_items}
        now = time.time()
        priority = _priority_sec()
        acted = False
        for qid, ts in list(notified_at.items()):
            if qid not in pending_ids:
                notified_at.pop(qid, None)
                continue
            if now - ts < priority:
                continue
            ok, _ = auto_answer_if_pending(qid)
            if ok:
                notified_at.pop(qid, None)
                acted = True
        return acted

    def _should_notify(qid: str) -> bool:
        last = notified_at.get(qid)
        if last is None:
            return True
        return time.time() - last >= _renotify_sec()

    def tick() -> bool:
        pending_items = list_pending_questions()
        if not pending_items:
            notified_at.clear()
            return False

        if _try_auto_respond(pending_items):
            return True

        payload = get_latest_pending_notification()
        if not payload:
            item = pending_items[0]
            payload = {
                "event": "new_ask",
                "id": item.get("id"),
                "question": item.get("question"),
                "created_at": item.get("created_at"),
                "context": item.get("context") or {},
                "response_mode": _response_mode(),
                "reply_cmd": f"python3 scripts/ask_bridge_reply.py {item.get('id')} \"…\"",
            }

        qid = str(payload.get("id") or "")
        if not qid or not _should_notify(qid):
            return False

        if not any(q.get("id") == qid for q in pending_items):
            return False

        payload["response_mode"] = _response_mode()
        payload["cursor_priority_sec"] = _priority_sec()
        notified_at[qid] = time.time()
        _emit_notify(payload, human=not args.quiet)
        return True

    mode = _response_mode()
    if args.once:
        if not tick():
            print(f"No pending notifications ({pending_notify_path()}, {queue_path()})")
        return

    if mode == "cursor_only":
        banner = f"cursor_only, re-notify {_renotify_sec()}s (dispatch via bot poller)"
    else:
        banner = f"grok_fallback after {_priority_sec()}s"
    print(
        f"Cursor ask watcher: {pending_notify_path()} + {queue_path()} every {args.interval}s ({banner})",
        flush=True,
    )
    while True:
        tick()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()