import re

from core.config import get_bot_config
from core.portfolio_baseline import (
    initial_capital,
    portfolio_pnl_for_display,
    reconcile_display_nav,
)
from data_manager import (
    is_dry_run_enhanced,
    resolve_ledger_scope,
    uses_exchange_ledger,
    uses_simulated_live_portfolio,
)
from services.gate_balance import fetch_spot_holdings, fetch_usdt_balance, format_holdings_lines
from services.order_service import source_label


def position_symbol(p: dict) -> str:
    sym = p["symbol"]
    return sym if "/" in sym else f"{sym}/USDT"


def _pnl_emoji(value: float) -> str:
    if value > 0.05:
        return "🟢"
    if value < -0.05:
        return "🔴"
    return "🟡"


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%"


def sort_positions_by_value(active: list, prices: dict) -> list:
    """Return positions sorted by current USDT value (highest first).

    Display (/sell, /positions) and sell execution must use the same order.
    """
    enriched = []
    for p in active:
        sym = position_symbol(p)
        price = float(prices.get(sym, 0) or 0)
        m = _position_metrics(p, price)
        enriched.append((p, m["value_usdt"]))
    enriched.sort(key=lambda row: row[1], reverse=True)
    return [p for p, _ in enriched]


def resolve_position_by_display_index(active: list, prices: dict, index: int):
    """Map 0-based display index (from numbered /sell list) to a position dict."""
    sorted_active = sort_positions_by_value(active, prices)
    if 0 <= index < len(sorted_active):
        return sorted_active[index]
    return None


def normalize_position_symbol_query(raw: str) -> str:
    """Accept RAVE, rave/usdt, RAVE/USDT → canonical pair."""
    s = (raw or "").strip().upper()
    if not s:
        return ""
    if "/" in s:
        base, _, quote = s.partition("/")
        return f"{base}/{quote or 'USDT'}"
    return f"{s}/USDT"


