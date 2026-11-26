"""Continuous market-oracle regime score (KLTB-7, #835).

Pure functions. Every weight, scale, half-life, gap, staleness limit and
mapping coefficient is an argument. Callers read them from
``rule_switches.oracle_score`` in committed ``config.json``.

Component inputs are the feature dict ``MarketDataClient.fetch_features``
already produces. This module does not recompute those features.

The score and each component stay in [-1, 1]. That bound and the half-life
step ``alpha = 1 - 0.5 ** (dt / half_life)`` are the definition in #835,
not a trading threshold.
"""

from __future__ import annotations

import math
from typing import Any

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)


def clip(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _blend_value(features: dict, legs: list) -> float | None:
    """Weighted sum of blend legs. Any missing leg drops the whole blend.

    The weights are the coefficients in the sum (0.7 and 0.3 stay 0.7·a + 0.3·b).
    They are not renormalised inside the blend.
    """
    if not isinstance(features, dict) or not legs:
        return None
    acc = 0.0
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        feature = leg.get("feature")
        weight = _finite(leg.get("weight"))
        if not isinstance(feature, str) or not feature or weight is None:
            return None
        if feature not in features:
            return None
        value = _finite(features.get(feature))
        if value is None:
            return None
        acc += weight * value
    return acc


def components(features: dict | None, specs: list | None) -> dict[str, float]:
    """One clipped component per spec whose inputs are present.

    A spec with ``blend`` uses the weighted sum of those features, then
    ``(blend - center) / scale * sign``. A single ``feature`` uses that value
    the same way. The result is clipped to [-1, 1]. A missing input, a
    non-numeric value, or a zero scale drops that component.
    """
    out: dict[str, float] = {}
    if not isinstance(features, dict) or not isinstance(specs, list):
        return out
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        blend = spec.get("blend")
        if isinstance(blend, list) and blend:
            value = _blend_value(features, blend)
        else:
            feature = spec.get("feature")
            if not isinstance(feature, str) or not feature or feature not in features:
                continue
            value = _finite(features.get(feature))
        scale = _finite(spec.get("scale"))
        if value is None or scale is None or scale == 0.0:
            continue
        center = _finite(spec.get("center", 0.0))
        sign = _finite(spec.get("sign", 1.0))
        if center is None or sign is None or sign == 0.0:
            continue
        out[name] = clip((value - center) / scale * sign)
    return out


def weights_of(specs: list | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(specs, list):
        return out
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        weight = _finite(spec.get("weight"))
        if isinstance(name, str) and name and weight is not None and weight > 0:
            out[name] = weight
    return out


def raw_score(parts: dict | None, weights: dict | None) -> float | None:
    """Weighted mean of the present components. Missing names are dropped
    and the remaining weights are renormalised. No positive weight → None.
    """
    if not isinstance(parts, dict) or not isinstance(weights, dict):
        return None
    num = 0.0
    den = 0.0
    for name, value in parts.items():
        if name not in weights:
            continue
        weight = _finite(weights.get(name))
        component = _finite(value)
        if weight is None or component is None or weight <= 0:
            continue
        num += weight * clip(component)
        den += weight
    if den <= 0:
        return None
    return clip(num / den)


def ema_update(prev: float | None, raw: float, dt_sec: float, half_life_sec: float) -> float:
    """One EMA step. ``alpha = 1 - 0.5 ** (dt / half_life)``.

    A missing previous value, a non-positive dt, or a non-positive half-life
    returns ``raw`` (already clipped).
    """
    raw_f = _finite(raw)
    if raw_f is None:
        prev_f = _finite(prev)
        return 0.0 if prev_f is None else clip(prev_f)
    raw_c = clip(raw_f)
    prev_f = _finite(prev)
    dt = _finite(dt_sec)
    half = _finite(half_life_sec)
    if prev_f is None or dt is None or half is None or dt <= 0 or half <= 0:
        return raw_c
    alpha = 1.0 - (0.5 ** (dt / half))
    return clip(prev_f + alpha * (raw_c - prev_f))


def smooth_score(
    prev: float | None,
    prev_ts: float | None,
    raw: float | None,
    now_ts: float | None,
    *,
    half_life_sec: float,
    max_gap_sec: float,
) -> tuple[float | None, bool]:
    """Return ``(smoothed, reset)``.

    A gap longer than ``max_gap_sec``, or a missing previous sample, resets
    to ``raw``. ``reset`` is True in that case. A missing raw returns the
    previous smoothed value and does not reset.
    """
    raw_f = _finite(raw)
    if raw_f is None:
        return (_finite(prev), False)
    prev_f = _finite(prev)
    prev_t = _finite(prev_ts)
    now_t = _finite(now_ts)
    gap = _finite(max_gap_sec)
    if prev_f is None or prev_t is None or now_t is None:
        return (clip(raw_f), True)
    dt = now_t - prev_t
    if dt <= 0 or (gap is not None and dt > gap):
        return (clip(raw_f), True)
    return (ema_update(prev_f, raw_f, dt, half_life_sec), False)


def _round_half_up(value: float) -> int:
    """Round half away from zero for the non-negative SMA lengths in #835."""
    return int(math.floor(float(value) + 0.5))


def filter_params(s: float, base_cfg: dict | None, mapping: dict | None) -> dict[str, Any]:
    """Map a score onto filter parameters relative to the tenant base.

    Every coefficient is read from ``mapping``. s = 0 returns the tenant base.
    ``max_pump_24h_pct`` is not mapped. ``sma_condition_off`` is true at and
    above ``sma_off_at``; ``sma_window`` is then the configured floor, used
    only to measure extension. ``size_round_up_frac`` is the fraction of
    ``min_trade`` inside which a shortfall may be rounded up (0 means skip).
    """
    base = base_cfg if isinstance(base_cfg, dict) else {}
    block = mapping if isinstance(mapping, dict) else {}
    s_f = _finite(s)
    if s_f is None:
        s_f = 0.0
    s_f = clip(s_f)

    def base_of(field: str) -> float | None:
        return _finite(base.get(field))

    sma_base = base_of("sma_days")
    tight = _finite(block.get("sma_tight_days"))
    neg_span = _finite(block.get("sma_neg_span"))
    pos_span = _finite(block.get("sma_pos_span"))
    floor = _finite(block.get("sma_floor"))
    off_at = _finite(block.get("sma_off_at"))
    sma_days: int | None = None
    sma_off = False
    sma_window: int | None = None
    if (
        sma_base is not None
        and sma_base > 0
        and tight is not None
        and tight > 0
        and neg_span is not None
        and neg_span > 0
        and pos_span is not None
        and pos_span > 0
        and floor is not None
    ):
        floor_n = max(1, _round_half_up(floor))
        if off_at is not None and s_f >= off_at:
            sma_off = True
            sma_window = floor_n
        elif s_f < 0:
            power = min((-s_f) / neg_span, 1.0)
            sma_days = _round_half_up(sma_base * ((tight / sma_base) ** power))
            if sma_days < 1:
                sma_days = max(1, _round_half_up(sma_base))
            sma_window = sma_days
        else:
            raw = sma_base - (sma_base - 1.0) * min(s_f / pos_span, 1.0)
            sma_days = max(floor_n, _round_half_up(raw))
            sma_window = sma_days

    ext = None
    ext_base = base_of("max_extension_pct")
    slope = _finite(block.get("extension_slope"))
    if ext_base is not None and slope is not None:
        ext = ext_base * (1.0 + slope * s_f)

    floor_base = _finite(block.get("floor_base"))
    floor_exp = _finite(block.get("floor_exponent"))

    def scaled_floor(field: str) -> float | None:
        value = base_of(field)
        if value is None or floor_base is None or floor_base <= 0 or floor_exp is None:
            return None
        return value * (floor_base ** (-s_f * floor_exp))

    frac = None
    round_frac = _finite(block.get("size_round_up_frac"))
    if round_frac is not None:
        frac = round_frac * max(s_f, 0.0)

    return {
        "s": s_f,
        "sma_days": sma_days,
        "sma_condition_off": sma_off,
        "sma_window": sma_window,
        "max_extension_pct": ext,
        "mcap_floor_usd": scaled_floor("mcap_floor_usd"),
        "liq_floor_usdt": scaled_floor("liq_floor_usdt"),
        "size_round_up_frac": frac,
    }


def clamp_not_looser(params: dict | None, base_cfg: dict | None, s: float | None) -> dict:
    """For s <= 0, refuse a mapped sma shorter or an extension wider than base.

    A positive score may loosen. An SMA condition that is off is left off.
    max_pump is not a mapped field.
    """
    out = dict(params or {})
    base = base_cfg if isinstance(base_cfg, dict) else {}
    s_f = _finite(s)
    if s_f is None or s_f > 0:
        return out
    base_sma = _finite(base.get("sma_days"))
    mapped_sma = _finite(out.get("sma_days"))
    if base_sma is not None and mapped_sma is not None and not out.get("sma_condition_off"):
        held = int(max(mapped_sma, base_sma))
        out["sma_days"] = held
        out["sma_window"] = held
    base_ext = _finite(base.get("max_extension_pct"))
    mapped_ext = _finite(out.get("max_extension_pct"))
    if base_ext is not None and mapped_ext is not None:
        out["max_extension_pct"] = min(mapped_ext, base_ext)
    return out


def normalize_mode(value: Any) -> str:
    """Only the literals ``off``, ``shadow`` and ``enforce``. Anything else is ``off``.

    Booleans, ``on``, ``yes`` and ``1`` are not enforce. A typo cannot loosen entries.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in MODES:
            return text
    return MODE_OFF


def _mode_override(value: Any) -> str | None:
    """A set override, or None when the value is blank so resolution can fall through."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "none", "null"):
        return None
    return normalize_mode(value)


def resolve_mode(effective_block: dict | None, tenant_id: str | None) -> str:
    """``tenant_mode_override`` if set, else ``tenant_modes[tenant_id]``, else ``default_mode``.

    The committed key is ``default_mode``. ``mode`` is ignored, so a save-back of the
    merged config cannot pin the committed default over ``tenant_modes``.
    """
    block = effective_block if isinstance(effective_block, dict) else {}
    if "tenant_mode_override" in block:
        chosen = _mode_override(block.get("tenant_mode_override"))
        if chosen is not None:
            return chosen
    modes = block.get("tenant_modes")
    if isinstance(modes, dict) and tenant_id and str(tenant_id) in modes:
        chosen = _mode_override(modes.get(str(tenant_id)))
        if chosen is not None:
            return chosen
    if "default_mode" in block:
        chosen = _mode_override(block.get("default_mode"))
        if chosen is not None:
            return chosen
    return MODE_OFF


def mode_from_raw(config_raw: dict | None, tenant_id: str | None) -> str:
    """Mode from an in-memory tenant-effective config. No disk and no Mongo."""
    raw = config_raw if isinstance(config_raw, dict) else {}
    switches = raw.get("rule_switches")
    block = switches.get("oracle_score") if isinstance(switches, dict) else None
    if not isinstance(block, dict):
        return MODE_OFF
    return resolve_mode(block, tenant_id)


def score_is_fresh(ts: float | None, now_ts: float | None, stale_after_sec: float | None) -> bool:
    stamp = _finite(ts)
    now = _finite(now_ts)
    limit = _finite(stale_after_sec)
    if stamp is None or now is None or limit is None or limit < 0:
        return False
    age = now - stamp
    if age < 0:
        return False
    return age <= limit


def should_write_history(last_ts: float | None, now_ts: float | None, min_interval_sec: float | None) -> bool:
    now = _finite(now_ts)
    if now is None:
        return False
    last = _finite(last_ts)
    if last is None:
        return True
    interval = _finite(min_interval_sec)
    if interval is None or interval <= 0:
        return True
    return (now - last) >= interval


def history_cutoff(now_ts: float, retain_sec: float) -> float | None:
    now = _finite(now_ts)
    retain = _finite(retain_sec)
    if now is None or retain is None or retain <= 0:
        return None
    return now - retain


def _fmt_num(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if text else "na"
    num = _finite(value)
    if num is None:
        return "na"
    text = f"{num:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def _fmt_bool(value: Any) -> str:
    if value is None:
        return "na"
    return "true" if bool(value) else "false"


def format_shadow_line(
    *,
    tenant: str,
    symbol: str,
    s: Any,
    raw: Any,
    age_s: Any,
    sma_days_fixed: Any,
    sma_days_score: Any,
    max_ext_fixed: Any,
    max_ext_score: Any,
    would_pass_fixed: Any,
    would_pass_score: Any,
    mcap_fixed: Any = None,
    mcap_score: Any = None,
    liq_fixed: Any = None,
    liq_score: Any = None,
    size_round_up: Any = None,
    prefix: str = "oracle_score_shadow",
) -> str:
    """One structured line. The required fields stay in this order.

    mcap, liquidity and size round-up are appended. They are logged only.
    """
    return (
        f"{prefix} tenant={tenant} symbol={symbol} "
        f"s={_fmt_num(s)} raw={_fmt_num(raw)} age_s={_fmt_num(age_s)} "
        f"sma_days_fixed={_fmt_num(sma_days_fixed)} sma_days_score={_fmt_num(sma_days_score)} "
        f"max_ext_fixed={_fmt_num(max_ext_fixed)} max_ext_score={_fmt_num(max_ext_score)} "
        f"would_pass_fixed={_fmt_bool(would_pass_fixed)} "
        f"would_pass_score={_fmt_bool(would_pass_score)} "
        f"mcap_fixed={_fmt_num(mcap_fixed)} mcap_score={_fmt_num(mcap_score)} "
        f"liq_fixed={_fmt_num(liq_fixed)} liq_score={_fmt_num(liq_score)} "
        f"size_round_up={_fmt_num(size_round_up)}"
    )
