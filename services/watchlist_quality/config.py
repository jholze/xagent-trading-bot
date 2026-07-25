"""watchlist_quality config helpers (Epic #124)."""

from __future__ import annotations

from typing import Any

_DEFAULT_WEIGHTS = {
    "liquidity": 0.35,
    "momentum": 0.20,
    "narrative": 0.15,
    "memory": 0.15,
    "regime_fit": 0.15,
}

_VALID_MODES = frozenset({"off", "shadow", "soft", "enforce"})


def watchlist_quality_section(config: dict | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    raw = config.get("watchlist_quality")
    return raw if isinstance(raw, dict) else {}


def wqe_mode(config: dict | None = None) -> str:
    """off | shadow | soft | enforce — default off until explicitly enabled."""
    sec = watchlist_quality_section(config)
    mode = str(sec.get("mode") or "off").strip().lower()
    if mode not in _VALID_MODES:
        return "off"
    return mode


def wqe_shadow_active(config: dict | None = None) -> bool:
    """Shadow+ modes compute scores; only soft/enforce change membership later."""
    return wqe_mode(config) in ("shadow", "soft", "enforce")


def score_weights(config: dict | None = None) -> dict[str, float]:
    sec = watchlist_quality_section(config)
    w = sec.get("weights") if isinstance(sec.get("weights"), dict) else {}
    out = {**_DEFAULT_WEIGHTS}
    for k in out:
        if k in w:
            try:
                out[k] = float(w[k])
            except (TypeError, ValueError):
                pass
    total = sum(out.values()) or 1.0
    # Normalize so weights sum to 1
    return {k: v / total for k, v in out.items()}


def vol_floor_t1_usd(config: dict | None = None) -> float:
    sec = watchlist_quality_section(config)
    floors = sec.get("vol_floors") if isinstance(sec.get("vol_floors"), dict) else {}
    try:
        return float(floors.get("t1_min_quote_vol_usd", 750_000) or 750_000)
    except (TypeError, ValueError):
        return 750_000.0
