from core.config import get_bot_config
from data_manager import load_trade_history
from price_fetcher import get_prices_batch
from strategies.positions import list_active_positions
from telegram_notifier import send_telegram_message


def _position_symbol(p: dict) -> str:
    sym = p["symbol"]
    return sym if "/" in sym else f"{sym}/USDT"


def handle(text: str) -> bool:
    if text not in ["/positions", "/status", "/balance"]:
        return False

    active = list_active_positions()
    history = load_trade_history()

    symbols = [_position_symbol(p) for p in active]
    prices = get_prices_batch(symbols)

    total_unreal = 0.0
    for p in active:
        sym = _position_symbol(p)
        price = prices.get(sym, 0.0)
        entry = p.get("average_entry", p.get("entry_price", 0))
        if price > 0 and entry > 0:
            total_unreal += (price - entry) * p["amount"]

    total_value = history.get("virtual_balance", 0) + total_unreal
    total_pnl = history.get("realized_pnl", 0) + total_unreal
    initial = get_bot_config().initial_capital_usdt
    pnl_pct = (total_pnl / initial * 100) if initial > 0 and total_pnl != 0 else 0

    msg = f"""<b>📊 Portfolio Overview</b>

Balance: <b>${history.get("virtual_balance", 0):.0f}</b>
Unrealized: <b>${total_unreal:.1f}</b>
Total Value: <b>${total_value:.0f}</b>
Total PnL: <b>${total_pnl:.1f}</b> ({pnl_pct:.1f}%)

<b>Active Positions ({len(active)}):</b>
"""
    msg += "──────────────────────────────────\n"
    for p in active:
        sym = _position_symbol(p)
        price = prices.get(sym, 0.0)
        highlight = p.get("highlight", "")
        entry = p.get("average_entry", p.get("entry_price", 0))
        amount = float(p["amount"])
        unreal = (price - entry) * amount if entry > 0 and price > 0 else 0
        unreal_pct = (unreal / (entry * amount) * 100) if entry > 0 and amount > 0 else 0
        msg += (
            f"{highlight}{p['symbol']:8} | Amt: {amount:.4f} | "
            f"Entry: ${entry:.4f} | Unreal: ${unreal:.1f} ({unreal_pct:+.1f}%)\n"
        )
        msg += "──────────────────────────────────\n"

    msg += "\n<b>── Last Trades ──</b>\n"
    msg += "──────────────────────────────────\n"
    trades = history.get("trades", [])[-8:]
    for t in reversed(trades):
        ts = t.get("timestamp", "")[:16].replace("T", " ")
        typ = "🟢 BUY" if t.get("type") == "BUY" else "🔴 SELL"
        pnl_str = f" PnL:${t.get('pnl', 0):+.1f}" if t.get("pnl") is not None else ""
        msg += (
            f"{ts} | {typ} | {t.get('symbol',''):<10} | "
            f"${t.get('price',0):.4f} | Amt:{t.get('amount',0):.4f}{pnl_str}\n"
        )
        msg += "──────────────────────────────────\n"

    send_telegram_message(msg)
    return True