"""Telegram /stack — prod vs staging observability report."""

from __future__ import annotations

import html
import threading
from datetime import datetime, timedelta
from pathlib import Path

from core.runtime_identity import resolve_bot_stack
from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message


def _remote_logs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "logs" / "remote"


def _parse_hours(text: str) -> float | None:
    parts = text.strip().split()
    if not parts or parts[0] != "/stack":
        return None
    if len(parts) == 1:
        return 24.0
    try:
        hours = float(parts[1].replace(",", "."))
    except ValueError:
        return None
    return max(1.0, min(hours, 168.0))


def _run_stack_report(hours: float) -> None:
    from services.stack_compare import build_stack_compare_report, format_stack_compare_telegram

    try:
        until = datetime.now()
        since = until - timedelta(hours=hours)
        remote = _remote_logs_dir()

        staging_dec = prod_dec = staging_snap = prod_snap = None
        if remote.exists():
            staging_dec = sorted(remote.glob("staging_decisions*.jsonl")) or None
            prod_dec = sorted(remote.glob("prod_decisions*.jsonl")) or None
            staging_snap = sorted(remote.glob("staging_snapshots*.jsonl")) or None
            prod_snap = sorted(remote.glob("prod_snapshots*.jsonl")) or None

        report = build_stack_compare_report(
            since=since,
            until=until,
            staging_decision_paths=staging_dec,
            prod_decision_paths=prod_dec,
            staging_snapshot_paths=staging_snap,
            prod_snapshot_paths=prod_snap,
        )
        chunks = format_stack_compare_telegram(report, local_stack=resolve_bot_stack())
        for i, chunk in enumerate(chunks):
            prefix = "<i>📊 Stack Compare (Fortsetzung)</i>\n\n" if i else ""
            if not send_telegram_message(prefix + chunk):
                plain = html.unescape(prefix + chunk)
                send_telegram_message(plain, parse_mode=None)
    except Exception as e:
        send_telegram_message(t("stack_failed", error=html.escape(str(e))))


def handle(text: str) -> bool:
    hours = _parse_hours(text)
    if hours is None:
        return False

    send_telegram_message(
        f"📊 <b>Stack Compare</b> wird erstellt…\n<i>Lookback: {hours:g}h</i>"
    )
    threading.Thread(
        target=_run_stack_report,
        args=(hours,),
        daemon=True,
        name="stack-cmd",
    ).start()
    return True