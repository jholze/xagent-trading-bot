"""Transparent multi-factor quality score (W2). Pure / network-free given inputs."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from services.watchlist_quality.config import score_weights, vol_floor_t1_usd
from services.watchlist_quality.memory_bias import MemoryWqeInput, get_memory_wqe_input


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def score_liquidity(
    *,
    quote_vol_24h: float | None,
    spread_pct: float | None = None,
    vol_ref_usd: float = 750_000.0,
) -> float:
    """log-scaled volume vs reference; mild spread penalty when known."""
    vol = max(0.0, _f(quote_vol_24h, 0.0))
    ref = max(1.0, float(vol_ref_usd or 750_000.0))
    # log10(vol+1) / log10(ref*10) → ~1.0 when vol ≈ 10x ref
    if vol <= 0:
        liq = 0.0
    else:
        liq = math.log10(vol + 1.0) / math.log10(ref * 10.0)
        liq = _clamp(liq)
    sp = spread_pct
    if sp is not None and sp > 0:
        # 0% → no pen, 1.5% → ~0.15 pen, 3%+ → heavy
        pen = _clamp(_f(sp) / 10.0, 0.0, 0.5)
        liq = _clamp(liq * (1.0 - pen))
    return liq


def score_momentum(*, change_24h_pct: float | None, change_7d_pct: float | None = None) -> float:
    """Clamp absolute moves into a mid-high band; extreme pumps slightly down."""
    c24 = _f(change_24h_pct, 0.0)
    c7 = _f(change_7d_pct, c24)
    # Prefer moderate positive structure over dead or parabolic
    def _band(x: float) -> float:
        ax = abs(x)
        if ax < 1:
            return 0.35  # flat
        if ax < 5:
            return 0.55
        if ax < 15:
            return 0.75
        if ax < 40:
            return 0.65  # hot but ok
        return 0.40  # parabolic / crashy

    return _clamp(0.6 * _band(c24) + 0.4 * _band(c7))


def score_narrative(
    *,
    cmc_rank: int | float | None = None,
    source: str | None = None,
    max_rank: int = 50,
) -> float:
    """Inverse rank + light source weight."""
    src = (source or "").lower()
    base = 0.45
    if "trending" in src:
        base = 0.55
    elif "gainer" in src:
        base = 0.50
    elif "listing" in src:
        base = 0.40
    elif src in ("base", "watchlist", ""):
        base = 0.50

    rank = cmc_rank
    if rank is not None:
        try:
            r = float(rank)
            if r > 0:
                # rank 1 → ~1.0, rank max_rank → ~0.2
                inv = 1.0 - (min(r, float(max_rank)) - 1.0) / float(max_rank)
                return _clamp(0.4 * base + 0.6 * inv)
        except (TypeError, ValueError):
            pass
    return _clamp(base)


def score_regime_fit(*, size_mult: float | None = None, sensor_policy: str | None = None) -> float:
    """Map fusion-ish size_mult / sensor policy to 0..1 (neutral default 0.5)."""
    sm = size_mult
    if sm is None:
        pol = (sensor_policy or "").lower()
        if pol in ("block", "risk_off", "off"):
            return 0.25
        if pol in ("throttle", "caution"):
            return 0.40
        if pol in ("allow", "risk_on", "on"):
            return 0.75
        return 0.5
    try:
        m = float(sm)
    except (TypeError, ValueError):
        return 0.5
    # size_mult 0 → 0.15, 0.5 → 0.4, 1.0 → 0.7, 1.5+ → 0.9
    return _clamp(0.15 + 0.5 * m)


@dataclass
class CoinQualityScore:
    symbol: str
    quality_score: float
    scores: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    tier_hint: str = "T2"  # shadow hint only — not enforced in W2
    memory: dict[str, Any] = field(default_factory=dict)
    quality_shadow_ai: float | None = None
    ai: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def score_coin(
    symbol: str,
    *,
    config: dict | None = None,
    quote_vol_24h: float | None = None,
    spread_pct: float | None = None,
    change_24h_pct: float | None = None,
    change_7d_pct: float | None = None,
    cmc_rank: int | float | None = None,
    source: str | None = None,
    mcap_usd: float | None = None,
    regime_size_mult: float | None = None,
    sensor_policy: str | None = None,
    memory: MemoryWqeInput | None = None,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
) -> CoinQualityScore:
    """Compute quality_score ∈ [0,1] — pure given metrics + optional memory DTO."""
    sym = (symbol or "").strip()
    weights = score_weights(config)
    vol_ref = vol_floor_t1_usd(config)

    if memory is None:
        memory = get_memory_wqe_input(
            sym, config=config, ledger_scope=ledger_scope, tenant_id=tenant_id
        )

    s_liq = score_liquidity(
        quote_vol_24h=quote_vol_24h, spread_pct=spread_pct, vol_ref_usd=vol_ref
    )
    s_mom = score_momentum(change_24h_pct=change_24h_pct, change_7d_pct=change_7d_pct)
    s_nar = score_narrative(cmc_rank=cmc_rank, source=source)
    s_mem = _clamp(float(memory.memory_score))
    s_reg = score_regime_fit(size_mult=regime_size_mult, sensor_policy=sensor_policy)

    components = {
        "liquidity": round(s_liq, 4),
        "momentum": round(s_mom, 4),
        "narrative": round(s_nar, 4),
        "memory": round(s_mem, 4),
        "regime_fit": round(s_reg, 4),
    }
    quality = sum(weights[k] * components[k] for k in components)
    quality = _clamp(quality)

    flags: list[str] = []
    if quote_vol_24h is not None and _f(quote_vol_24h) >= vol_ref:
        flags.append("vol_ok")
    elif quote_vol_24h is not None:
        flags.append("vol_low")
    if quote_vol_24h is None:
        flags.append("vol_unknown")
    if memory.entry_bias == "soft_block":
        flags.append("memory_soft_block")
    if memory.entry_bias == "prefer":
        flags.append("memory_prefer")
    if memory.hard_exclude_new_add:
        flags.append("memory_hard_exclude_new")

    # Shadow tier hint only (W2 does not enforce)
    if quality >= 0.65 and "vol_ok" in flags:
        tier = "T1"
    elif quality >= 0.40:
        tier = "T2"
    else:
        tier = "T3"

    return CoinQualityScore(
        symbol=sym,
        quality_score=round(quality, 4),
        scores=components,
        metrics={
            "quote_vol_24h": quote_vol_24h,
            "spread_pct": spread_pct,
            "change_24h_pct": change_24h_pct,
            "change_7d_pct": change_7d_pct,
            "mcap_usd": mcap_usd,
            "cmc_rank": cmc_rank,
            "source": source or "",
        },
        flags=flags,
        tier_hint=tier,
        memory={
            "entry_bias": memory.entry_bias,
            "memory_score": memory.memory_score,
            "hard_exclude_new_add": memory.hard_exclude_new_add,
            "source": memory.source,
            "rationale": memory.rationale,
        },
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score_coin_from_watchlist_row(
    coin: dict[str, Any],
    *,
    config: dict | None = None,
    venue: dict[str, Any] | None = None,
    regime_size_mult: float | None = None,
    sensor_policy: str | None = None,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
) -> CoinQualityScore:
    """Extract common fields from watchlist coin dict + optional venue stamp."""
    sym = str(coin.get("symbol") or coin.get("pair") or "").strip()
    venue = venue or {}
    vol = (
        coin.get("quote_vol_24h")
        or coin.get("volume_24h")
        or coin.get("quote_volume_24h_usdt")
        or venue.get("quote_volume_24h_usdt")
    )
    spread = coin.get("spread_pct") if coin.get("spread_pct") is not None else venue.get("spread_pct")
    return score_coin(
        sym,
        config=config,
        quote_vol_24h=_opt_float(vol),
        spread_pct=_opt_float(spread),
        change_24h_pct=_opt_float(coin.get("change_24h") or coin.get("percent_change_24h")),
        change_7d_pct=_opt_float(coin.get("change_7d") or coin.get("percent_change_7d")),
        cmc_rank=coin.get("cmc_rank") or coin.get("rank"),
        source=coin.get("source") or coin.get("watchlist_source"),
        mcap_usd=_opt_float(coin.get("mcap_usd") or coin.get("market_cap")),
        regime_size_mult=regime_size_mult,
        sensor_policy=sensor_policy,
        ledger_scope=ledger_scope,
        tenant_id=tenant_id,
    )
