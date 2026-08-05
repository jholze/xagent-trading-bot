"""Pure gainer leaders board + entry signal selection (no I/O).

Recognize: Top-N Spot USDT by 24h% — no vol cut, no min price; leverage flagged.
Eligible: quote_vol >= 500k and not leverage.
"""

from __future__ import annotations

from typing import Any

DEFAULT_ELIGIBLE_MIN_VOL = 500_000.0
DEFAULT_RECOGNIZE_TOP_N = 100
DEFAULT_LEVERAGE_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
DEFAULT_HEAT_MIN = 12.0
DEFAULT_HEAT_MAX = 80.0
DEFAULT_SIGNAL_MAX_RANK = 20

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

GAINER_SOURCES = frozenset(
    {
        "gainer_rank_entry",
        "gainer_accel",
        "gainer_live_heat",
        "gainer_heat",
        "gainer_continuation",
        "gate_prev_top",
        "gainer_sniper",
        "gainer_signal",
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


def is_leverage_symbol(symbol: str, suffixes: tuple[str, ...] | list[str] | None = None) -> bool:
    base = base_of(symbol)
    sfx = tuple(suffixes or DEFAULT_LEVERAGE_SUFFIXES)
    return any(base.endswith(x.upper()) for x in sfx)


def is_spot_usdt_base(symbol: str) -> bool:
    sym = normalize_symbol(symbol)
    if not sym.endswith("/USDT"):
        return False
    base = base_of(sym)
    return bool(base) and base not in _STABLES


def parse_quote_vol(t: dict[str, Any] | None) -> float:
    if not isinstance(t, dict):
        return 0.0
    qv = t.get("quoteVolume")
    if qv is None:
        try:
            last = float(t.get("last") or 0)
            bv = float(t.get("baseVolume") or 0)
            qv = last * bv if last > 0 else 0.0
        except (TypeError, ValueError):
            qv = 0.0
    try:
        return float(qv or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_pct_24h(t: dict[str, Any] | None) -> float:
    if not isinstance(t, dict):
        return 0.0
    pct = t.get("percentage")
    if pct is None:
        info = t.get("info") if isinstance(t.get("info"), dict) else {}
        pct = (info or {}).get("change_percentage") or (info or {}).get("change") or 0
    try:
        return float(pct or 0)
    except (TypeError, ValueError):
        return 0.0


def is_eligible(
    *,
    quote_vol: float,
    leverage: bool,
    min_vol: float = DEFAULT_ELIGIBLE_MIN_VOL,
) -> tuple[bool, str | None]:
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
    if s in GAINER_SOURCES:
        return True
    return s.startswith("gainer_")


def rank_leaders_from_tickers(
    tickers: dict[str, Any],
    *,
    top_n: int = DEFAULT_RECOGNIZE_TOP_N,
    min_vol_eligible: float = DEFAULT_ELIGIBLE_MIN_VOL,
) -> list[dict[str, Any]]:
    """Recognize board: no vol cut, no min price."""
    rows: list[dict[str, Any]] = []
    for raw, t in (tickers or {}).items():
        if not isinstance(t, dict):
            continue
        sym = normalize_symbol(raw)
        if not is_spot_usdt_base(sym):
            continue
        lev = is_leverage_symbol(sym)
        qv = parse_quote_vol(t)
        pct = parse_pct_24h(t)
        try:
            last = float(t.get("last") or 0)
        except (TypeError, ValueError):
            last = 0.0
        ok, reason = is_eligible(quote_vol=qv, leverage=lev, min_vol=min_vol_eligible)
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
    out = []
    for i, r in enumerate(rows[: max(1, int(top_n))], 1):
        row = dict(r)
        row["rank"] = i
        out.append(row)
    return out


def select_entry_signals(
    leaders: list[dict[str, Any]],
    *,
    heat_min: float = DEFAULT_HEAT_MIN,
    heat_max: float = DEFAULT_HEAT_MAX,
    max_rank: int = DEFAULT_SIGNAL_MAX_RANK,
    prev_board: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Pick buy candidates from eligible leaders (simple heat + sticky improve).

    - heat: eligible, rank<=max_rank, heat_min <= pct <= heat_max
    - sticky: was already on prev board in top max_rank and still eligible (rank stable/improved)
    """
    prev = prev_board or {}
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in leaders:
        if not row.get("eligible"):
            continue
        rank = int(row.get("rank") or 999)
        if rank > int(max_rank):
            continue
        pct = float(row.get("pct_24h") or 0)
        sym = str(row.get("symbol") or "")
        if not sym or sym in seen:
            continue
        trigger = None
        if heat_min <= pct <= heat_max:
            trigger = "heat"
        prev_row = prev.get(sym)
        if prev_row and int(prev_row.get("rank") or 999) <= max_rank:
            if rank <= int(prev_row.get("rank") or 999):
                trigger = "t1_sticky"
        if not trigger:
            continue
        seen.add(sym)
        source = "gainer_live_heat" if trigger == "heat" else "gainer_rank_entry"
        signals.append(
            {
                "symbol": sym,
                "trigger": trigger,
                "rank": rank,
                "pct_24h": pct,
                "quote_vol": float(row.get("quote_vol") or 0),
                "last": float(row.get("last") or 0),
                "eligible": True,
                "source": source,
            }
        )
    return signals


def clamp_usdt_to_vol(
    usdt: float,
    quote_vol: float,
    *,
    max_pct_of_vol: float = 2.0,
) -> float:
    """Cap ticket size to max_pct of 24h quote volume."""
    try:
        u = float(usdt or 0)
        v = float(quote_vol or 0)
        pct = float(max_pct_of_vol or 2.0)
    except (TypeError, ValueError):
        return 0.0
    if u <= 0:
        return 0.0
    if v <= 0:
        return u
    cap = v * (pct / 100.0)
    return float(min(u, cap)) if cap > 0 else u


def check_gainer_entry_caps(
    *,
    open_gainer_count: int,
    gainer_buys_today: int,
    max_open: int = 3,
    max_buys_per_day: int = 6,
) -> tuple[bool, str]:
    """Return (ok, reason)."""
    if int(open_gainer_count) >= int(max_open):
        return False, "max_open_gainer"
    if int(gainer_buys_today) >= int(max_buys_per_day):
        return False, "max_buys_per_day"
    return True, ""


def count_open_gainer_positions(positions: list[dict[str, Any]] | None) -> int:
    n = 0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        src = (
            p.get("entry_source")
            or p.get("source")
            or (p.get("position") or {}).get("entry_source")
            or ""
        )
        if is_gainer_source(str(src)):
            n += 1
            continue
        # also check strategy tag
        strat = str(p.get("strategy") or "")
        if strat.startswith("gainer"):
            n += 1
    return n
