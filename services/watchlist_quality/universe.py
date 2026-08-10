"""W5: Sensor + CMC-only universe helpers aligned to WQE tiers/scores."""

from __future__ import annotations

from typing import Any

from services.watchlist_quality.config import min_buy_score, wqe_mode
from services.watchlist_quality.enforce import buy_allowed
from services.watchlist_quality.store import load_quality_scores


def _score_map_from_store() -> dict[str, dict[str, Any]]:
    data = load_quality_scores()
    out: dict[str, dict[str, Any]] = {}
    for c in data.get("coins") or []:
        if isinstance(c, dict) and c.get("symbol"):
            out[str(c["symbol"])] = c
    return out


def get_sensor_watch_coins(
    config: dict | None = None,
    *,
    candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """R2: single entry for entry_sensor_loop + eval — always includes open positions."""
    mode = wqe_mode(config)
    if candidates is None:
        try:
            from data_manager import load_effective_watchlist

            candidates = load_effective_watchlist()
        except Exception:
            candidates = []
    if mode not in ("soft", "enforce"):
        return [c for c in (candidates or []) if isinstance(c, dict) and c.get("active", True)]

    uni = sensor_universe(candidates, config=config)
    # ensure open positions monitored even if filtered
    try:
        from strategies.positions import get_open_positions

        have = {str(c.get("symbol")) for c in uni if c.get("symbol")}
        for p in get_open_positions() or []:
            sym = p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)
            if not sym or str(sym) in have:
                continue
            uni.append({"symbol": str(sym), "active": True, "is_open": True, "tier": "POS"})
            have.add(str(sym))
    except Exception:
        pass
    return uni


def sensor_universe(
    candidates: list[dict[str, Any]] | None = None,
    *,
    config: dict | None = None,
    min_t2_score: float | None = None,
    allow_t3: bool = False,
) -> list[dict[str, Any]]:
    """Universe for entry_sensor: T1 ∪ (T2 if score ≥ threshold). Fail-open if WQE off."""
    mode = wqe_mode(config)
    if mode not in ("soft", "enforce"):
        return list(candidates or [])

    scores = _score_map_from_store()
    if candidates is None:
        try:
            from data_manager import load_effective_watchlist

            candidates = load_effective_watchlist()
        except Exception:
            candidates = []

    thr = min_t2_score
    if thr is None:
        thr = min_buy_score(config)

    out: list[dict[str, Any]] = []
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "").strip()
        if not sym:
            continue
        sc = scores.get(sym) or {}
        tier = str(sc.get("tier") or sc.get("tier_hint") or c.get("tier") or c.get("tier_hint") or "")
        q = sc.get("quality_shadow_ai")
        if q is None:
            q = sc.get("quality_score")
        try:
            qf = float(q) if q is not None else None
        except (TypeError, ValueError):
            qf = None

        if tier == "T1" or (not tier and qf is not None and qf >= 0.65):
            row = dict(c)
            row["tier"] = tier or "T1"
            if qf is not None:
                row["quality_score"] = qf
            out.append(row)
            continue
        if tier == "T2" or (not tier and qf is not None and qf >= thr):
            if qf is None or qf >= thr:
                row = dict(c)
                row["tier"] = tier or "T2"
                if qf is not None:
                    row["quality_score"] = qf
                out.append(row)
            continue
        if allow_t3 and tier == "T3":
            out.append(dict(c))
            continue
        # No score file yet — fail-open keep candidate in soft only if mode soft without scores
        if not scores and mode in ("shadow", "soft"):
            out.append(dict(c))
    return out


def cmc_only_buy_allowed(
    symbol: str,
    *,
    trending_rank: int | None = None,
    config: dict | None = None,
    source: str = "cmc_trending",
) -> tuple[bool, str]:
    """Gate CMC-only buys by WQE score/tier when mode soft/enforce."""
    mode = wqe_mode(config)
    if mode not in ("soft", "enforce"):
        return True, "wqe_off"

    scores = _score_map_from_store()
    row = dict(scores.get(symbol) or {"symbol": symbol, "source": source})
    row["source"] = source
    if trending_rank is not None:
        row["cmc_rank"] = trending_rank
        # Prefer score order over raw rank when scores exist
        if symbol in scores:
            # enforce: must pass buy_allowed
            return buy_allowed(
                symbol,
                scored_row=row,
                config=config,
                source=source,
                is_new_add=True,
            )
    return buy_allowed(
        symbol,
        scored_row=row,
        config=config,
        source=source,
        is_new_add=True,
    )


def rank_cmc_candidates_by_wqe(
    symbols: list[str],
    *,
    config: dict | None = None,
) -> list[str]:
    """Reorder CMC candidate symbols by quality_shadow_ai / quality_score desc."""
    scores = _score_map_from_store()
    if not scores or wqe_mode(config) == "off":
        return list(symbols)

    def key(sym: str) -> tuple:
        sc = scores.get(sym) or {}
        q = sc.get("quality_shadow_ai")
        if q is None:
            q = sc.get("quality_score")
        try:
            qf = float(q) if q is not None else -1.0
        except (TypeError, ValueError):
            qf = -1.0
        return (-qf, sym)

    return sorted(symbols, key=key)
