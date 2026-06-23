"""Morning briefing: once-per-day /morning summary for Telegram."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from core.build_info import format_build_line
from core.config import get_bot_config
from data_manager import atomic_write_json
from notifications.daily_stats import BOT_ROOT, window_stats
_STATE_FILE = BOT_ROOT / "data" / "morning_briefing.json"


def _current_chat_id() -> str:
    from notifications.telegram_commands.command_context import current_chat_id

    return current_chat_id()
_CHUNK_LIMIT = 4000


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_state() -> dict:
    if not _STATE_FILE.exists():
        return {"by_chat": {}}
    try:
        with _STATE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"by_chat": {}}


def _save_state(data: dict) -> None:
    atomic_write_json(str(_STATE_FILE), data)


def can_send_morning(chat_id: str | None = None) -> tuple[bool, str | None]:
    cid = str(chat_id or _current_chat_id() or "").strip()
    if not cid:
        return True, None
    state = _load_state()
    entry = (state.get("by_chat") or {}).get(cid)
    if not entry:
        return True, None
    if entry.get("date") != _today_key():
        return True, None
    sent_at = entry.get("sent_at", "")
    try:
        ts = datetime.fromisoformat(sent_at)
        time_label = ts.strftime("%H:%M")
    except Exception:
        time_label = sent_at or "?"
    return False, time_label


def mark_morning_sent(chat_id: str | None = None) -> None:
    cid = str(chat_id or _current_chat_id() or "").strip()
    if not cid:
        return
    state = _load_state()
    by_chat = dict(state.get("by_chat") or {})
    by_chat[cid] = {
        "date": _today_key(),
        "sent_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_state({"by_chat": by_chat})


def _split_telegram(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def build_morning_briefing(chat_id: str | None = None) -> list[str]:
    cfg = get_bot_config()
    if not cfg.observability_config.get("morning_briefing_enabled", True):
        return ["<b>☀️ Morning Briefing</b>\n\nDeaktiviert in config (<code>morning_briefing_enabled</code>)."]

    now = datetime.now()
    since = now - timedelta(hours=24)
    stats = window_stats(BOT_ROOT, since, until=now)
    dec = stats["decisions"]

    portfolio = {}
    risk = {}
    try:
        from notifications.terminal_dashboard import _portfolio_snapshot
        from services.trading_service import TradingService

        trading = TradingService()
        trading.refresh()
        portfolio = _portfolio_snapshot(cfg.trading_mode)
        risk = trading.risk.status_summary()
    except Exception:
        portfolio = {
            "total_value": stats["cash"] + stats["pos_value"],
            "balance": stats["cash"],
            "open_positions": stats["open_count"],
        }

    live = cfg.raw.get("live", {})
    build_line = format_build_line(html=True)

    trade_lines = []
    for trade in sorted(stats["trades"], key=lambda t: t["timestamp"])[-6:]:
        from notifications.daily_stats import parse_ts

        ts = parse_ts(trade["timestamp"]).strftime("%d.%m. %H:%M")
        usdt = trade.get("usdt_amount") or trade.get("usdt_received") or 0
        src = trade.get("source", "?")
        if trade["type"] == "SELL":
            pnl = trade.get("pnl") or 0
            trade_lines.append(
                f"• {ts} {trade['type']} {trade['symbol']} ${usdt:,.0f} ({src}) PnL {pnl:+.1f}"
            )
        else:
            trade_lines.append(f"• {ts} {trade['type']} {trade['symbol']} ${usdt:,.0f} ({src})")
    if not trade_lines:
        trade_lines.append("• — keine Trades in 24h —")

    highlight_lines = []
    for item in stats["highlights"]:
        exec_mark = "✓" if item["executed"] else "·"
        rat = f" — {item['rationale']}" if item["rationale"] else ""
        highlight_lines.append(
            f"{exec_mark} {item['time']} {item['symbol']} {item['action']}{rat}"
        )
    if not highlight_lines:
        highlight_lines.append("• — keine Highlights —")

    social_lines = stats["social"] or ["• — keine CMC/LC BUY/SELL —"]

    total_value = float(portfolio.get("total_value", 0) or 0)
    balance = float(portfolio.get("balance", stats["cash"]) or 0)
    open_pos = int(portfolio.get("open_positions", stats["open_count"]) or 0)

    msg = (
        f"<b>☀️ Morning Briefing</b>\n"
        f"<i>{now.strftime('%Y-%m-%d %H:%M')} · letzte 24h</i>\n"
        f"{build_line}\n"
        f"Modus: <code>{cfg.trading_mode}</code> · dry_run=<code>{live.get('dry_run')}</code>\n\n"
        f"<b>Portfolio jetzt</b>\n"
        f"NAV <b>${total_value:,.0f}</b> · Cash ${balance:,.0f} · {open_pos} Positionen\n"
        f"Realized gesamt {stats['realized_total']:+.1f} USDT\n\n"
        f"<b>Risk</b>\n"
        f"Equity ${float(risk.get('portfolio_equity', total_value) or 0):,.0f} · "
        f"Drawdown {float(risk.get('drawdown_pct', 0) or 0):.1f}%\n"
        f"Daily buys {risk.get('daily_buys', '?')}/{risk.get('max_daily_buys', '?')} · "
        f"sells {risk.get('daily_sells', 0)}/{risk.get('max_daily_sells', 0) or '∞'}\n\n"
        f"<b>Aktivität 24h</b>\n"
        f"Trades {len(stats['trades'])} ({stats['buys']} BUY / {stats['sells']} SELL"
        f"{f', {stats['dca_buys']} DCA' if stats['dca_buys'] else ''})\n"
        f"Sell-PnL {stats['sell_pnl']:+.1f} USDT · "
        f"Orders {len(stats['orders'])} ({stats['filled_orders']} filled / "
        f"{stats['rejected_orders']} rejected)\n"
        f"Entscheidungen {dec['total']} · DCA {dec['buy_dca']} "
        f"(exec {dec['buy_dca_executed']}, shadow {dec['buy_dca_shadow']})\n\n"
        f"<b>Highlights</b>\n"
        + "\n".join(highlight_lines)
        + "\n\n<b>Letzte Trades</b>\n"
        + "\n".join(trade_lines)
        + "\n\n<b>Social</b>\n"
        + "\n".join(social_lines)
        + f"\n\n<b>{stats['hermes']}</b>"
    )
    return _split_telegram(msg)


def send_morning_briefing(chat_id: str | None = None) -> bool:
    from telegram_notifier import send_telegram_message

    cid = chat_id or _current_chat_id()
    allowed, sent_time = can_send_morning(cid)
    if not allowed:
        send_telegram_message(
            f"☀️ <b>Morning Briefing</b> für heute bereits gesendet "
            f"(um {sent_time} Uhr).\nNächstes Briefing: morgen mit <code>/morning</code>."
        )
        return True

    chunks = build_morning_briefing(cid)
    ok = True
    for chunk in chunks:
        if not send_telegram_message(chunk, chat_id=cid):
            ok = False
    if ok:
        mark_morning_sent(cid)
    return ok