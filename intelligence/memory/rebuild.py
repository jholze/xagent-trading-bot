"""Rebuild TradeMemory + CoinProfile from filled orders (READ-ONLY ledger)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from intelligence.memory.models import CoinProfile, TradeMemory, utc_now_iso
from intelligence.memory.store import MemoryStore
from logger import log

SENSOR_SOURCES = frozenset(
    {"entry_sensor_15m", "vol_spike_15m", "entry_sensor", "15m_sensor"}
)


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:32] if len(s) > 32 and "+" not in s[19:25] else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _gross_loss_cfg(config_raw: dict | None = None) -> dict[str, Any]:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    mem = (config_raw or {}).get("memory") or {}
    gl = mem.get("gross_loss") or {}
    return {
        "enabled": bool(gl.get("enabled", True)),
        "min_loss_pct": float(gl.get("min_loss_pct", 25)),
        "min_loss_usdt": float(gl.get("min_loss_usdt", 500)),
        "soft_block_ttl_hours": float(gl.get("soft_block_ttl_hours", 336)),
        "size_bias_cap": float(gl.get("size_bias_cap", 0.5)),
        "soft_block_scope": str(gl.get("soft_block_scope") or "sensor_only"),
    }


def orders_to_trade_memories(
    orders: list[dict],
    *,
    ledger_scope: str,
    tenant_id: str,
    lookback_days: int = 90,
) -> list[TradeMemory]:
    """Map filled orders → TradeMemory (one row per fill)."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=int(lookback_days))
    out: list[TradeMemory] = []
    for o in orders:
        if str(o.get("status") or "").lower() != "filled":
            continue
        side = str(o.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        ts = o.get("timestamps") or {}
        filled = _parse_ts(ts.get("filled") or ts.get("created"))
        if filled is None or filled < since:
            continue
        ot = str(o.get("tenant_id") or tenant_id or "default")
        if tenant_id and ot != tenant_id and ot != "default":
            if tenant_id != "default":
                continue
        req = o.get("request") or {}
        ex = o.get("execution") or {}
        price = float(ex.get("price") or req.get("price") or 0)
        usdt = float(ex.get("usdt") or req.get("usdt") or 0)
        pnl = o.get("pnl")
        if isinstance(pnl, dict):
            pnl = pnl.get("usdt") or pnl.get("realized")
        try:
            pnl_f = float(pnl) if pnl is not None else None
        except Exception:
            pnl_f = None
        outcome = "open"
        if side == "sell" and pnl_f is not None:
            if pnl_f > 0.5:
                outcome = "win"
            elif pnl_f < -0.5:
                outcome = "loss"
            else:
                outcome = "breakeven"
        tid = str(o.get("id") or o.get("display_seq") or f"{o.get('symbol')}_{filled.isoformat()}")
        meta: dict[str, Any] = {"usdt": usdt, "status": "filled"}
        venue = ex.get("venue")
        if isinstance(venue, dict):
            meta["venue"] = venue
        out.append(
            TradeMemory(
                trade_id=f"{ledger_scope}:{tid}",
                symbol=str(o.get("symbol") or "?"),
                entry_time=filled.strftime("%Y-%m-%dT%H:%M:%SZ"),
                exit_time=filled.strftime("%Y-%m-%dT%H:%M:%SZ") if side == "sell" else "",
                direction=side,
                entry_price=price,
                exit_price=price if side == "sell" else 0.0,
                pnl_usdt=pnl_f,
                source=str(o.get("source") or "order"),
                outcome=outcome,
                reason=str(o.get("signal") or o.get("source") or ""),
                ledger_scope=ledger_scope,
                tenant_id=tenant_id,
                metadata=meta,
            )
        )
    return out


def _by_source_stats(trades: list[TradeMemory]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"buys": 0, "sells": 0, "pnl_usdt": 0.0}
    )
    for t in trades:
        src = (t.source or "unknown").lower()
        if t.direction == "buy":
            by[src]["buys"] += 1
        elif t.direction == "sell":
            by[src]["sells"] += 1
            if t.pnl_usdt is not None:
                by[src]["pnl_usdt"] = float(by[src]["pnl_usdt"]) + float(t.pnl_usdt)
    return {k: dict(v) for k, v in by.items()}


