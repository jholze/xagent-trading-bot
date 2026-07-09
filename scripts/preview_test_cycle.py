#!/usr/bin/env python3
"""Preview what the next bot cycle would do (read-only, live prices + OHLCV)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
os.environ.setdefault("MONGODB_DB", "xagent_test")

from core.config import get_bot_config
from core.models import MarketContext
from data_manager import load_effective_watchlist, load_orders, load_trade_history, resolve_ledger_scope
from price_fetcher import get_prices_batch
from strategies.dca import _check_hard_gates, _unrealized_loss_pct, dca_config, should_dca
from strategies.dca_recovery import evaluate_dca_recovery
from strategies.dca_portfolio import (
    _build_market,
    build_portfolio_dca_plan,
    collect_dca_targets,
    portfolio_config,
)
from strategies.positions import bootstrap_positions, get_position, list_active_positions
from strategies.registry import resolve_coin_config, resolve_strategy_params
from storage.mongo_client import resolve_database_name, resolve_mongo_uri


def _coin_params(coin: dict, pos: dict) -> dict:
    symbol = coin.get("symbol", "")
    tf = resolve_coin_config(coin).get("timeframe", "4h")
    return resolve_strategy_params(
        {"symbol": symbol, "timeframe": tf},
        has_position=True,
        frozen_tier=pos.get("strategy_tier"),
    )


def _position_rows(active: list[dict], prices: dict[str, float]) -> list[tuple]:
    rows = []
    for p in active:
        sym = p["symbol"]
        px = float(prices.get(sym, 0) or 0)
        entry = float(p.get("average_entry", 0) or 0)
        amt = float(p.get("amount", 0) or 0)
        gain = ((px / entry) - 1) * 100 if entry and px else 0.0
        notional = px * amt if px else 0.0
        sold = float(p.get("sold_percent", 0) or 0)
        rows.append((sym, gain, notional, sold))
    return rows


def _recent_trades_by_source(orders: list[dict], *, days: float = 2.0) -> dict[str, int]:
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [
        o for o in orders
        if o.get("status") == "filled"
        and ((o.get("timestamps") or {}).get("filled") or "")[:10] >= cut
    ]
    counts: dict[str, int] = {}
    for o in recent:
        src = str(o.get("source") or "?")
        counts[src] = counts.get(src, 0) + 1
    return counts


def _fmt_optional(value: float | None, *, digits: int = 2) -> str:
    if value is None:
        return "None"
    return f"{value:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview next cycle actions from current ledger")
    parser.add_argument("--detail", action="store_true", help="Show DCA hard-gate + scoring breakdown")
    args = parser.parse_args()

    scope = resolve_ledger_scope()
    bootstrap_positions(scope=scope)
    bot = get_bot_config()
    raw = bot.raw
    volatile_dca = (raw.get("volatile_altcoin") or {}).get("dca") or {}
    port_cfg = {**portfolio_config({}), **portfolio_config(volatile_dca)}

    history = load_trade_history()
    cash = float(history.get("virtual_balance", 0) or 0)
    active = list_active_positions()
    watchlist = load_effective_watchlist()
    open_syms = {p["symbol"] for p in active}
    open_wl = [c for c in watchlist if c.get("active", True) and c.get("symbol") in open_syms]
    prices = get_prices_batch(list(open_syms))

    nav = cash
    for p in active:
        px = float(prices.get(p["symbol"], 0) or 0)
        nav += float(p.get("amount", 0) or 0) * px

    uri = resolve_mongo_uri()
    host = uri.split("@")[-1] if "@" in uri else uri
    rows = _position_rows(active, prices)
    in_zone = [r for r in rows if -20 <= r[1] <= -3]
    winners8 = [r for r in rows if r[1] >= 8]

    print("=== TEST-LEDGER STAND ===")
    print(f"db={resolve_database_name()} host={host}")
    print(
        f"NAV ${nav:,.0f} | Cash ${cash:,.0f} | "
        f"Positions {len(active)}/{bot.max_open_positions}"
    )

    orders = [o for o in load_orders(scope).get("orders", []) if o.get("status") == "filled"]
    if orders:
        last = sorted(orders, key=lambda o: (o.get("timestamps") or {}).get("filled", ""))[-1]
        ts = str((last.get("timestamps") or {}).get("filled", ""))[:16]
        usdt = float((last.get("execution") or {}).get("usdt") or (last.get("request") or {}).get("usdt") or 0)
        print(f"Letzter Trade: {last.get('symbol')} {last.get('side')} {ts} ${usdt:,.0f}")

    print("\n=== STRATEGIE (Test-Config) ===")
    print(
        f"Portfolio-DCA: enabled={port_cfg.get('enabled')} mode={port_cfg.get('mode')} "
        f"max_buys={port_cfg.get('max_buys_per_cycle')} score≥{port_cfg.get('min_dca_score')}"
    )
    print("Loss-Zone -3%..-20%, Sizing $300–1200 (30% Positionswert), Cash-Buffer $300")
    print(f"Neue Einstiege: {'BLOCKIERT' if len(active) >= bot.max_open_positions else 'möglich'}")

    print("\n=== VERLIERER in DCA-Zone (-3%..-20%) ===")
    for sym, gain, notional, sold in sorted(in_zone, key=lambda r: r[1])[:12]:
        print(f"  {sym:14} {gain:+6.1f}%  ${notional:,.0f}  sold={sold:.0%}")
    if not in_zone:
        print("  — keine in Zone")

    print("\n=== GEWINNER ≥8% (Rotation-Pool) ===")
    for sym, gain, notional, sold in sorted(winners8, key=lambda r: -r[1])[:8]:
        print(f"  {sym:14} {gain:+6.1f}%  ${notional:,.0f}  sold={sold:.0%}")
    if not winners8:
        print("  — keiner ≥8%")

    print("\n=== PORTFOLIO-DCA (live OHLCV) ===")
    targets = collect_dca_targets(open_wl, prices, config_raw=raw)
    print(f"Qualifizierte Kandidaten (Score≥{port_cfg.get('min_dca_score')}): {len(targets)}")
    for t in sorted(targets, key=lambda x: -x.priority)[:6]:
        print(
            f"  {t.symbol:14} score={t.score:2d} loss={t.loss_pct:+6.1f}% "
            f"~${t.usdt_needed:,.0f} prio={t.priority:.1f}"
        )

    plan = build_portfolio_dca_plan(open_wl, prices, cash_available=cash, config_raw=raw)
    print("\n=== THEORETISCH NÄCHSTER ZYKLUS ===")
    if plan.buy:
        b = plan.buy
        print(
            f"→ BUY_DCA {b.symbol} ~${b.usdt_needed:,.0f} "
            f"(score {b.score}, loss {b.loss_pct:+.1f}%, source={b.source})"
        )
    else:
        print("→ kein Portfolio-DCA-Buy")
        if in_zone and not targets:
            print("  Grund: Positionen in Loss-Zone, aber Scoring < Schwelle")

    if plan.funding_sell:
        fs = plan.funding_sell
        print(f"→ Rotation-Sell {fs.symbol} +{fs.gain_pct:.1f}% ~${fs.expected_usdt:,.0f} ({fs.source})")
    else:
        print(f"→ kein Funding-Sell (Cash ${cash:,.0f} reicht, Buffer ${port_cfg.get('cash_buffer_usdt', 300):.0f})")

    print("→ TA-Auto-Buys: blockiert solange Slots voll")
    print("→ Exits (Ladder/Trailing/Stop): weiter auf offenen Positionen")

    cut = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d")
    recent = [
        o for o in orders
        if ((o.get("timestamps") or {}).get("filled") or "")[:10] >= cut
    ]
    print(f"\n=== LETZTE 24h ({len(recent)} Trades) ===")
    for o in sorted(recent, key=lambda x: (x.get("timestamps") or {}).get("filled", ""))[-6:]:
        ts = str((o.get("timestamps") or {}).get("filled", ""))[:16]
        usdt = float((o.get("execution") or {}).get("usdt") or (o.get("request") or {}).get("usdt") or 0)
        print(f"  {ts} {o.get('side', '').upper():4} {o.get('symbol', '?'):14} ${usdt:,.0f}  {o.get('source', '')}")

    by_src = _recent_trades_by_source(orders, days=2.0)
    dca_buys = [
        o for o in orders
        if o.get("status") == "filled"
        and o.get("side") == "buy"
        and o.get("source") in ("dca", "dca_recovery")
        and ((o.get("timestamps") or {}).get("filled") or "")[:10]
        >= (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    ]
    entry_buys = [
        o for o in orders
        if o.get("status") == "filled"
        and o.get("side") == "buy"
        and o.get("source") == "entry_sensor_15m"
        and ((o.get("timestamps") or {}).get("filled") or "")[:10]
        >= (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    ]
    print(f"\n=== FEATURE-WIRKUNG (letzte 48h) ===")
    print(f"Trades nach Quelle: {by_src}")
    print(f"Portfolio-DCA Käufe: {len(dca_buys)}  |  Entry-Sensor-15m: {len(entry_buys)}")
    if dca_buys:
        sizes = sorted({round(float((o.get("execution") or {}).get("usdt") or (o.get("request") or {}).get("usdt") or 0), 0) for o in dca_buys})
        print(f"DCA-Größen: {sizes} USDT (adaptives Sizing, nicht fix $400)")
    print(
        "Kein DCA jetzt ≠ Feature tot: Scoring filtert bewusst; "
        "Slots voll blockiert nur neue Einstiege, nicht DCA/Exits."
    )

    if args.detail:
        min_score = int(port_cfg.get("min_dca_score", 6))
        print(f"\n=== DCA HARD-GATE PASS + SCORING (Detail, min score {min_score}) ===")
        ranked: list[tuple[int, float, str, dict]] = []
        for p in sorted(active, key=lambda x: x["symbol"]):
            sym = p["symbol"]
            coin = next((c for c in open_wl if c.get("symbol") == sym), {"symbol": sym, "timeframe": "4h"})
            coin_cfg = resolve_coin_config(coin)
            tf = coin_cfg.get("timeframe", "4h")
            pos = get_position(sym, tf)
            px = float(prices.get(sym, 0) or 0)
            if px <= 0:
                print(f"  {sym:14} — kein Live-Preis")
                continue
            sp = _coin_params(coin, pos)
            dca_cfg = dca_config(sp)
            loss = _unrealized_loss_pct(float(pos.get("average_entry", 0) or 0), px)
            mc = MarketContext(
                symbol=sym,
                timeframe=tf,
                current_price=px,
                average_entry=float(pos.get("average_entry", 0) or 0),
                has_position=True,
                open_positions=1,
                strategy_params=sp,
            )
            ok, reason, _ = _check_hard_gates(mc, pos, sp, dca_cfg)
            if not ok:
                continue
            market = _build_market(sym, tf, px, pos, sp)
            dec = should_dca(market, pos, sp)
            recovery = evaluate_dca_recovery(market, pos, sp)
            core_keys = ("atr_distance", "rsi", "funding", "btc_underperf")
            core_met = sum(1 for k in core_keys if (dec.breakdown or {}).get(k, 0) > 0)
            ranked.append((dec.score, loss, sym, {"core": core_met, "should": dec.should_dca}))

            print(
                f"  {sym:14} loss={loss:+5.1f}% score={dec.score}/10 core={core_met}/4 "
                f"should={dec.should_dca}"
            )
            if dec.blocked_reason:
                print(f"    blocked: {dec.blocked_reason}")
            if dec.breakdown:
                print(f"    breakdown={dec.breakdown}")
            if recovery:
                print(
                    f"    recovery: score={recovery.score} usdt=${recovery.usdt_amount:,.0f} "
                    f"— {recovery.rationale}"
                )
            print(
                f"    market: rsi={market.rsi:.1f} atr%={market.atr_pct:.2f} "
                f"funding={_fmt_optional(market.funding_rate_pct, digits=4)} "
                f"btc_und={_fmt_optional(market.btc_underperf_ratio)} "
                f"bb={market.lower_bb:.6f} px={px:.6f}"
            )
            print()

        if ranked:
            ranked.sort(key=lambda r: (-r[0], r[1]))
            best_score, best_loss, best_sym, meta = ranked[0]
            print("=== KNAPPSTER KANDIDAT ===")
            print(
                f"  {best_sym}: score={best_score}/10 core={meta['core']}/4 loss={best_loss:+.1f}% "
                f"({'würde kaufen' if meta['should'] else 'noch geblockt'})"
            )
            if meta["core"] < 3 and best_score >= min_score:
                print("  → Score reicht, aber Core-Kriterien fehlen (oft Funding/BTC=None auf Test).")
        else:
            print("  Keine Position passiert Hard Gates.")

        print("\n=== IN LOSS-ZONE, ABER HARD-GATE BLOCK ===")
        zone_blocked = []
        for sym, gain, _, sold in sorted(in_zone, key=lambda r: r[1]):
            coin = next((c for c in open_wl if c.get("symbol") == sym), {"symbol": sym, "timeframe": "4h"})
            tf = resolve_coin_config(coin).get("timeframe", "4h")
            pos = get_position(sym, tf)
            px = float(prices.get(sym, 0) or 0)
            if px <= 0:
                continue
            sp = _coin_params(coin, pos)
            mc = MarketContext(
                symbol=sym,
                timeframe=tf,
                current_price=px,
                average_entry=float(pos.get("average_entry", 0) or 0),
                has_position=True,
                open_positions=1,
                strategy_params=sp,
            )
            ok, reason, _ = _check_hard_gates(mc, pos, sp, dca_config(sp))
            if not ok:
                zone_blocked.append((sym, gain, sold, reason))
        for sym, gain, sold, reason in zone_blocked[:10]:
            print(f"  {sym:14} {gain:+6.1f}% sold={sold:.0%}  → {reason}")
        if not zone_blocked:
            print("  —")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())