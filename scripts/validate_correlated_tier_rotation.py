#!/usr/bin/env python3
"""Ledger validation for the correlated-tier rotation experiment.

Tries MONGO_PUBLIC_URL / MONGO_URL / MONGODB_URI first, then falls back to
local snapshot JSON. Prints a four-part report:

  1. us_stock — old vs new trail on historical positions
  2. crypto_market — BTC/ETH drawdown events and open-book size
  3. stagnant-rotation — how often it would have fired
  4. false-positive check on non-selloff volatility

Usage:
  python3 scripts/validate_correlated_tier_rotation.py
  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test python3 scripts/validate_correlated_tier_rotation.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

US_STOCK = ("CRWVG/USDT", "NBISG/USDT", "SOXLG/USDT", "MVLLG/USDT")
BTC_ETH = ("BTC/USDT", "ETH/USDT")

OLD_TTP = {
    "enabled": True,
    "mode": "live",
    "dynamic_trail": True,
    "trail_pct": 6.0,
    "trail_pct_min": 3.0,
    "trail_pct_max": 12.0,
    "trail_pct_scale_start_pct": 18.0,
    "trail_pct_scale_peak_pct": 45.0,
    "arm_gain_pct": 10.0,
    "min_gain_pct": 8.0,
    "min_gain_pct_floor": 6.0,
    "trail_above_zero_after_arm": True,
    "max_steps": 1,
    "cooldown_hours": 0,
}
NEW_TTP = {
    **OLD_TTP,
    "dynamic_trail": False,
    "trail_pct": 3.5,
    "arm_gain_pct": 10.0,
    "min_gain_pct": 8.0,
    "full_close_gain_pct": 12.0,
}


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _order_ts(order: dict) -> datetime | None:
    ts = order.get("timestamps") or {}
    return _parse_ts(ts.get("filled") or ts.get("updated") or ts.get("created"))


def _order_px_amt(order: dict) -> tuple[float, float, float]:
    ex = order.get("execution") or {}
    req = order.get("request") or {}
    price = float(ex.get("price") or req.get("price") or 0)
    amount = float(ex.get("amount") or req.get("amount") or 0)
    usdt = ex.get("usdt") if ex.get("usdt") is not None else req.get("usdt")
    if usdt is not None:
        return price, amount, float(usdt)
    return price, amount, price * amount


def _load_json_orders(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("orders"), list):
        return [o for o in data["orders"] if isinstance(o, dict)]
    inner = data.get("orders")
    if isinstance(inner, dict) and isinstance(inner.get("orders"), list):
        return [o for o in inner["orders"] if isinstance(o, dict)]
    return []


def _try_mongo() -> tuple[list[dict], str]:
    """Return (orders, source_label) or ([], '')."""
    url = (
        os.environ.get("MONGO_PUBLIC_URL")
        or os.environ.get("MONGO_URL")
        or os.environ.get("MONGODB_URI")
        or ""
    ).strip()
    db_name = (os.environ.get("MONGODB_DB") or "xagent_test").strip()
    if not url:
        # last-ditch local operator DB (not Railway)
        url = "mongodb://127.0.0.1:27017"
        local_fallback = True
    else:
        local_fallback = url.startswith("mongodb://127.0.0.1") or "localhost" in url
    try:
        from pymongo import MongoClient
    except Exception as exc:
        return [], f"mongo-import-failed: {exc}"

    try:
        client = MongoClient(url, serverSelectionTimeoutMS=2500)
        client.admin.command("ping")
        db = client[db_name]
        coll_names = set(db.list_collection_names())
        orders: list[dict] = []
        if "orders" in coll_names:
            orders = list(db["orders"].find({}, {"_id": 0}))
        # some deployments split by tenant
        for name in sorted(coll_names):
            if name.startswith("orders") and name != "orders":
                orders.extend(list(db[name].find({}, {"_id": 0})))
        host = url.split("@")[-1].split("/")[0] if "@" in url else url.split("://", 1)[-1]
        label = f"mongo db={db_name} host={host} n={len(orders)}"
        if local_fallback:
            label += " (local; MONGO_PUBLIC_URL unset)"
        return orders, label
    except Exception as exc:
        return [], f"mongo-connect-failed: {exc}"


def _snapshot_candidates() -> list[Path]:
    return [
        ROOT / "data" / "demo_ledger_backup_railway_live.json",
        ROOT / "data" / "demo_ledger_pre_cleanup_backup.json",
        ROOT / "data" / "demo_ledger_backup_20260707_restored.json",
        ROOT / "auswertungen" / "ledger_backup_railway_test_20260708_235111.json",
    ]


def _best_snapshot() -> tuple[list[dict], Path | None]:
    best: list[dict] = []
    best_path: Path | None = None
    for path in _snapshot_candidates():
        if not path.exists():
            continue
        try:
            rows = _load_json_orders(path)
        except Exception:
            continue
        if len(rows) > len(best):
            best = rows
            best_path = path
    return best, best_path


def load_orders() -> tuple[list[dict], str]:
    orders, label = _try_mongo()
    filled_n = sum(1 for o in orders if str(o.get("status") or "").lower() == "filled")
    if orders and filled_n > 0:
        return orders, label
    mongo_note = label or "mongo: no data"
    if orders and filled_n == 0:
        mongo_note += " (0 filled — falling back to snapshot)"
    best, best_path = _best_snapshot()
    if best and best_path is not None:
        return best, f"local snapshot {best_path.relative_to(ROOT)} n={len(best)} ({mongo_note})"
    if orders:
        return orders, label
    return [], f"no ledger data ({mongo_note})"


@dataclass
class Lot:
    symbol: str
    timeframe: str
    entry_ts: datetime
    avg_entry: float
    amount: float
    peak_amount: float
    last_trade_at: datetime
    recent_high: float
    realized_pnl: float = 0.0
    buy_usdt: float = 0.0
    sells: list[dict] = field(default_factory=list)


def replay_lots(orders: list[dict]) -> tuple[list[Lot], list[tuple[datetime, dict[str, Lot]]]]:
    """Walk filled orders; return (closed_or_open lots, snapshots at each fill)."""
    active: dict[tuple[str, str], Lot] = {}
    finished: list[Lot] = []
    snapshots: list[tuple[datetime, dict[str, Lot]]] = []

    filled = [o for o in orders if str(o.get("status") or "").lower() == "filled"]
    filled.sort(key=lambda o: _order_ts(o) or datetime.min.replace(tzinfo=timezone.utc))

    for order in filled:
        ts = _order_ts(order)
        if not ts:
            continue
        symbol = str(order.get("symbol") or "")
        tf = str(order.get("timeframe") or "4h")
        side = str(order.get("side") or "").lower()
        price, amount, usdt = _order_px_amt(order)
        if not symbol or price <= 0 or amount <= 0:
            continue
        key = (symbol, tf)
        lot = active.get(key)
        if side == "buy":
            if lot is None or lot.amount <= 1e-12:
                lot = Lot(
                    symbol=symbol,
                    timeframe=tf,
                    entry_ts=ts,
                    avg_entry=price,
                    amount=amount,
                    peak_amount=amount,
                    last_trade_at=ts,
                    recent_high=price,
                    buy_usdt=usdt,
                )
                active[key] = lot
            else:
                cost = lot.avg_entry * lot.amount + usdt
                lot.amount += amount
                lot.avg_entry = cost / lot.amount if lot.amount else price
                lot.peak_amount = max(lot.peak_amount, lot.amount)
                lot.last_trade_at = ts
                lot.recent_high = max(lot.recent_high, price)
                lot.buy_usdt += usdt
        elif side == "sell" and lot is not None and lot.amount > 1e-12:
            lot.sells.append(
                {
                    "ts": ts,
                    "price": price,
                    "amount": amount,
                    "usdt": usdt,
                    "signal": str(order.get("signal") or ""),
                    "source": str(order.get("source") or ""),
                    "gain_pct": ((price / lot.avg_entry) - 1) * 100 if lot.avg_entry else 0.0,
                }
            )
            lot.recent_high = max(lot.recent_high, price)
            lot.amount = max(0.0, lot.amount - amount)
            lot.last_trade_at = ts
            lot.realized_pnl += float(order.get("pnl") or 0)
            if lot.amount <= 1e-12:
                finished.append(lot)
                del active[key]

        snapshots.append(
            (ts, {s: active[(s, t)] for (s, t) in active if active[(s, t)].amount > 1e-12})
        )

    for lot in active.values():
        if lot.amount > 1e-12:
            finished.append(lot)
    return finished, snapshots


def _eval_ttp(entry: float, high: float, price: float, cfg: dict) -> dict | None:
    from core.models import MarketContext
    from strategies.trailing_take_profit import evaluate_trailing_take_profit

    market = MarketContext(
        symbol="X/USDT",
        timeframe="4h",
        current_price=price,
        has_position=True,
        average_entry=entry,
    )
    pos = {"recent_high": high, "amount": 100.0, "peak_amount": 100.0}
    params = {"trailing_take_profit": cfg, "exit_ladder": {"enabled": False}}
    cand = evaluate_trailing_take_profit(market, pos, params)
    if cand is None:
        return None
    return {"action": cand.action, "rationale": cand.rationale}


def report_us_stock(lots: list[Lot], snapshots: list) -> list[str]:
    lines = ["## 1. us_stock — old vs new trail"]
    us_lots = [l for l in lots if l.symbol in US_STOCK]
    lines.append(f"us_stock lots in ledger: {len(us_lots)} symbols={sorted({l.symbol for l in us_lots}) or 'none'}")
    # Also scan sell prints for overlay-relevant gain
    analog = [l for l in lots if l.sells]
    would_full_close = 0
    would_trail_sooner = 0
    samples: list[str] = []
    compared = 0
    for lot in analog:
        if lot.avg_entry <= 0:
            continue
        # reconstruct a crude peak from sell prints + recent_high
        high = max([lot.avg_entry] + [s["price"] for s in lot.sells] + [lot.recent_high])
        for sell in lot.sells:
            px = sell["price"]
            gain = sell["gain_pct"]
            old = _eval_ttp(lot.avg_entry, high, px, OLD_TTP)
            new = _eval_ttp(lot.avg_entry, high, px, NEW_TTP)
            compared += 1
            if new and not old:
                would_trail_sooner += 1
            if new and "full_close" in (new.get("rationale") or ""):
                would_full_close += 1
            if len(samples) < 8 and (new or old) and gain >= 8:
                samples.append(
                    f"  {lot.symbol} {sell['ts'].date()} gain={gain:.1f}% high={(high/lot.avg_entry-1)*100:.1f}% "
                    f"old={old['action'] if old else '-'} new={new['action'] if new else '-'}"
                    f"{' FULL_CLOSE' if new and 'full_close' in (new.get('rationale') or '') else ''}"
                )
    lines.append(
        f"counterfactual on all filled sells (n={compared}): "
        f"new fires when old silent={would_trail_sooner}, "
        f"new full_close_gain hits={would_full_close}"
    )
    if us_lots:
        for lot in us_lots:
            peak = (lot.recent_high / lot.avg_entry - 1) * 100 if lot.avg_entry else 0
            lines.append(
                f"  {lot.symbol} entry={lot.avg_entry:.6g} peak≈{peak:.1f}% "
                f"sells={len(lot.sells)} still_open={lot.amount>1e-12}"
            )
    else:
        lines.append(
            "No CRWVG/NBISG/SOXLG/MVLLG lots in the available ledger. "
            "Cannot compare old vs new trail on real us_stock positions. "
            "Counterfactual above uses the rest of the book as a stand-in for "
            "arm=10 / trail=3.5 / full_close=12 vs volatile dynamic trail."
        )
        # concrete numbers from analog greens that crossed 12%
        crossed = 0
        full_close_examples = []
        for lot in analog:
            if lot.avg_entry <= 0:
                continue
            high = max([lot.avg_entry] + [s["price"] for s in lot.sells] + [lot.recent_high])
            peak = (high / lot.avg_entry - 1) * 100
            if peak < 12:
                continue
            crossed += 1
            # first sell at/after 12% would have been a full close under new overlay
            for sell in lot.sells:
                if sell["gain_pct"] >= 12:
                    full_close_examples.append(
                        f"  {lot.symbol} first ≥12% print {sell['ts'].date()} "
                        f"gain={sell['gain_pct']:.1f}% signal={sell['signal'] or sell['source'] or '?'}"
                    )
                    break
        lines.append(
            f"lots whose reconstructed peak ≥12%: {crossed} "
            f"(new overlay would prefer SELL_FULL at that print instead of trailing)"
        )
        lines.extend(full_close_examples[:10])
    lines.extend(samples)
    return lines


def _fetch_ohlcv(symbol: str, days: int = 60, timeframe: str = "1h") -> list[list]:
    try:
        from historical_prices import _fetch_ohlcv_range

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return _fetch_ohlcv_range(symbol, start, end, timeframe=timeframe) or []
    except Exception:
        return []


def _open_count_at(snapshots: list[tuple[datetime, dict]], when: datetime) -> int:
    if not snapshots:
        return 0
    best = 0
    for ts, book in snapshots:
        if ts <= when:
            best = len(book)
        else:
            break
    return best


def report_crypto_market(lots: list[Lot], snapshots: list) -> list[str]:
    lines = ["## 2. crypto_market — BTC/ETH drawdown detector"]
    from services.correlated_tier.drawdown_tracker import GroupDrawdownTracker

    btc = _fetch_ohlcv("BTC/USDT", days=60, timeframe="1h")
    eth = _fetch_ohlcv("ETH/USDT", days=60, timeframe="1h")
    btc_px: list[tuple[float, float]] = []
    eth_px: list[tuple[float, float]] = []
    if btc or eth:
        price_source = (
            "ccxt/gate 1h OHLCV via historical_prices "
            "(high then close per bar — 4%/15m is intra-hour; 1h high/close is the available proxy)"
        )
        # Feed bar-high then close so a dump inside the hour can still trip window-high.
        for b in btc:
            ts = b[0] / 1000.0
            btc_px.append((ts, float(b[2])))
            btc_px.append((ts + 1, float(b[4])))
        for b in eth:
            ts = b[0] / 1000.0
            eth_px.append((ts, float(b[2])))
            eth_px.append((ts + 1, float(b[4])))
    else:
        price_source = "ledger fill prices only (OHLCV fetch empty/failed)"
        for lot in lots:
            if lot.symbol == "BTC/USDT":
                btc_px.extend((s["ts"].timestamp(), s["price"]) for s in lot.sells)
            if lot.symbol == "ETH/USDT":
                eth_px.extend((s["ts"].timestamp(), s["price"]) for s in lot.sells)

    lines.append(f"price source: {price_source}")
    lines.append(f"BTC samples={len(btc_px)} ETH samples={len(eth_px)}")

    tr = GroupDrawdownTracker(
        "crypto_market",
        ["BTC/USDT", "ETH/USDT"],
        drawdown_pct=4.0,
        window_sec=900.0,
        min_confirming=1,
    )
    events: list[tuple[float, int]] = []
    active = False
    # merge ticks
    ticks: list[tuple[float, str, float]] = []
    for ts, px in btc_px:
        ticks.append((ts, "BTC/USDT", px))
    for ts, px in eth_px:
        ticks.append((ts, "ETH/USDT", px))
    ticks.sort()
    for ts, sym, px in ticks:
        tr.on_tick(sym, px, now=ts)
        ev = tr.evaluate(now=ts)
        if ev["active"] and not active:
            when = datetime.fromtimestamp(ts, tz=timezone.utc)
            events.append((ts, _open_count_at(snapshots, when)))
        active = bool(ev["active"])

    lines.append(
        f"detector window=900s drawdown=4% min_confirming=1 → "
        f"{len(events)} rising-edge events in the sample"
    )
    if events:
        opens = [n for _, n in events]
        lines.append(
            f"open positions at event: min={min(opens)} median={sorted(opens)[len(opens)//2]} "
            f"max={max(opens)} mean={sum(opens)/len(opens):.1f}"
        )
        for ts, n in events[:12]:
            when = datetime.fromtimestamp(ts, tz=timezone.utc)
            lines.append(f"  {when.isoformat()} open_lots≈{n}")
        if len(events) > 12:
            lines.append(f"  ... +{len(events)-12} more")
    else:
        lines.append("No 4%/15m confirmed drawdowns in the available price sample.")
    return lines


def report_stagnant(lots: list[Lot], snapshots: list, max_open: int = 36) -> list[str]:
    lines = ["## 3. stagnant-rotation — historical fires"]
    slack = 2
    gain_need = 8.0
    idle_need = 24.0
    fires = []
    # evaluate each lot at each later snapshot while it was open
    by_sym: dict[str, Lot] = {}
    for lot in lots:
        by_sym.setdefault(lot.symbol, lot)
    checked = 0
    for ts, book in snapshots:
        open_full = len(book)
        tight = open_full >= max_open - slack
        if not tight:
            continue
        for sym, lot in book.items():
            checked += 1
            if lot.avg_entry <= 0:
                continue
            # mark-to-market proxy: last known trade price on this lot
            last_px = lot.recent_high
            if lot.sells:
                last_px = lot.sells[-1]["price"]
            gain = (last_px / lot.avg_entry - 1) * 100
            idle_h = (ts - lot.last_trade_at).total_seconds() / 3600.0
            if gain >= gain_need and idle_h >= idle_need:
                fires.append((ts, sym, gain, idle_h, open_full))
    # de-dupe per symbol/day
    seen = set()
    unique = []
    for row in fires:
        key = (row[1], row[0].date())
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    lines.append(
        f"capacity {max_open}, slack={slack}: tight snapshots checked={checked}, "
        f"raw fires={len(fires)}, unique symbol-days={len(unique)}"
    )
    for ts, sym, gain, idle_h, slots in unique[:15]:
        lines.append(
            f"  {ts.date()} {sym} gain≈{gain:.1f}% idle≈{idle_h:.0f}h slots={slots}/{max_open}"
        )
    if len(unique) > 15:
        lines.append(f"  ... +{len(unique)-15} more")
    if not unique:
        if checked == 0:
            lines.append(
                "Book never reached cap-slack on this ledger, so the capacity gate "
                "never opened."
            )
        else:
            lines.append(
                "Book did go tight, but no open lot was simultaneously ≥+8% and "
                "idle ≥24h at those snapshots (last_trade_at is the last fill on "
                "that lot — frequent partials reset the idle clock)."
            )
    return lines


def report_false_positives() -> list[str]:
    lines = ["## 4. false-positive check (non-selloff volatility)"]
    from services.correlated_tier.drawdown_tracker import GroupDrawdownTracker

    btc = _fetch_ohlcv("BTC/USDT", days=60, timeframe="1h")
    if not btc:
        lines.append(
            "Cannot check false positives: no BTC OHLCV available "
            "(fetch failed). Need Gate history or a stored dump to compare "
            "slow bleeds / weekend chop against the 4%/15m rule."
        )
        return lines

    tr_fast = GroupDrawdownTracker(
        "crypto_market",
        ["BTC/USDT"],
        drawdown_pct=4.0,
        window_sec=900.0,
        min_confirming=1,
    )
    # Contrast: same 4% but a 12h window (slow bleed should trip this, not the 15m rule)
    tr_slow = GroupDrawdownTracker(
        "crypto_market_slow",
        ["BTC/USDT"],
        drawdown_pct=4.0,
        window_sec=12 * 3600.0,
        min_confirming=1,
    )
    fast_fires = 0
    slow_fires = 0
    hourly_range_ge4 = 0
    active_f = False
    active_s = False
    for bar in btc:
        ts = bar[0] / 1000.0
        high, close = float(bar[2]), float(bar[4])
        if high > 0 and (1 - close / high) * 100 >= 4:
            hourly_range_ge4 += 1
        tr_fast.on_tick("BTC/USDT", high, now=ts)
        tr_fast.on_tick("BTC/USDT", close, now=ts + 1)
        tr_slow.on_tick("BTC/USDT", high, now=ts)
        tr_slow.on_tick("BTC/USDT", close, now=ts + 1)
        ev_f = tr_fast.evaluate(now=ts + 1)
        ev_s = tr_slow.evaluate(now=ts + 1)
        if ev_f["active"] and not active_f:
            fast_fires += 1
        if ev_s["active"] and not active_s:
            slow_fires += 1
        active_f = bool(ev_f["active"])
        active_s = bool(ev_s["active"])

    lines.append(
        f"60d BTC 1h bars={len(btc)}: 4%/15m-window rising-edge={fast_fires}; "
        f"4%/12h-window rising-edge={slow_fires}; "
        f"hours with high→close ≥4%={hourly_range_ge4}."
    )
    lines.append(
        "The production detector is window-high over 900s, not ATH and not a 12h "
        "bleed. Hours with a ≥4% high→close range can exist without a 15m "
        "confirmed dump. Without a labeled non-selloff calendar we cannot prove "
        "a false positive; the 12h contrast plus unit test_slow_bleed_not_active "
        "is the available check."
    )
    if slow_fires > fast_fires:
        lines.append(
            f"Slow 12h window fired {slow_fires - fast_fires} more times than the "
            f"15m rule — those extra events are the 'would-be false positives' "
            f"the short window is designed to ignore."
        )
    return lines


def main() -> int:
    orders, source = load_orders()
    print("# correlated-tier rotation — ledger validation")
    print(f"data source: {source}")
    print(f"generated: {datetime.now(timezone.utc).isoformat()}")
    if not orders:
        print("No orders loaded. Nothing to validate.")
        return 1

    filled = [o for o in orders if str(o.get("status") or "").lower() == "filled"]
    print(f"orders={len(orders)} filled={len(filled)}")
    lots, snapshots = replay_lots(orders)
    print(f"reconstructed lots={len(lots)} fill-snapshots={len(snapshots)}")
    print()

    sections = []
    sections.extend(report_us_stock(lots, snapshots))
    sections.append("")
    sections.extend(report_crypto_market(lots, snapshots))
    sections.append("")
    sections.extend(report_stagnant(lots, snapshots))
    sections.append("")
    sections.extend(report_false_positives())
    print("\n".join(sections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
