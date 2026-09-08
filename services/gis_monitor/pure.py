"""Pure helpers for GIS daily monitor (no network, no mongo).

Recognize: rank Spot USDT by 24h% — no min-price filter; leverage listed+flagged.
Eligible: quote_vol >= 500k AND not leverage (no min price).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.models import OrderStatus

DEFAULT_ELIGIBLE_MIN_VOL = 500_000.0
DEFAULT_LEVERAGE_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")

_STABLES = frozenset(
    {
        "USDT",
        "USDC",
        "USD",
        "DAI",
        "BUSD",
        "FDUSD",
        "TUSD",
        "USDD",
        "USDE",
        "EUR",
        "EURT",
        "PYUSD",
    }
)

GAINER_SOURCE_PREFIXES = ("gainer_",)
GAINER_SOURCE_EXACT = frozenset(
    {
        "gainer_rank_entry",
        "gainer_accel",
        "gainer_live_heat",
        "gainer_continuation",
        "gate_prev_top",
        "gainer_prev",
        "gainer_expand",
        "gainer_sniper",
    }
)


def normalize_symbol(sym: str) -> str:
    s = str(sym or "").strip().upper()
    if not s:
        return ""
    if ":" in s:
        s = s.split(":", 1)[0]
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    return s


def base_of(sym: str) -> str:
    s = normalize_symbol(sym)
    if "/" in s:
        return s.split("/", 1)[0]
    return s


def is_leverage_symbol(
    symbol: str,
    suffixes: tuple[str, ...] | list[str] | None = None,
) -> bool:
    base = base_of(symbol)
    sfx = tuple(suffixes or DEFAULT_LEVERAGE_SUFFIXES)
    return any(base.endswith(x.upper()) for x in sfx)


def is_spot_usdt_base(symbol: str) -> bool:
    """USDT spot base, not stablecoin base. Leverage bases allowed (flagged later)."""
    sym = normalize_symbol(symbol)
    if not sym.endswith("/USDT"):
        return False
    base = base_of(sym)
    if not base or base in _STABLES:
        return False
    return True


def parse_ticker_quote_vol(t: dict[str, Any] | None) -> float:
    if not isinstance(t, dict):
        return 0.0
    qv = t.get("quoteVolume")
    if qv is None:
        last = float(t.get("last") or 0)
        base_vol = float(t.get("baseVolume") or 0)
        qv = last * base_vol if last > 0 else 0.0
    try:
        return float(qv or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_ticker_pct_24h(t: dict[str, Any] | None) -> float:
    if not isinstance(t, dict):
        return 0.0
    pct = t.get("percentage")
    if pct is None:
        info = t.get("info") or {}
        if isinstance(info, dict):
            pct = info.get("change_percentage") or info.get("change") or 0
    try:
        return float(pct or 0)
    except (TypeError, ValueError):
        return 0.0


def is_eligible_leader(
    *,
    quote_vol: float,
    leverage: bool,
    min_vol: float = DEFAULT_ELIGIBLE_MIN_VOL,
) -> tuple[bool, str | None]:
    """Eligible for trade: high volume, no leverage. No min-price rule."""
    if leverage:
        return False, "leverage"
    try:
        vol = float(quote_vol or 0)
    except (TypeError, ValueError):
        vol = 0.0
    if vol < float(min_vol):
        return False, "low_volume"
    return True, None


def is_gainer_source(source: str | None) -> bool:
    s = str(source or "").strip()
    if not s:
        return False
    if s in GAINER_SOURCE_EXACT:
        return True
    return any(s.startswith(p) for p in GAINER_SOURCE_PREFIXES)


def rank_leaders_from_tickers(
    tickers: dict[str, Any],
    *,
    top_n: int = 20,
    min_vol_eligible: float = DEFAULT_ELIGIBLE_MIN_VOL,
    leverage_suffixes: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rank Spot USDT by 24h% — no volume cut, no min price (recognize board)."""
    rows: list[dict[str, Any]] = []
    for raw_sym, t in (tickers or {}).items():
        if not isinstance(t, dict):
            continue
        sym = normalize_symbol(raw_sym)
        if not is_spot_usdt_base(sym):
            continue
        lev = is_leverage_symbol(sym, leverage_suffixes)
        qv = parse_ticker_quote_vol(t)
        pct = parse_ticker_pct_24h(t)
        last = 0.0
        try:
            last = float(t.get("last") or 0)
        except (TypeError, ValueError):
            last = 0.0
        ok, reason = is_eligible_leader(
            quote_vol=qv, leverage=lev, min_vol=min_vol_eligible
        )
        rows.append(
            {
                "symbol": sym,
                "pct_24h": round(pct, 4),
                "quote_vol": round(qv, 2),
                "last": last,
                "leverage": lev,
                "eligible": ok,
                "reject_reason": reason,
            }
        )
    rows.sort(key=lambda r: (float(r["pct_24h"]), float(r["quote_vol"])), reverse=True)
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows[: max(1, int(top_n))], 1):
        row = dict(r)
        row["rank"] = i
        out.append(row)
    return out


