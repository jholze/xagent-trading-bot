"""Pure gainer leaders board + entry signal selection (no I/O).

Recognize: Top-N Spot USDT by 24h% — no vol cut, no min price; leverage flagged.
Eligible: quote_vol >= 500k and not leverage.
"""

from __future__ import annotations

from typing import Any

DEFAULT_ELIGIBLE_MIN_VOL = 500_000.0
DEFAULT_RECOGNIZE_TOP_N = 100
DEFAULT_LEVERAGE_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
# fixed_v0 global band (GIS-14-style safety rollback)
DEFAULT_HEAT_MIN = 12.0
DEFAULT_HEAT_MAX = 40.0
DEFAULT_SIGNAL_MAX_RANK = 20
DEFAULT_ENTRY_POLICY = "fixed_v0"  # flip to coin_aware_v1 after smoke
DEFAULT_HARD_CEILING = 50.0
DEFAULT_FIRST_SEEN_TOP_K_MAX_MIN = 15.0
# ATR% (1h, 14) → vol bucket
DEFAULT_ATR_LOW_LT = 3.0
DEFAULT_ATR_HIGH_GT = 6.0
# pct_24h bands per bucket (hypotheses)
BUCKET_BANDS: dict[str, tuple[float, float]] = {
    "low": (8.0, 20.0),
    "mid": (10.0, 35.0),
    "high": (12.0, 45.0),
}

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


def vol_bucket_from_atr_pct(
    atr_pct: float | None,
    *,
    low_lt: float = DEFAULT_ATR_LOW_LT,
    high_gt: float = DEFAULT_ATR_HIGH_GT,
) -> str | None:
    """Map ATR% to low|mid|high. None if ATR missing (coin_aware: no entry)."""
    if atr_pct is None:
        return None
    try:
        a = float(atr_pct)
    except (TypeError, ValueError):
        return None
    if a != a:  # NaN
        return None
    if a < float(low_lt):
        return "low"
    if a > float(high_gt):
        return "high"
    return "mid"


def band_for_bucket(
    bucket: str | None,
    *,
    bands: dict[str, tuple[float, float]] | None = None,
) -> tuple[float, float] | None:
    table = bands or BUCKET_BANDS
    if not bucket:
        return None
    b = table.get(str(bucket))
    if not b or len(b) != 2:
        return None
    return float(b[0]), float(b[1])


def rank_quality_ok(
    *,
    rank: int,
    max_rank: int,
    prev_rank: int | None,
    scans_in_top_k: int,
    first_seen_age_min: float | None,
    first_seen_max_min: float = DEFAULT_FIRST_SEEN_TOP_K_MAX_MIN,
    require_fresh_for_high: bool = False,
    vol_bucket: str | None = None,
) -> tuple[bool, str]:
    """Band-quality: improved rank, fresh to top-k, or dwell scans>=2.

    high-bucket prefers fresh/improved (still allows scans>=2 if not require_fresh).
    """
    improved = prev_rank is not None and int(rank) < int(prev_rank)
    fresh = (
        first_seen_age_min is not None
        and float(first_seen_age_min) <= float(first_seen_max_min)
    )
    dwell = int(scans_in_top_k or 0) >= 2
    if require_fresh_for_high and vol_bucket == "high":
        if improved:
            return True, "rank_improved"
        if fresh:
            return True, "rank_new"
        return False, "high_needs_fresh_or_improve"
    if improved:
        return True, "rank_improved"
    if fresh:
        return True, "rank_new"
    if dwell:
        return True, "t1_sticky"
    return False, "rank_quality_weak"