def _venue_features(trades: list[TradeMemory], config_raw: dict | None = None) -> dict[str, Any]:
    from services.venue_quality import is_thin_venue_stamp, venue_quality_config

    vcfg = venue_quality_config(config_raw)
    thin_n = 0
    thick_n = 0
    pnl_thin = 0.0
    pnl_thick = 0.0
    last_qv = None
    last_spread = None
    last_stamp = None
    for t in trades:
        if t.direction != "buy":
            continue
        stamp = (t.metadata or {}).get("venue")
        if not isinstance(stamp, dict):
            continue
        last_stamp = stamp
        last_qv = stamp.get("quote_volume_24h_usdt")
        last_spread = stamp.get("spread_pct")
        thin = is_thin_venue_stamp(stamp, vcfg)
        # attach pnl from later sells loosely: use sell on same symbol after this buy
        sell_pnl = 0.0
        for s in trades:
            if s.direction != "sell" or s.pnl_usdt is None:
                continue
            if s.entry_time and t.entry_time and s.entry_time >= t.entry_time:
                sell_pnl += float(s.pnl_usdt)
        if thin:
            thin_n += 1
            pnl_thin += sell_pnl
        else:
            thick_n += 1
            pnl_thick += sell_pnl
    if last_stamp is None and thin_n == 0 and thick_n == 0:
        return {}
    out: dict[str, Any] = {
        "entries_thin_30d": thin_n,
        "entries_thick_30d": thick_n,
        "pnl_when_thin_usdt": round(pnl_thin, 2),
        "pnl_when_thick_usdt": round(pnl_thick, 2),
    }
    if last_qv is not None:
        out["last_entry_quote_vol_24h"] = float(last_qv)
    if last_spread is not None:
        out["last_entry_spread_pct"] = float(last_spread)
    if thin_n + thick_n > 0:
        out["thin_loss_rate"] = round(
            (1.0 if pnl_thin < -0.5 else 0.0) if thin_n else 0.0, 3
        )
    return out


