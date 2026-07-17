"""Rebuild TradeMemory + CoinProfile from filled orders (READ-ONLY ledger)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from intelligence.memory.models import CoinProfile, TradeMemory, utc_now_iso
from intelligence.memory.store import MemoryStore
from logger import log


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
        # tenant filter if present on order
        ot = str(o.get("tenant_id") or tenant_id or "default")
        if tenant_id and ot != tenant_id and ot != "default":
            # allow default-scoped rebuild of default tenant
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
                metadata={"usdt": usdt, "status": "filled"},
            )
        )
    return out


def compute_profile_from_trades(
    symbol: str,
    trades: list[TradeMemory],
    *,
    ledger_scope: str,
    tenant_id: str,
    min_samples: int = 3,
) -> CoinProfile:
    sells = [t for t in trades if t.direction == "sell"]
    buys = [t for t in trades if t.direction == "buy"]
    pnls = [float(t.pnl_usdt) for t in sells if t.pnl_usdt is not None]
    wins = sum(1 for p in pnls if p > 0.5)
    win_rate = (wins / len(pnls)) if pnls else 0.0
    total_pnl = sum(pnls) if pnls else 0.0
    avg_pnl = (total_pnl / len(pnls)) if pnls else 0.0
    dca = sum(1 for t in buys if "dca" in (t.source or "").lower())

    # size_bias: shrink after poor sell history; expand slightly after good
    size_bias = 1.0
    entry_bias = "neutral"
    rationale = "insufficient samples"
    n = len(pnls)
    if n >= min_samples:
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
    if n >= min_samples:
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
        features={"min_samples": min_samples, "sample_n": n},
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


def rebuild_from_orders(
    store: MemoryStore | None = None,
    *,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
    lookback_days: int = 90,
    min_samples: int = 3,
) -> dict[str, int]:
    """Full rebuild of trade memories + profiles. Ledger read-only."""
    store = store or MemoryStore()
    orders, scope = load_filled_orders_readonly(ledger_scope, tenant_id)
    trades = orders_to_trade_memories(
        orders, ledger_scope=scope, tenant_id=tenant_id, lookback_days=lookback_days
    )
    for t in trades:
        store.upsert_trade(t)

    by_sym: dict[str, list[TradeMemory]] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    n_prof = 0
    for sym, tlist in by_sym.items():
        prof = compute_profile_from_trades(
            sym, tlist, ledger_scope=scope, tenant_id=tenant_id, min_samples=min_samples
        )
        if store.upsert_profile(prof):
            n_prof += 1

    log(
        f"memory rebuild: orders={len(orders)} trades={len(trades)} profiles={n_prof} scope={scope}",
        "INFO",
    )
    return {
        "orders_read": len(orders),
        "trades_written": len(trades),
        "profiles_written": n_prof,
        "ledger_scope": scope,
    }
