from datetime import datetime, timedelta

from core.config import get_bot_config
from core.portfolio_baseline import initial_capital
from data_manager import (
    load_effective_watchlist,
    load_trade_history,
    load_x_accounts,
    resolve_ledger_scope,
)
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
        uses_exchange_ledger,
        uses_simulated_live_portfolio,
    )
    from notifications.telegram_commands.position_display import (
        _position_metrics,
        build_price_fallbacks,
        load_trade_history_safe,
        position_symbol,
    )
    from price_fetcher import get_prices_batch

    cfg = get_bot_config()
    mode = trading_mode or cfg.trading_mode
    scope = resolve_ledger_scope(mode)
    from core.tenant_context import tenant_snapshot

    tid, tenant_scope, _owner = tenant_snapshot()
    scope = tenant_scope or scope

    history = load_trade_history_safe(tenant_id=tid, scope=scope)
    from core.simulated_trading import is_simulated_trading, uses_order_ledger_cash
    from data_manager import resolve_sim_cash_balance, resolve_sim_realized_pnl

    if uses_order_ledger_cash(cfg.raw):
        from notifications.telegram_commands.position_display import _refresh_positions_for_snapshot

        active = _refresh_positions_for_snapshot(fast=True, tenant_id=tid, scope=scope)
        balance = resolve_sim_cash_balance(
            scope=scope, config=cfg.raw, history=history, tenant_id=tid,
        )
        realized = resolve_sim_realized_pnl(scope=scope, config=cfg.raw, tenant_id=tid)
    else:
        balance = float(history.get("virtual_balance", 0) or 0)
        realized = float(history.get("realized_pnl", history.get("total_pnl", 0)) or 0)
        active = None

    if is_simulated_trading(cfg.raw):
        balance_label = "Sim USDT"
    elif uses_exchange_ledger(mode):
        if balance <= 0:
            try:
                from services.gate_balance import fetch_usdt_balance

                balance = float(fetch_usdt_balance(cfg) or 0)
            except Exception:
                balance = 0.0
        balance_label = "USDT (Gate)"
    else:
        balance_label = "Balance"

    if active is None:
        active = list_active_positions(tenant_id=tid, scope=scope)
        if not active and int(history.get("open_positions", 0) or 0) > 0:
            from strategies.positions import bootstrap_positions

            bootstrap_positions(scope, tenant_id=tid)
            active = list_active_positions(tenant_id=tid, scope=scope)
    symbols = [position_symbol(p) for p in active]
    prices = get_prices_batch(symbols, fallbacks=build_price_fallbacks(active)) if symbols else {}

    open_lots_mtm = 0.0
    positions_market_value = 0.0
    for pos in active:
        sym = position_symbol(pos)
        metrics = _position_metrics(pos, float(prices.get(sym, 0) or 0))
        open_lots_mtm += metrics["unreal"]
        positions_market_value += metrics["value_usdt"]

    baseline = initial_capital(
        scope=scope,
        config=cfg.raw,
        history=history,
        trading_mode=mode,
    )
    total_value = balance + positions_market_value
    from core.portfolio_baseline import portfolio_pnl_for_display

    pnl = portfolio_pnl_for_display(total_value, baseline, realized, open_lots_mtm)

    return {
        "history": history,
        "balance": balance,
        "balance_label": balance_label,
        "realized": pnl["trade_realized"],
        "trade_realized": pnl["trade_realized"],
        "realized_ledger": realized,
        "unrealized": pnl["unrealized"],
        "open_lots_mtm": open_lots_mtm,
        "positions_market_value": positions_market_value,
        "total_value": total_value,
        "initial_capital": baseline,
        "ledger_scope": scope,
        "total_pnl": pnl["total_pnl"],
        "pnl_pct": pnl["pnl_pct"],
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
    else:
        usdt = float(trade.get("usdt_received", 0) or trade.get("usdt_amount", 0) or 0)
    pnl = trade.get("pnl")
    pnl_part = f" · PnL <b>${float(pnl):+.1f}</b>" if pnl is not None else ""
    return f"  · {typ} <b>{sym_html}</b> · ${usdt:.0f}{pnl_part} · <i>{src}</i>"


def format_executed_cycle_line(result: dict) -> str:
    """One line for a cycle-executed trade (BUY/SELL) including notional + PnL."""
    from notifications.coin_links import format_ticker_html

    sym = (result.get("symbol") or "").replace("/USDT", "")
    sym_html = format_ticker_html(sym, symbol_suffix="")
    order_type = result.get("order_type") or result.get("normalized_action") or "?"
    usdt = result.get("usdt_amount")
    if usdt is None:
        usdt = result.get("usdt")
    parts = [f"• {sym_html} {order_type}"]
    try:
        if usdt is not None and float(usdt) > 0:
            parts.append(f"${float(usdt):.0f}")
    except (TypeError, ValueError):
        pass
    if result.get("pnl") is not None:
        try:
            parts.append(f"PnL <b>${float(result['pnl']):+.1f}</b>")
        except (TypeError, ValueError):
            pass
    return "  " + " · ".join(parts)


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
    orders, _ = ledger.list_orders(hours=hours, page=1, per_page=limit, trade_book_only=True)
    if not orders:
        return [f"  <i>Keine Orders in den letzten {int(hours)}h ({ledger_label()}).</i>"]
    return [f"  {format_order_line(o)}" for o in orders]


def _cycle_summary_style() -> str:
    try:
        from core.config import get_bot_config

        style = (
            get_bot_config()
            .observability_config.get("cycle_notifications", {})
            .get("summary_style", "compact")
        )
        return str(style or "compact").strip().lower()
    except Exception:
        return "compact"


def _blocked_reject_tally(coin_results: list | None) -> dict[str, int]:
    """Count non-executed cycle rejects by short reason prefix."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for r in coin_results or []:
        if r.get("executed") or not r.get("trade_message"):
            continue
        msg = str(r.get("trade_message") or "")
        low = msg.lower()
        if "cash floor" in low or "cash_floor" in low:
            key = "cash_floor"
        elif "max open" in low:
            key = "max_open"
        elif "below minimum" in low or "size" in low:
            key = "size"
        else:
            key = "other"
        counts[key] += 1
    return dict(counts)


def build_cycle_summary(
    coin_results: list = None,
    trading_mode: str = "paper",
    x_signal_count: int = 0,
    cmc_signal_count: int = 0,
    lc_signal_count: int = 0,
    top_x: str = "",
    top_cmc: str = "",
    top_lc: str = "",
    *,
    style: str | None = None,
) -> str:
    snap = _portfolio_snapshot(trading_mode)
    balance = snap["balance"]
    balance_label = snap["balance_label"]
    realized = snap["realized"]
    total_value = snap["total_value"]
    nav_pnl = float(snap.get("total_pnl", float(realized or 0) + float(snap.get("unrealized", 0) or 0)))
    pnl_pct = float(snap.get("pnl_pct", 0.0))
    scope = snap.get("ledger_scope", resolve_ledger_scope(trading_mode))

    executed_trades = [r for r in (coin_results or []) if r.get("executed")]
    actions = [r for r in (coin_results or []) if r.get("normalized_action") != "HOLD"]
    style_s = (style or _cycle_summary_style()).strip().lower()
    if style_s not in ("compact", "full"):
        style_s = "compact"

    ledger = OrderService()
    day_stats = ledger.stats_executed_24h()
    attempts = ledger.stats_24h()

    open_n = int(snap.get("open_positions") or snap.get("position_count") or 0)
    if not open_n:
        try:
            from strategies.positions import count_open_positions

            open_n = int(count_open_positions() or 0)
        except Exception:
            open_n = 0
    try:
        from core.config import get_bot_config

        max_open = int(get_bot_config().max_open_positions or 0)
    except Exception:
        max_open = 0

    from notifications.telegram_i18n import money, signed_money, t

    if style_s == "compact":
        reason = ""
        try:
            from services.cycle_notification_policy import cycle_notification_policy

            reason = cycle_notification_policy.last_summary_reason or ""
        except Exception:
            pass
        if reason == "heartbeat":
            title = t("cycle_heartbeat")
        elif reason and "trade" in reason:
            title = t("cycle_trade")
        else:
            title = t("cycle_title")

        lines = [
            f"<b>{title}</b> — {datetime.now().strftime('%H:%M')} · "
            f"<b>{scope.upper()}</b>",
            t(
                "cycle_nav_line",
                cash_label=balance_label,
                cash=money(float(balance or 0)),
                nav=money(float(total_value or 0)),
                pnl=signed_money(nav_pnl),
                pct=f"{pnl_pct:+.1f}%",
            ),
        ]
        if max_open > 0:
            lines.append(
                t(
                    "cycle_slots_realized",
                    open=open_n,
                    max=max_open,
                    realized=signed_money(float(realized or 0)),
                )
            )
        else:
            lines.append(
                t("cycle_realized_only", realized=signed_money(float(realized or 0)))
            )

        try:
            from services.market_context_observability import format_fusion_line

            lines.append(format_fusion_line())
        except Exception:
            pass

        if attempts.get("rejected"):
            lines.append(
                t(
                    "cycle_orders_24h_blocked",
                    buys=day_stats["buys"],
                    sells=day_stats["sells"],
                    rejected=attempts["rejected"],
                )
            )
        else:
            lines.append(
                t(
                    "cycle_orders_24h",
                    buys=day_stats["buys"],
                    sells=day_stats["sells"],
                )
            )

        if executed_trades:
            lines.append(t("cycle_executed", count=len(executed_trades)))
            for r in executed_trades[:4]:
                lines.append(format_executed_cycle_line(r))
        else:
            tally = _blocked_reject_tally(coin_results)
            if tally:
                parts = [f"{k}×{n}" for k, n in sorted(tally.items(), key=lambda x: -x[1])]
                lines.append(t("cycle_blocked", tally=", ".join(parts)))

        lines.append(t("cycle_footer_compact"))
        return "\n".join(lines)

    # --- full (legacy / debug) ---
    lines = [
        f"<b>{t('cycle_title_full')}</b> — {datetime.now().strftime('%H:%M:%S')}",
        t("cycle_mode_ledger", mode=trading_mode.upper(), scope=scope.upper()),
        f"{balance_label}: ${float(balance or 0):,.0f} | "
        f"Gesamtwert: ${float(total_value or 0):,.0f} | "
        f"PnL: ${nav_pnl:+,.0f} ({pnl_pct:+.1f}%, Trades ${float(realized or 0):,.1f})",
        t(
            "cycle_signals",
            actions=len(actions),
            x=x_signal_count,
            cmc=cmc_signal_count,
            lc=lc_signal_count,
        ),
    ]
    try:
        from notifications.daily_portfolio import format_daily_nav_line

        daily = format_daily_nav_line(trading_mode, total_value=float(total_value or 0))
        if daily:
            lines.append(daily)
    except Exception:
        pass
    if top_x or top_cmc or top_lc:
        lines.append(t("cycle_social"))
        if top_x:
            lines.append(f"  🐦 {top_x}")
        if top_cmc:
            lines.append(f"  📊 {top_cmc}")
        if top_lc:
            lines.append(f"  🌙 {top_lc}")
    if actions:
        lines.append(t("cycle_decisions"))
        from notifications.coin_links import format_ticker_html

        for r in actions[:6]:
            sym = (r.get("symbol") or "").replace("/USDT", "")
            sym_html = format_ticker_html(sym, symbol_suffix="")
            act = r.get("normalized_action") or r.get("action")
            why = (r.get("why_de") or r.get("rationale") or "")[:80]
            status = "✅" if r.get("executed") else "🚫" if r.get("trade_message") else "👀"
            lines.append(f"  {status} {sym_html} {act}: {why}")
    if executed_trades:
        lines.append(t("cycle_executed_full", count=len(executed_trades)))
        for r in executed_trades[:5]:
            lines.append(format_executed_cycle_line(r))
    else:
        lines.append(t("cycle_no_auto_trades"))

    lines.append("")
    lines.append(
        t(
            "cycle_orders_24h_full",
            ledger=ledger_label(),
            buys=day_stats["buys"],
            sells=day_stats["sells"],
        )
    )
    blocked = attempts["rejected"] + attempts["cancelled"] + attempts["pending_confirmation"]
    if blocked:
        lines.append(
            t(
                "cycle_not_executed",
                rejected=attempts["rejected"],
                pending=attempts["pending_confirmation"],
                cancelled=attempts["cancelled"],
            )
        )
    lines.extend(recent_orders_lines())
    lines.append(t("cycle_footer_full"))
    return "\n".join(lines)