def _fill_symbol(order: dict[str, Any]) -> str:
    return normalize_symbol(order.get("symbol") or "")


def join_leaders_to_fills(
    leaders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    *,
    recognized_symbols: set[str] | list[str] | None = None,
    missed_rank_max: int = 10,
) -> list[dict[str, Any]]:
    """Join IST leaders to demo fills for one day."""
    rec: set[str] | None
    if recognized_symbols is None:
        rec = None
    else:
        rec = {normalize_symbol(s) for s in recognized_symbols if s}

    by_sym_buys: dict[str, list[dict]] = defaultdict(list)
    by_sym_sells: dict[str, list[dict]] = defaultdict(list)
    for o in fills or []:
        if not isinstance(o, dict):
            continue
        if str(o.get("status") or "").lower() not in (OrderStatus.EXECUTED.value, "closed", ""):
            # allow missing status in synthetic tests if side present
            if o.get("status") is not None and str(o.get("status")).lower() != OrderStatus.EXECUTED.value:
                continue
        sym = _fill_symbol(o)
        if not sym:
            continue
        side = str(o.get("side") or "").lower()
        if side == "buy":
            by_sym_buys[sym].append(o)
        elif side == "sell":
            by_sym_sells[sym].append(o)

    joined: list[dict[str, Any]] = []
    for lead in leaders:
        sym = normalize_symbol(lead.get("symbol") or "")
        buys = by_sym_buys.get(sym) or []
        sells = by_sym_sells.get(sym) or []
        gainer_buys = [b for b in buys if is_gainer_source(b.get("source"))]
        other_buys = [b for b in buys if not is_gainer_source(b.get("source"))]
        bought_gainer = len(gainer_buys) > 0
        bought_other = len(other_buys) > 0
        bought_any = bought_gainer or bought_other
        eligible = bool(lead.get("eligible"))
        rank = int(lead.get("rank") or 999)
        missed = bool(
            eligible and (not bought_any) and rank <= int(missed_rank_max)
        )
        sell_pnl = 0.0
        for s in sells:
            try:
                sell_pnl += float(s.get("pnl") or 0)
            except (TypeError, ValueError):
                pass
        if rec is None:
            recognized = None
        else:
            recognized = sym in rec
        note_parts = []
        if lead.get("reject_reason"):
            note_parts.append(str(lead["reject_reason"]))
        if missed:
            note_parts.append("missed_liquid")
        if bought_gainer:
            note_parts.append("gainer_buy")
        elif bought_other:
            note_parts.append("other_buy")
        joined.append(
            {
                "rank_ist": rank,
                "symbol": sym,
                "pct_24h": lead.get("pct_24h"),
                "quote_vol": lead.get("quote_vol"),
                "leverage": bool(lead.get("leverage")),
                "eligible": eligible,
                "reject_reason": lead.get("reject_reason"),
                "recognized": recognized,
                "bought_gainer": bought_gainer,
                "bought_other": bought_other,
                "missed": missed,
                "buy_count": len(buys),
                "sell_count": len(sells),
                "sell_pnl": round(sell_pnl, 4),
                "note": ",".join(note_parts) if note_parts else "",
            }
        )
    return joined