def select_entry_signals(
    leaders: list[dict[str, Any]],
    *,
    heat_min: float = DEFAULT_HEAT_MIN,
    heat_max: float = DEFAULT_HEAT_MAX,
    max_rank: int = DEFAULT_SIGNAL_MAX_RANK,
    prev_board: dict[str, dict[str, Any]] | None = None,
    entry_policy: str = DEFAULT_ENTRY_POLICY,
    hard_ceiling: float = DEFAULT_HARD_CEILING,
    atr_by_symbol: dict[str, float] | None = None,
    symbol_state: dict[str, dict[str, Any]] | None = None,
    bands: dict[str, tuple[float, float]] | None = None,
    first_seen_max_min: float = DEFAULT_FIRST_SEEN_TOP_K_MAX_MIN,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Pick buy candidates from eligible leaders.

    Policies:
    - fixed_v0: global [heat_min, heat_max] (rollback / default until coin_aware smoke)
    - coin_aware_v1: vol-bucket band AND rank quality AND hard_ceiling;
      missing ATR → no entry (board still shows the coin)
    """
    policy = (entry_policy or DEFAULT_ENTRY_POLICY).strip().lower()
    if policy in ("fixed", "v0", "legacy"):
        policy = "fixed_v0"
    if policy in ("coin_aware", "v1", "bucket"):
        policy = "coin_aware_v1"

    prev = prev_board or {}
    atr_map = atr_by_symbol or {}
    state = symbol_state or {}
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    import time as _time

    now = float(now_ts if now_ts is not None else _time.time())

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

        st = state.get(sym) or {}
        scans = int(st.get("scans_in_top_k") or row.get("scans_in_top_k") or 0)
        first_seen = st.get("first_seen_top_k_at") or row.get("first_seen_top_k_at")
        try:
            first_seen_f = float(first_seen) if first_seen is not None else None
        except (TypeError, ValueError):
            first_seen_f = None
        age_min = (
            (now - first_seen_f) / 60.0 if first_seen_f is not None else None
        )
        prev_row = prev.get(sym) or {}
        try:
            prev_rank = int(prev_row.get("rank")) if prev_row.get("rank") is not None else None
        except (TypeError, ValueError):
            prev_rank = None
        if prev_rank is None and st.get("prev_rank") is not None:
            try:
                prev_rank = int(st.get("prev_rank"))
            except (TypeError, ValueError):
                prev_rank = None

        atr_pct = None
        if sym in atr_map:
            try:
                atr_pct = float(atr_map[sym])
            except (TypeError, ValueError):
                atr_pct = None
        elif row.get("atr_pct") is not None:
            try:
                atr_pct = float(row.get("atr_pct"))
            except (TypeError, ValueError):
                atr_pct = None

        vol_bucket: str | None = None
        band_lo: float
        band_hi: float
        extension_score = pct

        if policy == "coin_aware_v1":
            if pct > float(hard_ceiling):
                continue
            vol_bucket = vol_bucket_from_atr_pct(atr_pct)
            if vol_bucket is None:
                continue  # ATR miss → no entry
            band = band_for_bucket(vol_bucket, bands=bands)
            if not band:
                continue
            band_lo, band_hi = band
            if pct < band_lo or pct > band_hi:
                continue
            ok_q, q_reason = rank_quality_ok(
                rank=rank,
                max_rank=max_rank,
                prev_rank=prev_rank,
                scans_in_top_k=scans,
                first_seen_age_min=age_min,
                first_seen_max_min=first_seen_max_min,
                require_fresh_for_high=True,
                vol_bucket=vol_bucket,
            )
            if not ok_q:
                continue
            trigger = q_reason if q_reason in ("rank_improved", "rank_new", "t1_sticky") else "heat"
            if trigger == "rank_improved":
                trigger = "t1_sticky"
            if trigger not in ("t1_sticky", "rank_new"):
                trigger = "heat"
        else:
            # fixed_v0
            if pct < float(heat_min) or pct > float(heat_max):
                continue
            trigger = "heat"
            if prev_rank is not None and rank <= prev_rank:
                trigger = "t1_sticky"
            band_lo, band_hi = float(heat_min), float(heat_max)
            vol_bucket = vol_bucket_from_atr_pct(atr_pct)

        seen.add(sym)
        source = "gainer_live_heat" if trigger == "heat" else "gainer_rank_entry"
        if trigger == "rank_new":
            source = "gainer_rank_entry"
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
                "entry_policy": policy,
                "vol_bucket": vol_bucket,
                "atr_pct": atr_pct,
                "extension_score": extension_score,
                "band_lo": band_lo,
                "band_hi": band_hi,
                "scans_in_top_k": scans,
                "rank_improved": bool(
                    prev_rank is not None and rank < int(prev_rank)
                ),
                "hard_ceiling": float(hard_ceiling) if policy == "coin_aware_v1" else float(heat_max),
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