def resolve_position_by_symbol(active: list, query: str, prices: dict | None = None):
    """Find open position by ticker or pair (e.g. RAVE or RAVE/USDT)."""
    sym = normalize_position_symbol_query(query)
    if not sym:
        return None
    matches = [p for p in active if position_symbol(p).upper() == sym]
    if not matches:
        ticker = sym.replace("/USDT", "")
        matches = [
            p for p in active
            if position_symbol(p).upper().replace("/USDT", "") == ticker
        ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if prices:
        sorted_matches = sort_positions_by_value(matches, prices)
        return sorted_matches[0]
    return matches[0]


def _entry_fallback_price(p: dict) -> float:
    for key in ("average_entry", "entry_price", "last_buy_price"):
        value = float(p.get(key, 0) or 0)
        if value > 0:
            return value
    return 0.0


def build_price_fallbacks(active: list) -> dict[str, float]:
    fallbacks = {}
    for p in active:
        sym = position_symbol(p)
        fb = _entry_fallback_price(p)
        if fb > 0:
            fallbacks[sym] = fb
    return fallbacks


def _price_source_note(source: str = None) -> str:
    labels = {
        "entry": " <i>(Entry-Schätzung)</i>",
        "stale": " <i>(letzter Kurs)</i>",
        "missing": "",
    }
    return labels.get(source or "", "")


def _effective_sold_fraction(p: dict) -> float:
    amount = float(p.get("amount", 0) or 0)
    peak = float(p.get("peak_amount", 0) or 0)
    if peak > 0 and amount >= 0:
        return min(1.0, max(0.0, 1.0 - amount / peak))
    return min(float(p.get("sold_percent", 0) or 0), 1.0)


def _position_cost_basis(p: dict) -> float:
    """USDT invested in this open lot at entry (entry × amount)."""
    entry = float(p.get("average_entry", p.get("entry_price", 0)) or 0)
    amount = float(p.get("amount", 0) or 0)
    if entry <= 0 or amount <= 0:
        return 0.0
    return entry * amount


def aggregate_open_coins_totals(active: list, prices: dict) -> dict:
    """Sum marktwert, einstand, and unrealized PnL across open positions."""
    marktwert = 0.0
    cost_basis = 0.0
    unreal = 0.0
    missing_prices = 0
    for p in active:
        sym = position_symbol(p)
        price = float(prices.get(sym, 0) or 0)
        m = _position_metrics(p, price)
        marktwert += m["value_usdt"]
        unreal += m["unreal"]
        cost_basis += _position_cost_basis(p)
        if price <= 0:
            missing_prices += 1
    return {
        "marktwert": marktwert,
        "cost_basis": cost_basis,
        "unreal": unreal,
        "missing_prices": missing_prices,
    }


def _position_metrics(p: dict, price: float) -> dict:
    entry = float(p.get("average_entry", p.get("entry_price", 0)) or 0)
    amount = float(p.get("amount", 0))
    value_usdt = price * amount if price > 0 else 0.0
    unreal = (price - entry) * amount if entry > 0 and price > 0 else 0.0
    unreal_pct = ((price / entry) - 1) * 100 if entry > 0 and price > 0 else 0.0
    sold_raw = _effective_sold_fraction(p)
    sold_pct = sold_raw * 100
    return {
        "entry": entry,
        "amount": amount,
        "price": price,
        "value_usdt": value_usdt,
        "unreal": unreal,
        "unreal_pct": unreal_pct,
        "sold_pct": sold_pct,
        "sold_warn": float(p.get("sold_percent", 0) or 0) > 1.0,
    }


def _position_amount_label(amount: float) -> str:
    from price_fetcher import format_token_amount

    return format_token_amount(amount)


def _positions_display_config() -> tuple[bool, int]:
    cfg = get_bot_config().observability_config
    show_tree = bool(cfg.get("positions_show_trade_tree", True))
    max_events = int(cfg.get("positions_max_events_per_coin", 6) or 6)
    return show_tree, max(1, max_events)


_TELEGRAM_CHUNK_LIMIT = 3900
_POSITION_CARD_SPLIT = re.compile(r"\n\n(?=<b>\d+\.</b>)")
_COMPACT_LINE_SPLIT = re.compile(r"\n(?=<b>\d+\.</b>)")


def _hard_split_telegram(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks or [""]


def chunk_positions_message(
    msg: str,
    limit: int = _TELEGRAM_CHUNK_LIMIT,
    *,
    annotate_pages: bool = True,
) -> list[str]:
    """Split /positions HTML at position-card boundaries for Telegram's 4096 limit."""
    body = (msg or "").strip()
    if len(body) <= limit:
        return [body]

    split_re = _COMPACT_LINE_SPLIT if not annotate_pages else _POSITION_CARD_SPLIT
    parts = split_re.split(body)
    if len(parts) <= 1:
        return _hard_split_telegram(body, limit)

    header, *cards = parts
    chunks: list[str] = []
    current = header.strip()
    continued = False
    line_sep = "\n" if not annotate_pages else "\n\n"

    for card in cards:
        card = card.strip()
        if not card:
            continue
        sep = line_sep if current else ""
        candidate = f"{current}{sep}{card}" if current else card
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        prefix = "<i>📊 Positionen (Fortsetzung)</i>\n\n" if continued or chunks else ""
        continued = True
        candidate = f"{prefix}{card}"
        if len(candidate) > limit:
            for piece in _hard_split_telegram(candidate, limit):
                chunks.append(piece)
            current = ""
        else:
            current = candidate

    if current:
        chunks.append(current)

    if len(chunks) <= 1 or not annotate_pages:
        return chunks

    total = len(chunks)
    tagged: list[str] = []
    for i, chunk in enumerate(chunks):
        tag = f"\n\n<i>({i + 1}/{total})</i>"
        room = limit - len(tag)
        tagged.append((chunk[:room] if len(chunk) > room else chunk) + tag)
    return tagged


def format_position_card(
    index: int,
    p: dict,
    price: float,
    numbered: bool = False,
    *,
    price_source: str = None,
    show_trade_tree: bool = False,
    position_orders: list | None = None,
    max_events: int = 6,
) -> str:
    from price_fetcher import format_usdt_price

    sym = position_symbol(p)
    m = _position_metrics(p, price)
    prefix = f"<b>{index}.</b> " if numbered else ""
    ticker = sym.split("/")[0]
    from notifications.coin_links import format_ticker_html

    ticker_html = format_ticker_html(ticker, symbol_suffix="")
    pnl_icon = _pnl_emoji(m["unreal"])

    value_part = ""
    if m["value_usdt"] > 0:
        value_part = f" · Wert <b>${m['value_usdt']:,.0f}</b>"
    header = (
        f"{prefix}<b>{ticker_html}</b> {pnl_icon} "
        f"<code>{_fmt_pct(m['unreal_pct'])}</code>{value_part}"
    )

    if show_trade_tree:
        from notifications.telegram_commands.position_ledger import build_position_trade_tree

        tree_lines = build_position_trade_tree(
            p,
            mark_price=m["price"] if price_source != "missing" else 0.0,
            orders=position_orders or [],
            max_events=max_events,
        )
        if price_source == "missing" and m["value_usdt"] <= 0:
            tree_lines.append("   └─ <i>⚠️ Kein Live-Kurs — Wert nicht in Gesamtwert</i>")
        return header + "\n" + "\n".join(tree_lines)

    sold_line = ""
    if m["sold_pct"] > 0 or m["sold_warn"]:
        sold_raw_pct = float(p.get("sold_percent", 0) or 0) * 100
        sold_val = f"{m['sold_pct']:.0f}%" if not m["sold_warn"] else f"⚠️ {sold_raw_pct:.0f}%"
        sold_line = f"\n   └ Bereits verkauft: <b>{sold_val}</b>"

    last = p.get("last_action")
    last_line = f" · Letzte Aktion: <b>{last}</b>" if last else ""

    price_str = format_usdt_price(m["price"])
    if price_source == "missing":
        price_str = "—"
    entry_str = format_usdt_price(m["entry"])
    source_note = _price_source_note(price_source)

    missing_line = ""
    if price_source == "missing" and m["value_usdt"] <= 0:
        missing_line = "\n   └ <i>⚠️ Kein Live-Kurs — Wert nicht in Gesamtwert</i>"

    return (
        f"{header}\n"
        f"   └ <code>{_position_amount_label(m['amount'])}</code> @ {price_str}{source_note} · Entry {entry_str}\n"
        f"   └ Wert <b>${m['value_usdt']:.1f}</b> · PnL <b>${m['unreal']:+.1f}</b>"
        f"{sold_line}{last_line}{missing_line}"
    )


def format_position_compact_line(
    index: int,
    p: dict,
    price: float,
    *,
    price_source: str = None,
) -> str:
    from notifications.coin_links import format_ticker_html

    sym = position_symbol(p)
    ticker_html = format_ticker_html(sym.split("/")[0], symbol_suffix="")
    m = _position_metrics(p, price)
    icon = _pnl_emoji(m["unreal"])
    missing = " · <i>kein Kurs</i>" if price_source == "missing" and m["value_usdt"] <= 0 else ""
    tf = p.get("timeframe", "4h")
    return (
        f"<b>{index}.</b> {ticker_html} <i>{tf}</i> {icon} <code>{_fmt_pct(m['unreal_pct'])}</code> "
        f"· <b>${m['value_usdt']:.0f}</b> · PnL <b>${m['unreal']:+.0f}</b>{missing}"
    )


def _lookup_order_timeframe(order_id: str) -> str | None:
    if not order_id:
        return None
    from data_manager import load_orders, resolve_ledger_scope

    for order in load_orders(resolve_ledger_scope()).get("orders", []):
        if order.get("id") == order_id:
            return str(order.get("timeframe") or "4h")
    return None


def _lookup_order_source(order_id: str) -> str:
    if not order_id:
        return "manual"
    from data_manager import load_orders, resolve_ledger_scope

    for order in load_orders(resolve_ledger_scope()).get("orders", []):
        if order.get("id") == order_id:
            return source_label(order.get("source", "manual"))
    return "manual"


def format_sell_trade_detail(result) -> str:
    from price_fetcher import format_token_amount, format_usdt_price
    from strategies.positions import get_position

    sym = result.symbol or ""
    tf = _lookup_order_timeframe(getattr(result, "order_id", "")) or "4h"
    amount = float(result.amount or 0)
    price = float(result.price or 0)
    usdt = float(result.usdt_amount or 0)
    pnl = float(result.pnl or 0) if result.pnl is not None else 0.0
    ticker = sym.replace("/USDT", "")
    from notifications.coin_links import format_ticker_html

    ticker_html = format_ticker_html(ticker, symbol_suffix="")
    entry = (price - (pnl / amount)) if amount > 0 and pnl else 0.0
    trade_pct = ((price / entry) - 1) * 100 if entry > 0 and price > 0 else 0.0

    pos = get_position(sym, tf)
    remaining = float(pos.get("amount", 0) or 0)
    sold_pct = float(pos.get("sold_percent", 0) or 0) * 100
    src = _lookup_order_source(getattr(result, "order_id", ""))

    lines = [
        f"<b>Verkauf — {ticker_html}</b>",
        (
            f"   └ <code>{format_token_amount(amount)}</code> @ "
            f"{format_usdt_price(price)} · <b>${usdt:.0f}</b>"
        ),
    ]
    if entry > 0:
        lines.append(
            f"   └ Entry {format_usdt_price(entry)} · Trade-PnL "
            f"<b>${pnl:+.1f}</b> (<code>{trade_pct:+.1f}%</code>)"
        )
    elif pnl:
        lines.append(f"   └ Trade-PnL <b>${pnl:+.1f}</b>")

    remain_part = f"<code>{format_token_amount(remaining)}</code> {ticker}" if remaining > 0 else "<i>geschlossen</i>"
    sold_note = f" · {sold_pct:.0f}% verkauft" if sold_pct > 0 else ""
    lines.append(f"   └ Verbleibend {remain_part}{sold_note} · {tf} · <i>{src}</i>")
    return "\n".join(lines)


def _trade_quantity_label(t: dict) -> str:
    from price_fetcher import format_token_amount

    amount = float(t.get("amount", 0) or 0)
    if t.get("type") == "BUY" and amount <= 0:
        usdt = float(t.get("usdt_amount", 0) or 0)
        if usdt > 0:
            return f"<b>${usdt:.0f}</b>"
    return f"<code>{format_token_amount(amount)}</code>"


def _trade_side_label(trade: dict) -> str:
    """BUY always green; SELL green/red/yellow by realized PnL."""
    from notifications.telegram_i18n import t as _t

    side = (trade.get("type") or trade.get("side") or "").upper()
    if side == "BUY":
        return _t("trade_buy")
    pnl_raw = trade.get("pnl")
    try:
        pnl = float(pnl_raw) if pnl_raw is not None else None
    except (TypeError, ValueError):
        pnl = None
    if pnl is None:
        icon = "⚪"
    else:
        icon = _pnl_emoji(pnl)
    return _t("trade_sell_icon", icon=icon)


def _trade_line(t: dict) -> str:
    from price_fetcher import format_usdt_price
    from notifications.coin_links import format_ticker_html

    ts = t.get("timestamp", "")[:16].replace("T", " ")
    typ = _trade_side_label(t)
    sym = (t.get("symbol") or "").replace("/USDT", "")
    sym_html = format_ticker_html(sym, symbol_suffix="")
    pnl = t.get("pnl")
    try:
        pnl_f = float(pnl) if pnl is not None else None
    except (TypeError, ValueError):
        pnl_f = None
    pnl_part = f" · PnL <b>${pnl_f:+.1f}</b>" if pnl_f is not None else ""
    src = source_label(t.get("source", "auto"))
    return (
        f"\n{typ} <b>{sym_html}</b> · <i>{src}</i> · {ts}\n"
        f"   └ {_trade_quantity_label(t)} @ "
        f"{format_usdt_price(float(t.get('price', 0)))}{pnl_part}"
    )


def format_portfolio_summary(
    history: dict,
    total_unreal: float,
    position_count: int,
    mode_label: str = "",
    *,
    cash_balance: float = None,
    cash_label: str = "Cash",
    positions_market_value: float = 0.0,
    prices: dict = None,
    fast_daily_nav: bool = False,
    include_position_header: bool = True,
    trade_realized: float = None,
    positions_cost_basis: float = None,
) -> str:
    balance = float(cash_balance if cash_balance is not None else history.get("virtual_balance", 0))
    cfg = get_bot_config()
    initial = initial_capital(
        scope=resolve_ledger_scope(cfg.trading_mode),
        config=cfg.raw,
        history=history,
        trading_mode=cfg.trading_mode,
    )
    if trade_realized is None:
        trade_realized = float(history.get("realized_pnl", history.get("total_pnl", 0)) or 0)
    open_mtm = float(total_unreal or 0)
    pos_mv = float(positions_market_value or 0)
    cost_basis = (
        float(positions_cost_basis)
        if positions_cost_basis is not None
        else pos_mv - open_mtm
    )
    nav_fix = reconcile_display_nav(
        balance,
        initial,
        pos_mv,
        trade_realized,
        open_mtm,
        positions_cost_basis=cost_basis,
    )
    balance = nav_fix["cash_balance"]
    total_value = nav_fix["total_value"]
    pnl = portfolio_pnl_for_display(total_value, initial, trade_realized, open_mtm)
    total_pnl = pnl["total_pnl"]
    open_lots_mtm = pnl["open_lots_mtm"]
    pnl_pct = pnl["pnl_pct"]
    from notifications.telegram_i18n import money, signed_money, t

    wert_icon = _pnl_emoji(total_pnl)
    trade_icon = _pnl_emoji(trade_realized)
    lots_icon = _pnl_emoji(open_lots_mtm)
    mode_line = f" · <i>{mode_label}</i>" if mode_label else ""
    daily_line = ""
    try:
        from notifications.daily_portfolio import format_daily_nav_line

        daily_line = format_daily_nav_line(
            total_value=total_value,
            prices=prices,
            cache_ttl_sec=180.0 if fast_daily_nav else 120.0,
            lightweight=bool(fast_daily_nav),
        )
        if daily_line:
            daily_line = f"{daily_line}\n"
    except Exception:
        pass

    wert_detail = t(
        "portfolio_growth_detail",
        total=money(total_value),
        initial=money(initial),
    ) + "\n"
    trade_detail = ""
    if trade_realized or position_count > 0:
        trade_detail = t(
            "portfolio_realized",
            icon=trade_icon,
            realized=signed_money(trade_realized),
        ) + "\n"

    # Capacity / floor: "Kaufplätze" vs open coins (proposal B wording)
    floor_line = ""
    slots_line = ""
    full_n = int(position_count or 0)
    lots_n = int(position_count or 0)
    max_open = int(getattr(cfg, "max_open_positions", 0) or 0)
    setup = ""
    try:
        risk_cfg = cfg.risk_config if hasattr(cfg, "risk_config") else {}
        floor_pct = float((risk_cfg or {}).get("cash_floor_pct", 0) or 0)
        floor_abs = max(0.0, float(initial) * (floor_pct / 100.0)) if floor_pct > 0 else 0.0
        spendable = max(0.0, float(balance) - floor_abs) if floor_pct > 0 else max(0.0, float(balance))
        try:
            from risk.risk_manager import RiskManager

            st = RiskManager(cfg).status_summary()
            full_n = int(st.get("open_full_slots", full_n) or full_n)
            lots_n = int(st.get("open_positions", lots_n) or lots_n)
            if st.get("position_capacity_enabled"):
                max_open = int(
                    st.get("max_open_eff")
                    or st.get("max_open_positions")
                    or max_open
                )
                mode = str(st.get("cash_mode") or "").upper()
                regime = str(st.get("capacity_regime") or "").upper()
                bits = []
                if mode:
                    bits.append(mode)
                if regime and regime not in ("NEUTRAL", ""):
                    bits.append(regime)
                bits.append(f"eff={max_open}")
                factors = st.get("position_capacity_factors") or {}
                warm = factors.get("warmup") if isinstance(factors, dict) else None
                if warm:
                    bits.append(f"warmup{int(warm):+d}")
                setup = " · ".join(bits) if bits else f"eff={max_open}"
            else:
                max_open = int(
                    st.get("max_open_positions")
                    or getattr(cfg, "max_open_positions", 0)
                    or 0
                )
                setup = f"static={max_open}" if max_open else ""
            for sk in ("spendable_new", "spendable_usdt"):
                if st.get(sk) is not None:
                    try:
                        spendable = max(0.0, float(st.get(sk)))
                        break
                    except (TypeError, ValueError):
                        pass
            if st.get("cash_floor_abs") is not None:
                try:
                    floor_abs = max(0.0, float(st["cash_floor_abs"]))
                    floor_pct = floor_pct or 1.0  # show floor line when abs known
                except (TypeError, ValueError):
                    pass
        except Exception:
            setup = f"base={max_open}" if max_open else ""

        if floor_pct > 0 or floor_abs > 0:
            floor_line = (
                t(
                    "portfolio_cash_floor",
                    floor=money(floor_abs),
                    spendable=money(spendable),
                )
                + "\n"
            )
        if max_open > 0:
            free_n = max(0, int(max_open) - int(full_n))
            if free_n <= 0:
                slots_line = t(
                    "portfolio_buy_slots_full",
                    full=full_n,
                    max=max_open,
                )
            else:
                slots_line = t(
                    "portfolio_buy_slots_free",
                    free=free_n,
                    full=full_n,
                    max=max_open,
                )
            if setup:
                slots_line += f" · <i>{setup}</i>"
            slots_line += "\n"
    except Exception:
        pass

    tails_n = max(0, int(lots_n) - int(full_n))
    coins_line = ""
    if position_count > 0:
        coins_line = t("portfolio_open_coins", count=position_count) + "\n"
        if tails_n > 0:
            coins_line += t(
                "portfolio_open_breakdown",
                full=full_n,
                tails=tails_n,
            ) + "\n"
        if pos_mv > 0:
            coins_line += (
                t(
                    "portfolio_cost_mark",
                    cost=money(cost_basis),
                    mark=money(pos_mv),
                )
                + "\n"
                + t(
                    "portfolio_delta_entry",
                    icon=lots_icon,
                    delta=signed_money(open_lots_mtm),
                )
                + "\n"
            )
        else:
            coins_line += t("portfolio_cost_mark_na", cost=money(cost_basis)) + "\n"

    body = (
        f"<b>{t('portfolio_title')}</b>{mode_line}\n\n"
        + t("portfolio_cash", label=cash_label, balance=f"{balance:,.2f}")
        + "\n"
        + floor_line
        + slots_line
        + coins_line
        + t("portfolio_nav", total=money(total_value))
        + "\n"
        + t(
            "portfolio_growth",
            icon=wert_icon,
            pnl=signed_money(total_pnl, decimals=1),
            pct=f"{pnl_pct:+.1f}%",
            initial=money(initial),
        )
        + "\n"
        + wert_detail
        + trade_detail
        + daily_line
    )
    if include_position_header and position_count > 0:
        if pos_mv > 0:
            body += t(
                "portfolio_positions_header",
                count=position_count,
                mark=money(pos_mv),
            )
        else:
            body += t("portfolio_positions_header_na", count=position_count)
    elif include_position_header:
        body += t("portfolio_positions_empty_header")
    return body.rstrip() + ("\n" if body else "")


def format_positions_message(
    active: list,
    prices: dict,
    history: dict,
    mode_label: str = "",
    include_trades: bool = True,
    numbered: bool = True,
    title: str = None,
    *,
    cash_balance: float = None,
    cash_label: str = "Cash",
    trade_realized: float = None,
    gate_holdings: list = None,
    price_sources: dict = None,
    fast_daily_nav: bool = False,
    detail_level: str = "full",
) -> str:
    level = (detail_level or "full").strip().lower()

    if not active:
        if level == "summary" and not title:
            return format_portfolio_summary(
                history,
                0.0,
                0,
                mode_label,
                cash_balance=cash_balance,
                cash_label=cash_label,
                positions_market_value=0.0,
                prices=prices,
                fast_daily_nav=fast_daily_nav,
                include_position_header=False,
                trade_realized=trade_realized,
            )
        from notifications.telegram_i18n import t

        cash = float(cash_balance if cash_balance is not None else history.get("virtual_balance", 0))
        empty = (
            f"<b>{t('portfolio_title')}</b>\n\n"
            f"{t('portfolio_no_positions')}\n"
            + t("portfolio_cash", label=cash_label, balance=f"{cash:,.2f}")
        )
        if mode_label:
            empty += f"\n<i>{mode_label}</i>"
        if gate_holdings:
            empty += "\n\n<b>Gate Spot-Bestände</b>\n"
            empty += "\n".join(format_holdings_lines(gate_holdings, {}))
        if include_trades:
            empty += f"\n\n{t('portfolio_last_trades')}\n"
            trades = history.get("trades", [])[-5:]
            if not trades:
                empty += t("portfolio_no_trades")
            else:
                for t in reversed(trades):
                    empty += _trade_line(t)
        return empty

    coin_totals = aggregate_open_coins_totals(active, prices)
    total_unreal = coin_totals["unreal"]
    positions_market_value = coin_totals["marktwert"]
    positions_cost_basis = coin_totals["cost_basis"]
    sorted_active = sort_positions_by_value(active, prices)
    rows = [
        (p, float(prices.get(position_symbol(p), 0) or 0))
        for p in sorted_active
    ]

    if title:
        msg = f"<b>{title}</b>\n\n"
    else:
        msg = format_portfolio_summary(
            history, total_unreal, len(active), mode_label,
            cash_balance=cash_balance, cash_label=cash_label,
            positions_market_value=positions_market_value,
            positions_cost_basis=positions_cost_basis,
            prices=prices,
            fast_daily_nav=fast_daily_nav,
            include_position_header=level != "summary",
            trade_realized=trade_realized,
        ) + "\n"

    if gate_holdings:
        msg += "\n\n<b>Gate Spot-Bestände</b>\n"
        msg += "\n".join(format_holdings_lines(gate_holdings, prices))

    if level == "summary":
        return msg.rstrip()

    sources = price_sources or {}
    if level == "compact":
        compact_lines = [
            format_position_compact_line(
                i, p, price, price_source=sources.get(position_symbol(p)),
            )
            for i, (p, price) in enumerate(rows, 1)
        ]
        if compact_lines:
            msg += "\n".join(compact_lines)
        return msg.rstrip()

    show_tree, max_events = _positions_display_config()
    orders_grouped = {}
    if show_tree:
        from data_manager import resolve_ledger_scope
        from notifications.telegram_commands.position_ledger import orders_by_position_key

        orders_grouped = orders_by_position_key(resolve_ledger_scope())

    cards = []
    for i, (p, price) in enumerate(rows, 1):
        sym = position_symbol(p)
        tf = p.get("timeframe", "4h")
        order_key = f"{sym}|{tf}"
        cards.append(format_position_card(
            i,
            p,
            price,
            numbered=numbered,
            price_source=sources.get(sym),
            show_trade_tree=show_tree,
            position_orders=orders_grouped.get(order_key, []),
            max_events=max_events,
        ))
    msg += "\n\n".join(cards)

    if include_trades and not show_tree:
        msg += "\n\n<b>Letzte Trades</b>\n"
        trades = history.get("trades", [])[-5:]
        if not trades:
            msg += "<i>Keine Trades im Ledger.</i>"
        else:
            for t in reversed(trades):
                msg += _trade_line(t)

    return msg


def format_sell_list_message(active: list, prices: dict) -> str:
    msg = format_positions_message(
        active,
        prices,
        load_trade_history_safe(),
        include_trades=False,
        numbered=True,
        title="📍 Positionen verkaufen",
    )
    from notifications.telegram_commands.menu_i18n import context_footer, current_language

    return msg + "\n\n" + context_footer("sell", current_language(), example="RAVE 30")


def load_trade_history_safe(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
) -> dict:
    from core.simulated_trading import is_simulated_trading, simulated_ledger_scope
    from data_manager import load_trade_history_document

    cfg = get_bot_config().raw
    scope = scope or simulated_ledger_scope(get_bot_config().trading_mode, cfg)
    if is_simulated_trading(cfg):
        return load_trade_history_document(scope, cfg, tenant_id=tenant_id)
    if uses_exchange_ledger(get_bot_config().trading_mode):
        from data_manager import load_live_trade_history

        return load_live_trade_history()
    from data_manager import load_trade_history

    return load_trade_history()


def _portfolio_tenant_ids(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
) -> tuple[str, str]:
    from core.tenant_context import resolve_tenant_id, resolve_tenant_scope

    return resolve_tenant_id(tenant_id), resolve_tenant_scope(scope)


def _active_lots_from_merged(merged: dict) -> list[dict]:
    from strategies.positions import _active_lot_from_store_key, is_open_position

    active: list[dict] = []
    for pos_key, raw in merged.items():
        if not is_open_position(raw):
            continue
        lot = _active_lot_from_store_key(pos_key, raw)
        if lot["symbol"].upper().startswith("TEST"):
            continue
        active.append(lot)
    return active


def _sim_order_ledger_bundle(
    *,
    tenant_id: str,
    scope: str,
    history: dict,
    cfg,
    prefer_memory: bool = True,
) -> dict:
    """One Mongo orders load + one replay → cash, realized, open lots."""
    from core.sim_ledger_replay import replay_simulated_ledger
    from core.simulated_trading import simulated_ledger_scope
    from data_manager import load_orders, load_positions_document
    from services.ledger_sync import _reconcile_ladder_steps_in_snapshot
    from strategies.positions import (
        derive_positions_from_orders_and_cache,
        list_active_positions,
    )

    ledger_scope = simulated_ledger_scope(cfg.trading_mode, cfg.raw)
    filled = [
        o
        for o in load_orders(ledger_scope, tenant_id=tenant_id).get("orders", [])
        if o.get("status") == "filled"
    ]
    initial = initial_capital(
        scope=ledger_scope,
        config=cfg.raw,
        history=history,
        trading_mode=cfg.trading_mode,
    )
    replayed = replay_simulated_ledger(filled, initial)
    cash = float(replayed["cash"])
    trade_realized = float(replayed["realized_pnl"])

    active: list[dict] = []
    if prefer_memory:
        active = list_active_positions(tenant_id=tenant_id, scope=scope)
    if not active:
        order_snap = dict(replayed["positions"])
        _reconcile_ladder_steps_in_snapshot(order_snap)
        cache_doc = load_positions_document(ledger_scope, tenant_id=tenant_id)
        merged = derive_positions_from_orders_and_cache(
            order_snap, cache_doc, tenant_id=tenant_id
        )
        active = _active_lots_from_merged(merged)

    return {
        "history": history,
        "cash_balance": cash,
        "trade_realized": trade_realized,
        "active": active,
        "gate_holdings": None,
        "filled_orders": filled,
    }


def _refresh_positions_for_snapshot(
    *,
    fast: bool = False,
    tenant_id: str | None = None,
    scope: str | None = None,
) -> list[dict]:
    """Open lots for portfolio display.

    fast=True: prefer warm in-memory tenant store (no full order replay).
    Falls back to ledger rebuild when memory is empty or fast=False.
    """
    from strategies.positions import (
        list_active_positions,
        list_active_positions_from_ledger,
    )

    tid, sc = _portfolio_tenant_ids(tenant_id=tenant_id, scope=scope)
    if fast:
        active = list_active_positions(tenant_id=tid, scope=sc)
        if active:
            return active
    return list_active_positions_from_ledger(scope=sc, tenant_id=tid)


def resolve_portfolio_context(
    *,
    fast: bool = False,
    tenant_id: str | None = None,
    scope: str | None = None,
) -> dict:
    from core.simulated_trading import is_simulated_trading, simulated_ledger_scope, uses_order_ledger_cash
    from data_manager import resolve_sim_cash_balance

    cfg = get_bot_config()
    tid, sc = _portfolio_tenant_ids(tenant_id=tenant_id, scope=scope)
    history = load_trade_history_safe(tenant_id=tid, scope=sc)
    if uses_simulated_live_portfolio(cfg.raw):
        if is_dry_run_enhanced(cfg.raw):
            cash_label = "Sim USDT"
        elif is_simulated_trading(cfg.raw):
            cash_label = "Sim USDT"
        else:
            cash_label = "Cash (Dry Run)"

        if uses_order_ledger_cash(cfg.raw):
            bundle = _sim_order_ledger_bundle(
                tenant_id=tid,
                scope=sc,
                history=history,
                cfg=cfg,
                prefer_memory=bool(fast),
            )
            return {
                "history": history,
                "cash_balance": bundle["cash_balance"],
                "cash_label": cash_label,
                "trade_realized": bundle["trade_realized"],
                "gate_holdings": None,
                "active": bundle["active"],
            }

        ledger_scope = simulated_ledger_scope(cfg.trading_mode, cfg.raw)
        cash = resolve_sim_cash_balance(
            scope=ledger_scope,
            config=cfg.raw,
            history=history,
            tenant_id=tid,
        )
        trade_realized = float(
            history.get("realized_pnl", history.get("total_pnl", 0)) or 0
        )
        return {
            "history": history,
            "cash_balance": cash,
            "cash_label": cash_label,
            "trade_realized": trade_realized,
            "gate_holdings": None,
        }
    if uses_exchange_ledger(cfg.trading_mode):
        from services.gate_balance import fetch_balance_bundle

        bundle = fetch_balance_bundle(cfg, max_age_sec=25.0 if fast else 20.0)
        return {
            "history": history,
            "cash_balance": float(bundle.get("usdt", 0)),
            "cash_label": "Gate USDT",
            "trade_realized": float(history.get("realized_pnl", history.get("total_pnl", 0)) or 0),
            "gate_holdings": bundle.get("holdings") or [],
        }
    return {
        "history": history,
        "cash_balance": float(history.get("virtual_balance", 0)),
        "cash_label": "Cash",
        "trade_realized": float(history.get("realized_pnl", history.get("total_pnl", 0)) or 0),
        "gate_holdings": None,
    }


def format_trade_banner(result) -> str:
    from price_fetcher import format_token_amount, format_usdt_price

    sym = (result.symbol or "").replace("/USDT", "")
    price = float(result.price or 0)
    amount = float(result.amount or 0)
    usdt = float(result.usdt_amount or 0)
    price_str = format_usdt_price(price)
    amount_str = format_token_amount(amount)
    from notifications.telegram_i18n import t

    if result.order_type == "BUY":
        return (
            f"{t('buy_done', sym=sym)}\n"
            f"   └ <code>{amount_str}</code> @ {price_str} · <b>${usdt:.0f}</b>"
        )
    pnl_part = (
        t("sell_done_pnl", pnl=f"{float(result.pnl):+.1f}")
        if result.pnl is not None
        else ""
    )
    return (
        f"{t('sell_done', sym=sym)}\n"
        f"   └ <code>{amount_str}</code> @ {price_str} · <b>${usdt:.0f}</b>{pnl_part}"
    )


def _resolve_snapshot_detail_level(trade_result, detail_level: str | None) -> str:
    if detail_level:
        return detail_level
    if trade_result is not None and getattr(trade_result, "executed", False):
        return "summary"
    return "compact"


def _portfolio_tenant_header(tenant_id: str) -> str:
    from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled

    tid = (tenant_id or "").strip()
    if multi_tenant_enabled() and tid and tid != DEFAULT_TENANT:
        return f"<i>Tenant: {tid}</i>\n"
    return ""


def send_positions_snapshot(
    trade_result=None,
    mode_label: str = None,
    *,
    fast: bool = True,
    chat_id: str | int | None = None,
    detail_level: str | None = None,
    tenant_id: str | None = None,
    scope: str | None = None,
) -> bool:
    """Send portfolio overview to Telegram; optional trade banner after buy/sell."""
    from price_fetcher import get_prices_batch
    from services.trading_service import TradingService
    from telegram_notifier import send_telegram_message

    tid, sc = _portfolio_tenant_ids(tenant_id=tenant_id, scope=scope)
    ctx = resolve_portfolio_context(fast=fast, tenant_id=tid, scope=sc)
    # Prefer lots already derived in the single order-ledger pass (no 2nd Mongo load).
    active = ctx.get("active")
    if active is None:
        active = _refresh_positions_for_snapshot(fast=fast, tenant_id=tid, scope=sc)

    symbols = [position_symbol(p) for p in active]
    if ctx.get("gate_holdings"):
        symbols.extend(h["symbol"] for h in ctx["gate_holdings"])
    unique_symbols = list(dict.fromkeys(symbols))
    fallbacks = build_price_fallbacks(active)
    if unique_symbols:
        prices, price_sources = get_prices_batch(
            unique_symbols, fallbacks=fallbacks, return_sources=True,
        )
    else:
        prices, price_sources = {}, {}
    mode = mode_label or TradingService().mode_label()
    level = _resolve_snapshot_detail_level(trade_result, detail_level)
    msg = format_positions_message(
        active,
        prices,
        ctx["history"],
        mode_label=mode,
        include_trades=(level == "full"),
        cash_balance=ctx["cash_balance"],
        cash_label=ctx["cash_label"],
        trade_realized=ctx.get("trade_realized"),
        gate_holdings=ctx.get("gate_holdings"),
        price_sources=price_sources,
        fast_daily_nav=fast,
        detail_level=level,
    )
    if trade_result is not None and getattr(trade_result, "executed", False):
        msg = f"{format_trade_banner(trade_result)}\n\n{msg}"
        if trade_result.order_type == "SELL":
            msg = f"{msg}\n\n{format_sell_trade_detail(trade_result)}"

    header = _portfolio_tenant_header(tid)
    if header:
        msg = f"{header}{msg}"

    chunks = chunk_positions_message(msg, annotate_pages=(level == "full"))
    ok = True
    for chunk in chunks:
        if not send_telegram_message(chunk, chat_id=chat_id):
            ok = False
    return ok