def compute_profile_from_trades(
    symbol: str,
    trades: list[TradeMemory],
    *,
    ledger_scope: str,
    tenant_id: str,
    min_samples: int = 3,
    config_raw: dict | None = None,
) -> CoinProfile:
    sells = [t for t in trades if t.direction == "sell"]
    buys = [t for t in trades if t.direction == "buy"]
    pnls = [float(t.pnl_usdt) for t in sells if t.pnl_usdt is not None]
    wins = sum(1 for p in pnls if p > 0.5)
    win_rate = (wins / len(pnls)) if pnls else 0.0
    total_pnl = sum(pnls) if pnls else 0.0
    avg_pnl = (total_pnl / len(pnls)) if pnls else 0.0
    dca = sum(1 for t in buys if "dca" in (t.source or "").lower())

    size_bias = 1.0
    entry_bias = "neutral"
    rationale = "insufficient samples"
    n = len(pnls)
    gl = _gross_loss_cfg(config_raw)
    features: dict[str, Any] = {
        "min_samples": min_samples,
        "sample_n": n,
        "by_source": _by_source_stats(trades),
    }
    venue_f = _venue_features(trades, config_raw)
    if venue_f:
        features["venue"] = venue_f

    worst_usdt = min(pnls) if pnls else 0.0
    worst_pct = 0.0
    last_loss_at = None
    last_loss_source = None
    for t in sells:
        if t.pnl_usdt is None or float(t.pnl_usdt) >= 0:
            continue
        # approximate pct from entry/exit if present
        pct = 0.0
        if t.entry_price and t.exit_price and t.entry_price > 0:
            pct = (float(t.exit_price) / float(t.entry_price) - 1.0) * 100.0
        if float(t.pnl_usdt) <= worst_usdt:
            worst_usdt = float(t.pnl_usdt)
            worst_pct = pct
            last_loss_at = t.exit_time or t.entry_time
            last_loss_source = t.source
    if pnls:
        features["worst_loss_usdt"] = round(worst_usdt, 2)
        features["worst_loss_pct"] = round(worst_pct, 2)
        if last_loss_at:
            features["last_loss_at"] = last_loss_at
        if last_loss_source:
            features["last_loss_source"] = last_loss_source

    # Gross-loss soft_block even when n < min_samples (BDX-class)
    gross = False
    if gl.get("enabled", True) and pnls:
        if worst_usdt <= -abs(gl["min_loss_usdt"]) or worst_pct <= -abs(gl["min_loss_pct"]):
            gross = True

    if gross:
        size_bias = min(float(gl["size_bias_cap"]), 0.5)
        entry_bias = "soft_block"
        rationale = (
            f"gross_loss n={n} worst_usdt={worst_usdt:.1f} worst_pct={worst_pct:.1f} "
            f"scope={gl.get('soft_block_scope')}"
        )
        ttl_h = float(gl.get("soft_block_ttl_hours") or 336)
        if last_loss_at:
            try:
                start = _parse_ts(last_loss_at) or datetime.now(timezone.utc)
                until = start + timedelta(hours=ttl_h)
                features["soft_block_until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
        features["soft_block_scope"] = gl.get("soft_block_scope") or "sensor_only"
        # thin venue boost TTL already encoded if needed by reflector later
    elif n >= min_samples:
        if win_rate < 0.35 or total_pnl < -50:
            size_bias = 0.65
            entry_bias = "soft_block"
            rationale = f"weak history win_rate={win_rate:.0%} n={n} pnl={total_pnl:.1f}"
        elif win_rate < 0.45 or total_pnl < 0:
            size_bias = 0.85
            rationale = f"mixed history win_rate={win_rate:.0%} n={n} pnl={total_pnl:.1f}"
        elif win_rate >= 0.55 and total_pnl > 20:
            size_bias = 1.1
            entry_bias = "prefer"
            rationale = f"strong history win_rate={win_rate:.0%} n={n} pnl={total_pnl:.1f}"
        else:
            rationale = f"ok history win_rate={win_rate:.0%} n={n}"
    elif n > 0:
        rationale = f"few samples n={n} (fail-open bias=1.0)"

    risk = 0.5
    if n >= min_samples or gross:
        risk = max(0.1, min(0.9, 1.0 - win_rate + (0.2 if total_pnl < 0 else 0)))

    return CoinProfile(
        symbol=symbol,
        ledger_scope=ledger_scope,
        tenant_id=tenant_id,
        as_of=utc_now_iso(),
        trades_30d=len(trades),
        sells_30d=len(sells),
        buys_30d=len(buys),
        win_rate=round(win_rate, 4),
        total_pnl_usdt=round(total_pnl, 2),
        avg_pnl_usdt=round(avg_pnl, 2),
        dca_count_30d=dca,
        size_bias=size_bias,
        entry_bias=entry_bias,
        risk_score=round(risk, 3),
        rationale=rationale,
        features=features,
    )


def load_filled_orders_readonly(
    ledger_scope: str | None = None,
    tenant_id: str = "default",
) -> tuple[list[dict], str]:
    """READ-ONLY load of orders — never mutates ledger."""
    try:
        from data_manager import load_orders, resolve_ledger_scope

        scope = ledger_scope or resolve_ledger_scope()
        data = load_orders(scope, tenant_id=tenant_id) or {}
        orders = list(data.get("orders") or [])
        return orders, scope
    except Exception as e:
        log(f"memory rebuild: load_orders failed (fail-open): {e}", "WARNING")
        return [], ledger_scope or "live"


def trade_history_to_trade_memories(
    rows: list[dict],
    *,
    ledger_scope: str,
    tenant_id: str,
    lookback_days: int = 90,
) -> list[TradeMemory]:
    """Map trade_history.trades[] rows → TradeMemory (READ-ONLY history doc).

    Fills gaps when the orders collection is sparse (common on staging demo).
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=int(lookback_days))
    out: list[TradeMemory] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type") or row.get("side") or "").upper()
        if typ in ("BUY", "B"):
            side = "buy"
        elif typ in ("SELL", "S"):
            side = "sell"
        else:
            continue
        filled = _parse_ts(row.get("timestamp") or row.get("time") or row.get("filled_at"))
        if filled is None or filled < since:
            continue
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        if "/" not in sym:
            sym = f"{sym}/USDT"
        try:
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            pnl_f = float(row["pnl"]) if row.get("pnl") is not None else None
        except (TypeError, ValueError):
            pnl_f = None
        outcome = "open"
        if side == "sell" and pnl_f is not None:
            if pnl_f > 0.5:
                outcome = "win"
            elif pnl_f < -0.5:
                outcome = "loss"
            else:
                outcome = "breakeven"
        oid = str(row.get("order_id") or row.get("exchange_order_id") or "")
        tid = oid or f"th_{sym}_{filled.strftime('%Y%m%d%H%M%S')}_{side}"
        usdt = 0.0
        try:
            usdt = float(row.get("usdt_amount") or row.get("usdt_received") or 0)
        except (TypeError, ValueError):
            usdt = 0.0
        out.append(
            TradeMemory(
                trade_id=f"{ledger_scope}:th:{tid}",
                symbol=sym,
                entry_time=filled.strftime("%Y-%m-%dT%H:%M:%SZ"),
                exit_time=filled.strftime("%Y-%m-%dT%H:%M:%SZ") if side == "sell" else "",
                direction=side,
                entry_price=price,
                exit_price=price if side == "sell" else 0.0,
                pnl_usdt=pnl_f,
                source=str(row.get("source") or "trade_history"),
                outcome=outcome,
                reason=str(row.get("signal") or row.get("type") or side.upper()),
                ledger_scope=ledger_scope,
                tenant_id=tenant_id,
                metadata={"usdt": usdt, "from": "trade_history"},
            )
        )
    return out


def load_trade_history_rows_readonly(
    ledger_scope: str | None = None,
    tenant_id: str = "default",
) -> list[dict]:
    """READ-ONLY trade_history document rows — never mutates ledger."""
    try:
        from data_manager import load_trade_history_document, resolve_ledger_scope

        scope = ledger_scope or resolve_ledger_scope()
        # demo scope uses demo history; paper/live as configured
        hist_scope = "demo" if scope == "demo" else scope
        if hist_scope == "live":
            # live dry-run often uses live trade history helper
            try:
                from data_manager import load_live_trade_history

                doc = load_live_trade_history() or {}
                return list(doc.get("trades") or [])
            except Exception:
                pass
        doc = load_trade_history_document(hist_scope, tenant_id=tenant_id) or {}
        return list(doc.get("trades") or [])
    except Exception as e:
        log(f"memory rebuild: trade_history load failed (fail-open): {e}", "WARNING")
        return []


def _merge_trade_memories(*groups: list[TradeMemory]) -> list[TradeMemory]:
    """Dedupe by trade_id; prefer first occurrence."""
    by_id: dict[str, TradeMemory] = {}
    for group in groups:
        for t in group or []:
            if not t or not t.trade_id:
                continue
            if t.trade_id not in by_id:
                by_id[t.trade_id] = t
    return list(by_id.values())


def rebuild_from_orders(
    store: MemoryStore | None = None,
    *,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
    lookback_days: int = 90,
    min_samples: int = 3,
    config_raw: dict | None = None,
) -> dict[str, int]:
    """Full rebuild of trade memories + profiles. Ledger read-only.

    Sources (merged, deduped):
      1) filled orders collection
      2) trade_history document (fills gaps when orders sparse)
    """
    store = store or MemoryStore()
    orders, scope = load_filled_orders_readonly(ledger_scope, tenant_id)
    from_orders = orders_to_trade_memories(
        orders, ledger_scope=scope, tenant_id=tenant_id, lookback_days=lookback_days
    )
    hist_rows = load_trade_history_rows_readonly(scope, tenant_id)
    from_hist = trade_history_to_trade_memories(
        hist_rows, ledger_scope=scope, tenant_id=tenant_id, lookback_days=lookback_days
    )
    trades = _merge_trade_memories(from_orders, from_hist)
    for t in trades:
        store.upsert_trade(t)

    by_sym: dict[str, list[TradeMemory]] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    n_prof = 0
    for sym, tlist in by_sym.items():
        prof = compute_profile_from_trades(
            sym,
            tlist,
            ledger_scope=scope,
            tenant_id=tenant_id,
            min_samples=min_samples,
            config_raw=config_raw,
        )
        if store.upsert_profile(prof):
            n_prof += 1

    log(
        f"memory rebuild: orders={len(orders)} hist_rows={len(hist_rows)} "
        f"trades={len(trades)} (ord={len(from_orders)} hist={len(from_hist)}) "
        f"profiles={n_prof} scope={scope}",
        "INFO",
    )
    return {
        "orders_read": len(orders),
        "hist_rows_read": len(hist_rows),
        "trades_from_orders": len(from_orders),
        "trades_from_history": len(from_hist),
        "trades_written": len(trades),
        "profiles_written": n_prof,
        "ledger_scope": scope,
    }
