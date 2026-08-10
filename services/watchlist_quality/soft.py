"""W3: soft watchlist transform — vol floor + score sort + open positions keep.

Pure functions: no network, no global watchlist mutation.
"""

from __future__ import annotations

from typing import Any


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _sym(coin: dict[str, Any]) -> str:
    return str(coin.get("symbol") or coin.get("pair") or "").strip()


def _quote_vol(coin: dict[str, Any]) -> float | None:
    for k in (
        "quote_vol_24h",
        "volume_24h",
        "quote_volume_24h_usdt",
        "quote_volume_24h",
    ):
        if coin.get(k) is not None:
            return _f(coin.get(k))
    return None


def _quality(coin: dict[str, Any]) -> float:
    """Prefer AI-fused shadow score when present."""
    for k in ("quality_shadow_ai", "quality_score", "wqe_score"):
        if coin.get(k) is not None:
            return _f(coin.get(k))
    return 0.0


def apply_soft_watchlist(
    coins: list[dict[str, Any]],
    *,
    open_symbols: set[str] | frozenset[str] | list[str] | None = None,
    min_quote_vol_usd: float = 750_000.0,
    use_ai_score: bool = True,
) -> list[dict[str, Any]]:
    """Filter + sort for soft mode.

    Rules:
    - Open positions always kept (even under vol floor)
    - Non-open under vol floor dropped
    - Remaining sorted: open first, then quality desc
    - Vol unknown (None) kept but sorted last among non-open (fail-open keep)
    """
    open_set = {str(s).strip() for s in (open_symbols or []) if s}
    floor = float(min_quote_vol_usd or 0.0)

    kept: list[dict[str, Any]] = []
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        sym = _sym(c)
        if not sym:
            continue
        is_open = sym in open_set
        vol = _quote_vol(c)
        if not is_open and vol is not None and vol < floor:
            continue
        row = dict(c)
        row["_wqe_is_open"] = is_open
        row["_wqe_vol"] = vol
        if use_ai_score:
            q = _quality(row)
        else:
            q = _f(row.get("quality_score"), 0.0)
        row["_wqe_sort_score"] = q
        kept.append(row)

    # positions first, then score desc, symbol for stability
    kept.sort(
        key=lambda r: (
            0 if r.get("_wqe_is_open") else 1,
            -float(r.get("_wqe_sort_score") or 0.0),
            _sym(r),
        )
    )
    # strip private keys for consumers that want clean dicts — keep optional
    out: list[dict[str, Any]] = []
    for r in kept:
        clean = {k: v for k, v in r.items() if not str(k).startswith("_wqe_")}
        clean["wqe_soft_kept"] = True
        clean["quality_score"] = r.get("quality_score", r.get("_wqe_sort_score"))
        if r.get("quality_shadow_ai") is not None:
            clean["quality_shadow_ai"] = r.get("quality_shadow_ai")
        out.append(clean)
    return out


def soft_scan_order(
    scored_coins: list[dict[str, Any]],
    *,
    open_symbols: set[str] | list[str] | None = None,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Config-aware soft transform (reads vol floor from watchlist_quality)."""
    from services.watchlist_quality.config import (
        use_ai_sort_score,
        vol_floor_t1_usd,
        wqe_mode,
    )

    mode = wqe_mode(config)
    if mode not in ("soft", "enforce"):
        # no membership change — return as-is sorted by det score only for convenience
        rows = [dict(c) for c in (scored_coins or []) if isinstance(c, dict)]
        rows.sort(key=lambda r: -_quality(r))
        return rows

    floor = vol_floor_t1_usd(config)
    # AI5: use_ai_score True prefers quality_shadow_ai when present on rows
    use_ai_score = use_ai_sort_score(config)
    return apply_soft_watchlist(
        scored_coins,
        open_symbols=open_symbols,
        min_quote_vol_usd=floor,
        use_ai_score=use_ai_score,
    )
