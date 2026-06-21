from datetime import datetime, timedelta

from data_manager import get_strategy_backtest_entry, list_strategy_targets, save_strategy_backtest_entry
from intelligence.strategy_backtest import StrategyBacktester, coin_key
from services.strategy_backtest_worker import StrategyBacktestWorker
from notifications.telegram_commands.command_context import activate_command
from telegram_notifier import send_telegram_message

_FORCE_COOLDOWN: dict[str, datetime] = {}


def _parse_symbol(text: str) -> tuple[str | None, str | None]:
    parts = text.strip().split()
    if len(parts) < 2:
        return None, None
    raw = parts[1].upper()
    if "/" not in raw:
        raw = f"{raw}/USDT"
    days = None
    if len(parts) >= 3:
        try:
            days = int(parts[2])
        except ValueError:
            days = None
    return raw, days


def handle(text: str) -> bool:
    if text == "/backtest":
        lines = StrategyBacktestWorker.get().status_lines()
        if not lines:
            send_telegram_message("📊 <b>Strategy Backtest</b>\n\nKeine Strategie-Coins in config.strategies.")
            return True
        msg = "📊 <b>Strategy Backtest</b> (adaptives Scheduling)\n\n" + "\n".join(lines)
        send_telegram_message(msg)
        return True

    if text == "/backtest_lock":
        activate_command("backtest_lock")
        send_telegram_message(
            "🔒 <b>/backtest_lock</b> — Coin vom Auto-Backtest ausschließen\n\n"
            "Danach nur Symbol senden, z.B. <code>ARIA</code>"
        )
        return True

    if text == "/backtest_results":
        activate_command("backtest_results")
        send_telegram_message(
            "📊 <b>/backtest_results</b> — Letztes Backtest-Ergebnis\n\n"
            "Danach nur Symbol senden, z.B. <code>HIGH</code>"
        )
        return True

    if text.startswith("/backtest_lock "):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if "/" not in sym:
            sym = f"{sym}/USDT"
        locked_any = False
        for entry in list_strategy_targets():
            if entry["symbol"] != sym:
                continue
            tf = entry.get("timeframe", "4h")
            key = coin_key(sym, tf)
            data = get_strategy_backtest_entry(key)
            data["locked"] = True
            save_strategy_backtest_entry(key, data)
            locked_any = True
        send_telegram_message(
            f"🔒 {sym} von Auto-Backtest ausgeschlossen." if locked_any else f"❌ {sym} nicht in strategies."
        )
        return True

    if text.startswith("/backtest_results "):
        sym = text.split(maxsplit=1)[1].strip().upper()
        if "/" not in sym:
            sym = f"{sym}/USDT"
        matches = []
        for entry in list_strategy_targets():
            if entry["symbol"] == sym:
                key = coin_key(sym, entry.get("timeframe", "4h"))
                matches.append(get_strategy_backtest_entry(key))
        if not matches:
            send_telegram_message(f"❌ Kein Backtest-Eintrag für {sym}")
            return True
        parts = [f"📊 <b>Backtest {sym}</b>"]
        for data in matches:
            m = data.get("metrics", {})
            parts.append(
                f"\n<b>{data.get('timeframe', '4h')}</b> — letzter Lauf: {data.get('last_run', '—')}\n"
                f"Churn: {m.get('signal_churn', 0)} | PnL sim: ${m.get('pnl_sim', 0)} | ATR: {m.get('atr_pct', 0)}%\n"
                f"Nächster Check: {data.get('next_review_at', '—')}\n"
                f"<i>{data.get('review_reason', '')}</i>"
            )
            if data.get("applied_params"):
                parts.append(f"Applied: <code>{data['applied_params']}</code>")
        send_telegram_message("\n".join(parts))
        return True

    if text.startswith("/backtest "):
        symbol, days = _parse_symbol(text[10:])
        if not symbol:
            send_telegram_message("❌ Nutzung: <code>/backtest SYMBOL [TAGE]</code>")
            return True
        cooldown_key = symbol
        last = _FORCE_COOLDOWN.get(cooldown_key)
        if last and datetime.now() - last < timedelta(hours=6):
            send_telegram_message("⏳ Force-Backtest Cooldown aktiv (6h).")
            return True
        entry = None
        for e in list_strategy_targets():
            if e["symbol"] == symbol:
                entry = e
                break
        if not entry:
            send_telegram_message(f"❌ {symbol} nicht in config.strategies")
            return True
        tf = entry.get("timeframe", "4h")
        send_telegram_message(f"⏳ Backtest <b>{symbol}</b> {tf} gestartet…")
        try:
            backtester = StrategyBacktester()
            result = backtester.compare_variants(symbol, tf, entry, days=days or None)
            m = result.metrics
            msg = (
                f"📊 <b>{symbol}</b> {tf} ({result.days}d)\n"
                f"Signale: {m.signal_churn} | PnL sim: ${m.pnl_sim:.1f} | Win: {m.win_rate:.0%}\n"
                f"ATR: {m.atr_pct:.1f}% | US-vol: {m.volume_profile.us_session_volume_ratio:.0%}"
            )
            if result.best_variant:
                msg += f"\nBessere Variante: +{result.improvement_pct:.1f}% — <code>{result.best_variant}</code>"
            send_telegram_message(msg)
            StrategyBacktestWorker.get().force_enqueue(symbol, tf)
            StrategyBacktestWorker.get().tick()
            _FORCE_COOLDOWN[cooldown_key] = datetime.now()
        except Exception as e:
            send_telegram_message(f"❌ Backtest fehlgeschlagen: {e}")
        return True

    return False