def compute_kpis(
    leaders: list[dict[str, Any]],
    join_rows: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    *,
    recognized_symbols: set[str] | list[str] | None = None,
    top_k: int = 20,
    missed_rank_max: int = 10,
) -> dict[str, Any]:
    """KPI dict for one monitor day."""
    n_leaders = len(leaders) or 0
    n_eligible = sum(1 for L in leaders if L.get("eligible"))
    eligible_coverage = (n_eligible / n_leaders) if n_leaders else 0.0

    # Recall proxy
    recall = None
    recall_reason = None
    if recognized_symbols is None:
        recall_reason = "no_recognized_snapshot"
    else:
        rec = {normalize_symbol(s) for s in recognized_symbols if s}
        if not rec:
            recall_reason = "recognized_set_empty"
        else:
            ist_syms = {
                normalize_symbol(L.get("symbol") or "")
                for L in leaders
                if L.get("symbol")
            }
            inter = len(ist_syms & rec)
            denom = min(int(top_k), len(ist_syms)) or 1
            # recall among IST top set size
            recall = round(inter / denom, 4)
            recall_reason = "ok"

    missed_liquid = [
        r["symbol"]
        for r in join_rows
        if r.get("missed") and int(r.get("rank_ist") or 999) <= missed_rank_max
    ]

    eligible_top10 = [
        r
        for r in join_rows
        if r.get("eligible") and int(r.get("rank_ist") or 999) <= 10
    ]
    sleeve_hits = sum(
        1 for r in eligible_top10 if r.get("bought_gainer")
    )
    sleeve_hit_rate = (
        round(sleeve_hits / len(eligible_top10), 4) if eligible_top10 else None
    )

    # pnl by source (sells)
    pnl_by_source: dict[str, float] = defaultdict(float)
    buy_by_source: dict[str, int] = defaultdict(int)
    sell_by_exit: dict[str, int] = defaultdict(int)
    n_buy = n_sell = 0
    gainer_sell_pnls: list[float] = []
    for o in fills or []:
        if not isinstance(o, dict):
            continue
        st = str(o.get("status") or "filled").lower()
        if st not in (OrderStatus.EXECUTED.value, "closed"):
            if o.get("status") is not None and st != OrderStatus.EXECUTED.value:
                continue
        side = str(o.get("side") or "").lower()
        src = str(o.get("source") or "unknown")
        if side == "buy":
            n_buy += 1
            buy_by_source[src] += 1
        elif side == "sell":
            n_sell += 1
            exit_src = str(o.get("exit_source") or src or "unknown")
            sell_by_exit[exit_src] += 1
            try:
                pnl = float(o.get("pnl") or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            # attribute sell pnl to exit_source primarily, also source
            pnl_by_source[exit_src] += pnl
            if is_gainer_source(o.get("source")) or is_gainer_source(
                o.get("exit_source")
            ):
                gainer_sell_pnls.append(pnl)

    gainer_expectancy = (
        round(sum(gainer_sell_pnls) / len(gainer_sell_pnls), 4)
        if gainer_sell_pnls
        else None
    )

    return {
        "n_leaders": n_leaders,
        "n_eligible_in_top": n_eligible,
        "eligible_coverage": round(eligible_coverage, 4),
        "recall_proxy": recall,
        "recall_proxy_reason": recall_reason,
        "missed_liquid_leaders": missed_liquid,
        "missed_liquid_count": len(missed_liquid),
        "sleeve_hit_rate_eligible_top10": sleeve_hit_rate,
        "n_buy_fills": n_buy,
        "n_sell_fills": n_sell,
        "buy_count_by_source": dict(sorted(buy_by_source.items(), key=lambda x: -x[1])),
        "sell_count_by_exit_source": dict(
            sorted(sell_by_exit.items(), key=lambda x: -x[1])
        ),
        "pnl_by_source": {
            k: round(v, 4) for k, v in sorted(pnl_by_source.items(), key=lambda x: x[0])
        },
        "gainer_sell_n": len(gainer_sell_pnls),
        "gainer_sell_expectancy": gainer_expectancy,
        "gainer_sell_pnl_sum": round(sum(gainer_sell_pnls), 4) if gainer_sell_pnls else 0.0,
        "eligible_min_vol_usdt": DEFAULT_ELIGIBLE_MIN_VOL,
        "rules": {
            "min_price_filter": False,
            "eligible_min_quote_vol_usdt": DEFAULT_ELIGIBLE_MIN_VOL,
            "leverage_excluded_from_eligible": True,
        },
    }
