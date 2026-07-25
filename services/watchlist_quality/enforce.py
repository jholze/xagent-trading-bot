"""W4: enforce path — tier caps, min buy score, memory hard-exclude new adds.

Pure transforms + gate helpers. Does not write orders or block sells.
"""

from __future__ import annotations

from typing import Any

from services.watchlist_quality.config import watchlist_quality_section, wqe_mode


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _sym(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "").strip()


def _score(row: dict[str, Any]) -> float:
    for k in ("quality_shadow_ai", "quality_score", "wqe_score"):
        if row.get(k) is not None:
            return _f(row.get(k))
    return 0.0


def regime_caps(config: dict | None = None, *, regime: str | None = None) -> dict[str, int]:
    """Tier caps by regime label (risk-on / neutral / risk-off)."""
    sec = watchlist_quality_section(config)
    table = sec.get("regime_caps") if isinstance(sec.get("regime_caps"), dict) else {}
    defaults = {
        "risk-on": {"T1": 12, "T2": 8, "T3": 2},
        "neutral": {"T1": 10, "T2": 4, "T3": 0},
        "risk-off": {"T1": 8, "T2": 2, "T3": 0},
        "block": {"T1": 8, "T2": 0, "T3": 0},
    }
    key = (regime or "neutral").strip().lower().replace("_", "-")
    if key in ("riskon", "risk on", "on"):
        key = "risk-on"
    if key in ("riskoff", "risk off", "off"):
        key = "risk-off"
    base = dict(defaults.get(key) or defaults["neutral"])
    override = table.get(key) if isinstance(table.get(key), dict) else {}
    for t in ("T1", "T2", "T3"):
        if t in override:
            try:
                base[t] = int(override[t])
            except (TypeError, ValueError):
                pass
    return base


def min_buy_score(config: dict | None = None) -> float:
    from services.watchlist_quality.config import min_buy_score as _mbs

    return _mbs(config)


def assign_tier(row: dict[str, Any], *, config: dict | None = None) -> str:
    """POS | T1 | T2 | T3 from score + flags (enforce)."""
    if row.get("is_open") or row.get("_wqe_is_open"):
        return "POS"
    tier = str(row.get("tier_hint") or row.get("tier") or "").upper()
    if tier in ("T1", "T2", "T3", "POS"):
        # re-check T3 off
        pass
    else:
        q = _score(row)
        if q >= 0.65 and "vol_low" not in (row.get("flags") or []):
            tier = "T1"
        elif q >= 0.40:
            tier = "T2"
        else:
            tier = "T3"
    # memory hard exclude new → demote to T3 unless open
    if row.get("hard_exclude_new_add") or "memory_hard_exclude_new" in (row.get("flags") or []):
        if not (row.get("is_open") or row.get("_wqe_is_open")):
            if str(row.get("source") or "").lower() in (
                "cmc_trending",
                "trending",
                "dry_run_overlay",
                "cmc",
            ) or row.get("is_new_add"):
                tier = "T3"
    return tier


def apply_enforce_tiers(
    coins: list[dict[str, Any]],
    *,
    open_symbols: set[str] | list[str] | None = None,
    config: dict | None = None,
    regime: str | None = None,
    drop_t3: bool | None = None,
) -> list[dict[str, Any]]:
    """Cap T1/T2/T3 by regime; POS unlimited; sort POS → T1 → T2 → T3 by score."""
    open_set = {str(s).strip() for s in (open_symbols or []) if s}
    caps = regime_caps(config, regime=regime)
    sec = watchlist_quality_section(config)
    if drop_t3 is None:
        drop_t3 = bool(sec.get("drop_t3", True))

    buckets: dict[str, list[dict[str, Any]]] = {"POS": [], "T1": [], "T2": [], "T3": []}
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        row = dict(c)
        sym = _sym(row)
        if not sym:
            continue
        if sym in open_set:
            row["is_open"] = True
        tier = assign_tier(row, config=config)
        row["tier"] = tier
        row["quality_score"] = _score(row)
        buckets.setdefault(tier, []).append(row)

    for t in ("T1", "T2", "T3"):
        buckets[t].sort(key=lambda r: -_score(r))
        cap = int(caps.get(t, 0))
        if cap >= 0:
            buckets[t] = buckets[t][:cap]

    if drop_t3:
        buckets["T3"] = []

    # POS first (stable by symbol), then T1, T2, T3 by score
    buckets["POS"].sort(key=lambda r: _sym(r))
    out: list[dict[str, Any]] = []
    for t in ("POS", "T1", "T2", "T3"):
        out.extend(buckets.get(t) or [])
    return out


def buy_allowed(
    symbol: str,
    *,
    scored_row: dict[str, Any] | None = None,
    config: dict | None = None,
    source: str | None = None,
    is_new_add: bool = False,
    has_open_position: bool = False,
) -> tuple[bool, str]:
    """W4 buy gate for TA/CMC paths. Sells not in scope (always call only on buys)."""
    mode = wqe_mode(config)
    if mode not in ("soft", "enforce"):
        return True, "wqe_off"

    if has_open_position:
        return True, "open_position"

    row = dict(scored_row or {})
    row.setdefault("symbol", symbol)
    if source:
        row.setdefault("source", source)
    if is_new_add:
        row["is_new_add"] = True

    # Memory hard exclude new trending
    hard = bool(
        row.get("hard_exclude_new_add")
        or "memory_hard_exclude_new" in (row.get("flags") or [])
        or (row.get("memory") or {}).get("hard_exclude_new_add")
    )
    src = str(row.get("source") or source or "").lower()
    trending_src = any(
        x in src for x in ("cmc_trending", "trending", "dry_run", "cmc")
    )
    if mode == "enforce" and hard and (is_new_add or trending_src) and not has_open_position:
        return False, "memory_hard_exclude_new"

    if mode == "enforce":
        tier = assign_tier(row, config=config)
        if tier == "T3":
            return False, "tier_t3"
        if tier not in ("T1", "T2", "POS"):
            return False, f"tier_{tier}"

    q = _score(row)
    floor = min_buy_score(config)
    if q < floor and mode == "enforce":
        return False, f"min_buy_score:{q:.3f}<{floor:.3f}"

    # soft: only warn via reason still allowed if above soft floor was applied upstream
    return True, "ok"


def filter_new_adds_memory(
    coins: list[dict[str, Any]],
    *,
    base_symbols: set[str] | list[str] | None = None,
    open_symbols: set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Drop new trending adds with hard_exclude_new_add (enforce helper)."""
    base = {str(s).strip() for s in (base_symbols or []) if s}
    open_set = {str(s).strip() for s in (open_symbols or []) if s}
    out = []
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        sym = _sym(c)
        if sym in open_set or sym in base:
            out.append(c)
            continue
        hard = bool(
            c.get("hard_exclude_new_add")
            or "memory_hard_exclude_new" in (c.get("flags") or [])
            or (c.get("memory") or {}).get("hard_exclude_new_add")
        )
        if hard:
            continue
        out.append(c)
    return out
