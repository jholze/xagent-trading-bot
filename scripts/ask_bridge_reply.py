#!/usr/bin/env python3
"""Submit a Cursor answer to the Telegram /ask bridge."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Reply to a Telegram /ask question")
    parser.add_argument("question_id", help="Question id from ask_bridge_watcher")
    parser.add_argument("answer", help="Answer text (quote if it contains spaces)")
    parser.add_argument(
        "--by",
        default="cursor",
        choices=("cursor", "headless", "grok"),
        help="Answer source label (default: cursor)",
    )
    args = parser.parse_args()

    from services.telegram_ask_bridge import submit_answer

    ok, err = submit_answer(args.question_id, args.answer, answered_by=args.by)
    if not ok:
        print(f"Error: {err}")
        sys.exit(1)
    print(f"Answer queued for #{args.question_id} — bot poller will send to Telegram.")


if __name__ == "__main__":
    main()