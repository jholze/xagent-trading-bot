from datetime import datetime, timedelta

from core.config import get_bot_config
from data_manager import load_effective_watchlist, load_trade_history, load_x_accounts
from services.order_service import OrderService, format_order_line, ledger_label
from intelligence.accuracy_tracker import AccuracyTracker
from price_fetcher import get_prices
from strategies.positions import list_active_positions
from terminal_ui import print_dashboard


def _win_rate(history: dict) -> str:
    sells = [t for t in history.get("trades", []) if t.get("type") == "SELL" and "pnl" in t]
    if not sells:
        return "—"
    wins = sum(1 for t in sells if t.get("pnl", 0) > 0)
    return f"{wins / len(sells) * 100:.0f}%"


def _portfolio_snapshot(trading_mode: str = None) -> dict:
    """Cash, PnL, and position values for dashboard and cycle summary."""
    from data_manager import (
        is_dry_run_enhanced,
        load_live_trade_history,
        uses_exchange_ledger,
        uses_simulated_live_portfolio,
    )
    from notifications.telegram_commands.position_display import (
        _position_metrics,
        build_price_fallbacks,
        position_symbol,
    )
    from price_fetcher import get_prices_batch

    cfg = get_bot_config()
    mode = trading_mode or cfg.trading_mode

    if uses_exchange_ledger(mode):
        history = load_live_trade_history()
        if uses_simulated_live_portfolio(cfg.raw):
            balance = float(history.get("virtual_balance", cfg.simulated_balance_usdt))
            balance_label = "Sim USDT" if is_dry_run_enhanced(cfg.raw) else "Dry Run USDT"
        else:
            balance = history.get("virtual_balance")
            if balance is None:
                try:
                    from services.gate_balance import fetch_usdt_balance
                    balance = fetch_usdt_balance(cfg)
                except Exception:
                    balance = 0.0
            balance = float(balance or 0)
            balance_label = "USDT (Gate)"
        realized = float(history.get("total_pnl", history.get("realized_pnl", 0)))
    else:
        history = load_trade_history()
        balance = float(history.get("virtual_balance", 0))
        realized = float(history.get("realized_pnl", 0))
        balance_label = "Balance"

    active = list_active_positions()
    symbols = [position_symbol(p) for p in active]
    prices = get_prices_batch(symbols, fallbacks=build_price_fallbacks(active)) if symbols else {}

    unrealized = 0.0
    positions_market_value = 0.0
    for pos in active:
        sym = position_symbol(pos)
        metrics = _position_metrics(pos, float(prices.get(sym, 0) or 0))
        unrealized += metrics["unreal"]
        positions_market_value += metrics["value_usdt"]

    return {
        "history": history,
        "balance": balance,
        "balance_label": balance_label,
        "realized": realized,
        "unrealized": unrealized,
        "positions_market_value": positions_market_value,
        "total_value": balance + positions_market_value,
    }


def build_dashboard_data(
    cycle_signals: list = None,
    coin_results: list = None,
    trading_mode: str = "paper",
    next_update: int = 60,
) -> dict:
    snap = _portfolio_snapshot(trading_mode)
    history = snap["history"]
    balance = snap["balance"]
    realized = snap["realized"]
    unrealized = snap["unrealized"]
    total_value = snap["total_value"]
    watchlist = load_effective_watchlist()
    active_coins = [c["symbol"].split("/")[0] for c in watchlist if c.get("active", True)]

    signal_lines = list(cycle_signals or [])
    for result in coin_results or []:
        line = (
            f"→ {result.get('symbol')} | {result.get('action')} "
            f"({result.get('normalized_action')}) | RSI {result.get('rsi', 0):.1f} | "
            f"{result.get('ampel_emoji', '')} {result.get('rationale', '')[:40]}"
        )
        if result.get("executed"):
            line += " | ✓ executed"
        signal_lines.append(line)

    if not signal_lines:
        signal_lines = ["No strong signals this cycle..."]

    board = AccuracyTracker().get_leaderboard()[:5]
    trust_lines = [
        f"@{row['handle']} trust {row['trust_score']:.0f} | hit {row['hit_rate']*100:.0f}%"
        for row in board
    ]
    signal_lines.extend([""] + trust_lines[:4])

    accounts = [a.get("handle", a) for a in load_x_accounts()[:6]]

    return {
        "balance": f"${balance:,.0f}",
        "unrealized": f"${unrealized:,.1f}",
        "realized_pnl": f"${realized:,.1f}",
        "total_value": f"${total_value:,.0f}",
        "active_positions": len(list_active_positions()),
        "win_rate": _win_rate(history),
        "coins": active_coins[:8],
        "x_accounts": accounts,
        "signals": signal_lines,
        "last_cycle": datetime.now().strftime("%H:%M:%S"),
        "status": f"🟢 Running | {trading_mode.upper()}",
        "next_update": next_update,
        "trading_mode": trading_mode.upper(),
        "trust_leaderboard": board,
    }


def render_cycle_dashboard(
    cycle_signals: list = None,
    coin_results: list = None,
    trading_mode: str = "paper",
    next_update: int = 60,
):
    cfg = get_bot_config()
    if not cfg.raw.get("observability", {}).get("terminal_dashboard", True):
        return
    data = build_dashboard_data(cycle_signals, coin_results, trading_mode, next_update)
    print_dashboard(data)


