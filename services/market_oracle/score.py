"""Continuous market-oracle regime score (KLTB-7, #835).

Pure functions. Every weight, scale, half-life, gap, staleness limit and
mapping anchor is an argument. Callers read them from
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


def components(features: dict | None, specs: list | None) -> dict[str, float]:
    """One clipped component per spec whose feature is present.

    ``(value - center) / scale * sign``, then clipped to [-1, 1]. A missing
    feature, a non-numeric value, or a zero scale drops that component.
    """
    out: dict[str, float] = {}
    if not isinstance(features, dict) or not isinstance(specs, list):
        return out
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        feature = spec.get("feature")
        if not isinstance(name, str) or not name or not isinstance(feature, str) or not feature:
            continue
        if feature not in features:
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


def _interp(s: float, anchors: list, field: str) -> float | None:
    points: list[tuple[float, float]] = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or field not in anchor:
            continue
        xs = _finite(anchor.get("s"))
        ys = _finite(anchor.get(field))
        if xs is None or ys is None:
            continue
        points.append((xs, ys))
    if not points:
        return None
    points.sort(key=lambda item: item[0])
    if s <= points[0][0]:
        return points[0][1]
    if s >= points[-1][0]:
        return points[-1][1]
    for (s0, y0), (s1, y1) in zip(points, points[1:]):
        if s0 <= s <= s1:
            if s1 == s0:
                return y1
            t = (s - s0) / (s1 - s0)
            return y0 + t * (y1 - y0)
    return points[-1][1]


def _scaled(interp: float | None, base: float | None, ref: float | None) -> float | None:
    if interp is None:
        return base
    if base is None or ref is None or ref == 0.0:
        return interp
    return interp * (base / ref)


def filter_params(s: float, base_cfg: dict | None, mapping: dict | None) -> dict[str, Any]:
    """Map a score onto filter parameters.

    At each anchor, a field scales by ``base / reference`` so s = 0 reproduces
    the tenant base when the reference equals that base. Outside the anchor
    span the nearest anchor is held, so a higher score is never stricter than
    the loosest anchor. ``sma_days`` is floored (a non-increasing round) and
    then held at ``mapping["sma_days_min"]`` when that key is set.
    """
    base = base_cfg if isinstance(base_cfg, dict) else {}
    block = mapping if isinstance(mapping, dict) else {}
    ref = block.get("reference") if isinstance(block.get("reference"), dict) else {}
    anchors = block.get("anchors") if isinstance(block.get("anchors"), list) else []
    s_f = _finite(s)
    if s_f is None:
        s_f = 0.0
    s_f = clip(s_f)
    sma_min = _finite(block.get("sma_days_min"))

    def one(field: str, *, as_int: bool = False) -> float | int | None:
        interp = _interp(s_f, anchors, field)
        base_v = _finite(base.get(field))
        ref_v = _finite(ref.get(field))
        scaled = _scaled(interp, base_v, ref_v)
        if scaled is None:
            return None
        if not as_int:
            return scaled
        n = int(math.floor(scaled + 1e-9))
        if sma_min is not None:
            n = max(int(math.floor(sma_min)), n)
        if n < 1:
            if base_v is None:
                return None
            return max(1, int(math.floor(base_v)))
        return n

    return {
        "s": s_f,
        "sma_days": one("sma_days", as_int=True),
        "max_extension_pct": one("max_extension_pct"),
        "max_pump_24h_pct": one("max_pump_24h_pct"),
        "mcap_floor_usd": one("mcap_floor_usd"),
        "liq_floor_usdt": one("liq_floor_usdt"),
        "size_round_up_usdt": one("size_round_up_usdt"),
    }


def clamp_not_looser(params: dict | None, base_cfg: dict | None, s: float | None) -> dict:
    """For s <= 0, refuse a mapped sma shorter or an extension wider than base.

    A positive score may loosen. max_pump is left as mapped; the trend filter
    does not apply it in enforce (the caller keeps the tenant pump cap).
    """
    out = dict(params or {})
    base = base_cfg if isinstance(base_cfg, dict) else {}
    s_f = _finite(s)
    if s_f is None or s_f > 0:
        return out
    base_sma = _finite(base.get("sma_days"))
    mapped_sma = _finite(out.get("sma_days"))
    if base_sma is not None and mapped_sma is not None:
        out["sma_days"] = int(max(mapped_sma, base_sma))
    base_ext = _finite(base.get("max_extension_pct"))
    mapped_ext = _finite(out.get("max_extension_pct"))
    if base_ext is not None and mapped_ext is not None:
        out["max_extension_pct"] = min(mapped_ext, base_ext)
    return out


def normalize_mode(value: Any) -> str | None:
    """Return a known mode, or None when the value is absent.

    An unknown non-empty value is ``off`` so a typo cannot loosen entries.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return MODE_ENFORCE if value else MODE_OFF
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return None
    if text in MODES:
        return text
    if text in ("1", "true", "yes", "on"):
        return MODE_ENFORCE
    if text in ("0", "false", "no"):
        return MODE_OFF
    return MODE_OFF


def resolve_mode(tenant_block: dict | None, committed_block: dict | None, tenant_id: str | None) -> str:
    """Tenant ``mode`` if set, else ``tenant_modes[tenant_id]``, else committed ``mode``."""
    if isinstance(tenant_block, dict) and "mode" in tenant_block:
        chosen = normalize_mode(tenant_block.get("mode"))
        if chosen is not None:
            return chosen
    committed = committed_block if isinstance(committed_block, dict) else {}
    modes = committed.get("tenant_modes")
    if isinstance(modes, dict) and tenant_id:
        key = str(tenant_id)
        if key in modes:
            chosen = normalize_mode(modes.get(key))
            if chosen is not None:
                return chosen
    chosen = normalize_mode(committed.get("mode"))
    return chosen if chosen is not None else MODE_OFF


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
