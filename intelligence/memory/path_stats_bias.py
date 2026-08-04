"""Soft bias of exit knobs from memory_path_stats (read-only, fail-open).

Live path only *reads* precomputed summaries. Never runs episode scans.
Kill: MEMORY_PATH_STATS=0 / memory.path_stats.enabled=false
      OR memory.path_stats.soft_bias.enabled=false

Does not touch floor_at_entry, arm_on_peak, or order execution.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from intelligence.memory.path_stats import (
    PathBandSummary,
    list_path_summaries_for_symbol,
    path_stats_enabled,
)
from logger import log

_DEFAULT_SOFT = {
    "enabled": True,
    "require_quality": "ok",
    "prefer_band_pct": 10,
    "timeframe": "1h",
    "max_trail_delta_pct": 3.0,
    "max_arm_delta_pct": 2.0,
    "tighten_if_giveback_above": 0.10,
    "loosen_if_giveback_below": 0.05,
    "high_trail_hit_rate": 0.55,
    "high_extension_rate": 0.40,
    "min_trail_floor_pct": 5.0,
    "max_trail_ceil_pct": 30.0,
    "min_arm_floor_pct": 3.0,
    "max_arm_ceil_pct": 20.0,
    # TTP trail moves half as hard as stop trail (softer green trail)
    "ttp_trail_scale": 0.5,
}


def soft_bias_config(config: dict | None = None) -> dict:
    raw: dict = {}
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        mem = (config or {}).get("memory") or {}
        ps = mem.get("path_stats") or {}
        sb = ps.get("soft_bias") if isinstance(ps.get("soft_bias"), dict) else {}
        raw = dict(sb or {})
        # Inherit path_stats timeframe if soft_bias omits it
        if "timeframe" not in raw and ps.get("timeframe"):
            raw["timeframe"] = ps.get("timeframe")
    except Exception:
        raw = {}
    out = {**_DEFAULT_SOFT, **raw}
    out["enabled"] = bool(out.get("enabled", True))
    return out


def soft_bias_enabled(config: dict | None = None) -> bool:
    """Both master path_stats flag and soft_bias.enabled must allow."""
    if not path_stats_enabled(config):
        return False
    return bool(soft_bias_config(config).get("enabled", True))


def pick_band_summary(
    summaries: list[PathBandSummary],
    *,
    prefer_band: float = 0.10,
    require_quality: str = "ok",
) -> PathBandSummary | None:
    """Pick closest band to prefer_band with acceptable sample quality."""
    if not summaries:
        return None
    usable = summaries
    if require_quality == "ok":
        usable = [s for s in summaries if s.sample_quality == "ok"]
    if not usable:
        return None
    return min(usable, key=lambda s: abs(float(s.band) - float(prefer_band)))


def compute_bias_deltas(
    summary: PathBandSummary,
    bias_cfg: dict | None = None,
) -> dict[str, Any]:
    """Map episode summary → small trail/arm deltas (percent points).

    High median giveback / trail-hit rate → tighten trail (negative delta).
    Low giveback + high extension → loosen trail (positive delta).
    """
    cfg = {**_DEFAULT_SOFT, **(bias_cfg or {})}
    max_t = float(cfg.get("max_trail_delta_pct") or 3.0)
    max_a = float(cfg.get("max_arm_delta_pct") or 2.0)
    tight_gb = float(cfg.get("tighten_if_giveback_above") or 0.10)
    loose_gb = float(cfg.get("loosen_if_giveback_below") or 0.05)
    high_trail = float(cfg.get("high_trail_hit_rate") or 0.55)
    high_ext = float(cfg.get("high_extension_rate") or 0.40)

    gb = summary.median_max_giveback
    p_trail = summary.p_hit_trail
    p_ext = summary.p_hit_extension
    if gb is None:
        return {"trail_delta_pct": 0.0, "arm_delta_pct": 0.0, "reason": "no_giveback"}

    trail_delta = 0.0
    arm_delta = 0.0
    reason = "neutral"

    tighten = gb >= tight_gb or (p_trail is not None and p_trail >= high_trail)
    loosen = (
        gb <= loose_gb
        and p_ext is not None
        and p_ext >= high_ext
        and not tighten
    )

    if tighten:
        # Scale 0..1 from threshold → 0.20 giveback (or p_trail 0.55→0.85)
        sev_gb = 0.0
        if gb >= tight_gb:
            sev_gb = min(1.0, (gb - tight_gb) / max(0.01, 0.20 - tight_gb))
        sev_tr = 0.0
        if p_trail is not None and p_trail >= high_trail:
            sev_tr = min(1.0, (p_trail - high_trail) / max(0.01, 0.85 - high_trail))
        sev = max(sev_gb, sev_tr)
        trail_delta = -max_t * (0.35 + 0.65 * sev)
        # Dumps after arm → arm a bit earlier so stop is live sooner
        arm_delta = -max_a * (0.25 + 0.5 * sev)
        reason = "tighten"
    elif loosen:
        sev_gb = min(1.0, (loose_gb - gb) / max(0.01, loose_gb)) if loose_gb > 0 else 0.5
        sev_ex = 0.0
        if p_ext is not None:
            sev_ex = min(1.0, (p_ext - high_ext) / max(0.01, 0.80 - high_ext))
        sev = max(sev_gb, sev_ex)
        trail_delta = max_t * (0.25 + 0.6 * sev)
        # Runners often extend → arm a tad later so we don't choke early
        arm_delta = max_a * (0.2 + 0.4 * sev)
        reason = "loosen"

    return {
        "trail_delta_pct": round(trail_delta, 3),
        "arm_delta_pct": round(arm_delta, 3),
        "reason": reason,
        "giveback": round(float(gb), 4),
        "p_hit_trail": None if p_trail is None else round(float(p_trail), 4),
        "p_hit_extension": None if p_ext is None else round(float(p_ext), 4),
        "band": summary.band_key,
        "n": summary.n,
        "quality": summary.sample_quality,
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _apply_section(
    section: dict,
    *,
    trail_delta: float,
    arm_delta: float,
    trail_keys: tuple[str, ...],
    arm_keys: tuple[str, ...],
    min_trail: float,
    max_trail: float,
    min_arm: float,
    max_arm: float,
) -> dict:
    out = dict(section)
    for k in trail_keys:
        if k not in out:
            continue
        try:
            out[k] = round(_clamp(float(out[k]) + trail_delta, min_trail, max_trail), 3)
        except (TypeError, ValueError):
            pass
    for k in arm_keys:
        if k not in out:
            continue
        try:
            out[k] = round(_clamp(float(out[k]) + arm_delta, min_arm, max_arm), 3)
        except (TypeError, ValueError):
            pass
    return out


def apply_path_stats_soft_bias(
    strategy_params: dict | None,
    symbol: str,
    *,
    config: dict | None = None,
    summaries: list[PathBandSummary] | None = None,
) -> dict:
    """Return strategy_params with soft trail/arm bias. Fail-open identity on miss.

    Never raises to callers (except programming errors in tests with injected summaries).
    """
    sp = deepcopy(strategy_params or {})
    if not symbol:
        return sp
    try:
        if not soft_bias_enabled(config):
            return sp
        bias_cfg = soft_bias_config(config)
        prefer = float(bias_cfg.get("prefer_band_pct") or 10) / 100.0
        # Prefer band near trailing activation if present
        ts = sp.get("trailing_stop") if isinstance(sp.get("trailing_stop"), dict) else {}
        if ts.get("activation_gain_pct") is not None:
            try:
                prefer = float(ts["activation_gain_pct"]) / 100.0
            except (TypeError, ValueError):
                pass
        ttp = (
            sp.get("trailing_take_profit")
            if isinstance(sp.get("trailing_take_profit"), dict)
            else {}
        )
        # If only TTP arm is set and closer to default prefer, leave prefer from stop

        if summaries is None:
            summaries = list_path_summaries_for_symbol(
                symbol,
                timeframe=str(bias_cfg.get("timeframe") or "1h"),
                config=config,
            )
        picked = pick_band_summary(
            summaries,
            prefer_band=prefer,
            require_quality=str(bias_cfg.get("require_quality") or "ok"),
        )
        if not picked:
            return sp

        deltas = compute_bias_deltas(picked, bias_cfg)
        trail_d = float(deltas.get("trail_delta_pct") or 0.0)
        arm_d = float(deltas.get("arm_delta_pct") or 0.0)
        if trail_d == 0.0 and arm_d == 0.0:
            sp["_path_stats_bias"] = {**deltas, "applied": False, "symbol": symbol}
            return sp

        min_t = float(bias_cfg.get("min_trail_floor_pct") or 5.0)
        max_t = float(bias_cfg.get("max_trail_ceil_pct") or 30.0)
        min_a = float(bias_cfg.get("min_arm_floor_pct") or 3.0)
        max_a = float(bias_cfg.get("max_arm_ceil_pct") or 20.0)
        ttp_scale = float(bias_cfg.get("ttp_trail_scale") or 0.5)

        if ts:
            sp["trailing_stop"] = _apply_section(
                ts,
                trail_delta=trail_d,
                arm_delta=arm_d,
                trail_keys=("min_trail_pct", "max_trail_pct"),
                arm_keys=("activation_gain_pct",),
                min_trail=min_t,
                max_trail=max_t,
                min_arm=min_a,
                max_arm=max_a,
            )
            # Preserve safety rails — never flip off via bias
            sp["trailing_stop"]["floor_at_entry"] = ts.get("floor_at_entry", True)
            sp["trailing_stop"]["arm_on_peak"] = ts.get("arm_on_peak", True)

        if ttp:
            sp["trailing_take_profit"] = _apply_section(
                ttp,
                trail_delta=trail_d * ttp_scale,
                arm_delta=arm_d,
                trail_keys=("trail_pct", "trail_pct_min", "trail_pct_max"),
                arm_keys=("arm_gain_pct",),
                min_trail=max(2.0, min_t * 0.5),
                max_trail=max_t,
                min_arm=min_a,
                max_arm=max_a,
            )
            # Soft green floor stays on
            if "trail_above_zero_after_arm" in ttp:
                sp["trailing_take_profit"]["trail_above_zero_after_arm"] = ttp[
                    "trail_above_zero_after_arm"
                ]

        sp["_path_stats_bias"] = {
            **deltas,
            "applied": True,
            "symbol": symbol,
            "prefer_band": prefer,
        }
        return sp
    except Exception as e:
        log(f"path_stats soft_bias fail-open {symbol}: {e}", "DEBUG")
        return deepcopy(strategy_params or {})
