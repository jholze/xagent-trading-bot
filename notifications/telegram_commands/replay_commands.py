"""Tier-2 evaluation jobs: /churn_replay, /counterfactual, /session_cancel."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from bus.jobs import heavy_job_queue
from bus.sessions import session_manager
from hermes.churn_replay import analyze_churn, format_telegram_summary
from hermes.counterfactual import replay_window
from notifications.telegram_commands.command_context import current_chat_id
from telegram_notifier import send_telegram_message


def _normalize_symbol(raw: str) -> str:
    sym = raw.strip().upper()
    if "/" not in sym:
        sym = f"{sym}/USDT"
    return sym


def _parse_since(parts: list[str]) -> datetime | None:
    for i, p in enumerate(parts):
        if p == "--since" and i + 1 < len(parts):
            try:
                return datetime.fromisoformat(parts[i + 1].replace("Z", ""))
            except ValueError:
                return None
    return datetime.now() - timedelta(days=30)


def _run_churn_replay(symbol: str, since: datetime | None):
    try:
        result = analyze_churn(symbol, since=since)
        send_telegram_message(format_telegram_summary(result))
    except Exception as e:
        send_telegram_message(f"❌ Churn replay fehlgeschlagen: {e}")


def _run_counterfactual(symbol: str, days: int):
    try:
        end = datetime.now()
        start = end - timedelta(days=days)
        from core.config import get_bot_config

        cfg = get_bot_config()
        tf = "4h"
        for entry in cfg._raw.get("strategies", []):
            if entry.get("symbol") == symbol:
                tf = entry.get("timeframe", "4h")
                break
        summary = replay_window(symbol, tf, start, end)
        if summary.get("error"):
            send_telegram_message(f"❌ Counterfactual: {summary['error']}")
            return
        lines = [f"📊 <b>Counterfactual {symbol}</b> ({days}d)"]
        for name, row in (summary.get("variants") or {}).items():
            lines.append(
                f"<b>{name}</b>: sells {row.get('sells', 0)}, "
                f"sharpe {row.get('sharpe', 0):.2f}, win {row.get('win_rate', 0):.0%}"
            )
        send_telegram_message("\n".join(lines))
    except Exception as e:
        send_telegram_message(f"❌ Counterfactual fehlgeschlagen: {e}")


def handle(text: str) -> bool:
    if text == "/session_cancel":
        if session_manager.end(chat_id=current_chat_id() or os.getenv("TELEGRAM_CHAT_ID")):
            try:
                from bus.notifications import notification_publisher

                notification_publisher.flush_deferred()
            except Exception:
                pass
            send_telegram_message("✅ Aktive Session beendet. Gepufferte Nachrichten werden nachgereicht.")
        else:
            send_telegram_message("ℹ️ Keine aktive HEAVY-Session.")
        return True

    if text.startswith("/churn_replay"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram_message("❌ Nutzung: <code>/churn_replay SYMBOL [--since ISO-DATUM]</code>")
            return True
        symbol = _normalize_symbol(parts[1])
        since = _parse_since(parts)
        chat_id = current_chat_id() or os.getenv("TELEGRAM_CHAT_ID", "")
        job_id, err = heavy_job_queue.enqueue(
            "churn_replay",
            chat_id,
            lambda: _run_churn_replay(symbol, since),
            params={"symbol": symbol, "since": since.isoformat() if since else None},
        )
        if err:
            send_telegram_message(err)
            return True
        send_telegram_message(f"⏳ Churn replay <b>{symbol}</b> gestartet (Job {job_id})…")
        return True

    if text.startswith("/counterfactual"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram_message("❌ Nutzung: <code>/counterfactual SYMBOL [TAGE]</code>")
            return True
        symbol = _normalize_symbol(parts[1])
        days = 7
        if len(parts) >= 3:
            try:
                days = max(1, min(90, int(parts[2])))
            except ValueError:
                pass
        chat_id = current_chat_id() or os.getenv("TELEGRAM_CHAT_ID", "")
        job_id, err = heavy_job_queue.enqueue(
            "counterfactual",
            chat_id,
            lambda: _run_counterfactual(symbol, days),
            params={"symbol": symbol, "days": days},
        )
        if err:
            send_telegram_message(err)
            return True
        send_telegram_message(f"⏳ Counterfactual <b>{symbol}</b> ({days}d) gestartet (Job {job_id})…")
        return True

    return False