def _parse_trade_timestamp(trade: dict):
    raw = trade.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", ""))
    except Exception:
        return None


def _trade_source_label(source: str) -> str:
    labels = {
        "manual": "manuell",
        "auto": "Auto",
        "x": "X-Signal",
        "cmc": "CMC",
        "lc": "LunarCrush",
    }
    return labels.get(source or "auto", source or "Auto")


def format_recent_trade_line(trade: dict) -> str:
    from notifications.coin_links import format_ticker_html

    sym = (trade.get("symbol") or "").replace("/USDT", "")
    sym_html = format_ticker_html(sym, symbol_suffix="")
    typ = trade.get("type", "?")
    src = _trade_source_label(trade.get("source", "auto"))
    if typ == "BUY":
        usdt = float(trade.get("usdt_amount", 0) or 0)
        return f"  · {typ} <b>{sym_html}</b> · ${usdt:.0f} · <i>{src}</i>"
    usdt = float(trade.get("usdt_received", 0) or 0)
    pnl = trade.get("pnl")
    pnl_part = f" · PnL <b>${float(pnl):+.1f}</b>" if pnl is not None else ""
    return f"  · {typ} <b>{sym_html}</b> · ${usdt:.0f}{pnl_part} · <i>{src}</i>"


def recent_trades_lines(history: dict, hours: float = 24, limit: int = 5) -> list[str]:
    cutoff = datetime.now() - timedelta(hours=hours)
    recent = []
    for trade in reversed(history.get("trades", [])):
        ts = _parse_trade_timestamp(trade)
        if ts is not None and ts < cutoff:
            continue
        recent.append(trade)
        if len(recent) >= limit:
            break
    if not recent:
        return ["  <i>Keine Trades in den letzten 24h.</i>"]
    return [format_recent_trade_line(t) for t in recent]


def recent_orders_lines(hours: float = 24, limit: int = 5) -> list[str]:
    ledger = OrderService()
    orders, _ = ledger.list_orders(hours=hours, page=1, per_page=limit)
    if not orders:
        return [f"  <i>Keine Orders in den letzten {int(hours)}h ({ledger_label()}).</i>"]
    return [f"  {format_order_line(o)}" for o in orders]


def build_cycle_summary(
    coin_results: list = None,
    trading_mode: str = "paper",
    x_signal_count: int = 0,
    cmc_signal_count: int = 0,
    lc_signal_count: int = 0,
    top_x: str = "",
    top_cmc: str = "",
    top_lc: str = "",
) -> str:
    snap = _portfolio_snapshot(trading_mode)
    balance = snap["balance"]
    balance_label = snap["balance_label"]
    realized = snap["realized"]
    total_value = snap["total_value"]

    executed = [r for r in (coin_results or []) if r.get("executed")]
    actions = [r for r in (coin_results or []) if r.get("normalized_action") != "HOLD"]

    lines = [
        f"<b>📋 Zyklus-Zusammenfassung</b> — {datetime.now().strftime('%H:%M:%S')}",
        f"Modus: <b>{trading_mode.upper()}</b>",
        f"{balance_label}: ${float(balance or 0):,.0f} | "
        f"Gesamtwert: ${float(total_value or 0):,.0f} | Realized: ${float(realized or 0):,.1f}",
        f"Signale: {len(actions)} handelbar | {x_signal_count} X | {cmc_signal_count} CMC | {lc_signal_count} LC",
    ]
    if top_x or top_cmc or top_lc:
        lines.append("<b>Social:</b>")
        if top_x:
            lines.append(f"  🐦 {top_x}")
        if top_cmc:
            lines.append(f"  📊 {top_cmc}")
        if top_lc:
            lines.append(f"  🌙 {top_lc}")
    if actions:
        lines.append("<b>Entscheidungen:</b>")
        from notifications.coin_links import format_ticker_html

        for r in actions[:6]:
            sym = (r.get("symbol") or "").replace("/USDT", "")
            sym_html = format_ticker_html(sym, symbol_suffix="")
            act = r.get("normalized_action") or r.get("action")
            why = (r.get("why_de") or r.get("rationale") or "")[:80]
            status = "✅" if r.get("executed") else "🚫" if r.get("trade_message") else "👀"
            lines.append(f"  {status} {sym_html} {act}: {why}")
    if executed:
        lines.append(f"<b>Ausgeführt:</b> {len(executed)} Trade(s)")
        for r in executed[:5]:
            lines.append(f"  • {r.get('symbol')} {r.get('order_type')}")
    else:
        lines.append("Keine Auto-Trades in diesem Zyklus.")

    ledger = OrderService()
    stats = ledger.stats_24h()
    lines.append("")
    lines.append(f"<b>Orders (24h, {ledger_label()}):</b> "
                 f"✅{stats['filled']} ❌{stats['rejected']} 🚫{stats['cancelled']}")
    lines.extend(recent_orders_lines())
    lines.append("<i>Details: <code>/decisions</code> · <code>/orders</code> · /buy · /sell</i>")
    return "\n".join(lines)