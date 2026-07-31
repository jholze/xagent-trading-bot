"""Per-coin rebuy cooldown after sell (regime + exit + CoinProfile).

Flag: risk.rebuy_cooldown.enabled
Rollback: enabled=false → architecture.min_hours_after_sell_before_rebuy

Does not replace sensor gross-loss 168h (_sensor_reentry_cooloff_blocked).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_REGIMES = frozenset({"RISK_ON", "NEUTRAL", "RISK_OFF", "CRASH", "WARMUP"})

# Exact full-string aliases (lowercase) → exit_key
_EXIT_ALIASES: dict[str, str] = {
    "trail_tp": "trailing_take_profit",
    "ttp": "trailing_take_profit",
    "sell": "technical",
    "sell_full": "technical",
    "sell_tp": "technical",
    "lc": "social",
    "cmc": "social",
}

# First match wins. Patterns are lowercase substrings of joined exit labels.
# Order matters: more specific patterns before broad ones (e.g. stop before trailing).
_EXIT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stop", ("sell_stop", "stop_loss", "x_stop_loss", "stoploss")),
    (
        "trailing_take_profit",
        (
            "trailing_take_profit",
            "trail_tp",
            "ttp",
            "profit_max_lifetime",
            "time_profit",
        ),
    ),
    ("bb_upper", ("bb_upper",)),
    ("vol_structure", ("vol_exhaustion", "vol_dump")),
    ("exit_sensor", ("exit_15m", "exit_pullback", "exit_volume", "exit_btc", "exit_1h")),
    ("trailing_stop", ("trailing_stop",)),
    ("ladder_tail", ("ladder_terminal", "tail_idle")),
    ("grid", ("grid",)),
    ("social", ("cmc", "lunarcrush", " social", "social_")),
    (
        "technical",
        (
            "technical",
            "sell_partial",
            "sell_full",
            "sell_30",
            "sell_20",
            "sell_10",
            "sell_tp",
            "rsi_sell",
            "take_profit",
        ),
    ),
)

# exit_key → order-source tags in features.by_source
_CHANNEL_TAGS: dict[str, tuple[str, ...]] = {
    "grid": ("grid",),
    "social": ("cmc", "lc", "auto"),
    "technical": ("auto", "technical"),
    "trailing_take_profit": ("auto", "grid"),
    "bb_upper": ("auto", "grid"),
    "trailing_stop": ("auto", "grid"),
    "vol_structure": ("auto",),
    "exit_sensor": ("auto",),
}


def rebuy_cooldown_config(risk_config: dict | None = None, raw: dict | None = None) -> dict:
    risk = risk_config if isinstance(risk_config, dict) else {}
    cfg = risk.get("rebuy_cooldown")
    if isinstance(cfg, dict):
        return dict(cfg)
    if isinstance(raw, dict):
        arch = raw.get("architecture") or {}
        if isinstance(arch.get("rebuy_cooldown"), dict):
            return dict(arch["rebuy_cooldown"])
        nested = (raw.get("risk") or {}).get("rebuy_cooldown")
        if isinstance(nested, dict):
            return dict(nested)
    return {}


def rebuy_cooldown_enabled(risk_config: dict | None = None, raw: dict | None = None) -> bool:
    return bool(rebuy_cooldown_config(risk_config, raw).get("enabled"))


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _cfg_f(m: dict, key: str, default: float) -> float:
    return _num(m.get(key, default), default)


def _attr_f(obj: Any, name: str, default: float = 0.0) -> float:
    return _num(getattr(obj, name, default), default)


def _attr_i(obj: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(obj, name, default) or default)
    except (TypeError, ValueError):
        return default


def is_hard_stop_sell(signal: str) -> bool:
    """Hard stop-loss only — not ATR trailing_stop / trail TP.

    Legacy risk_manager._is_stop_loss_sell matches any 'STOP' substring and
    incorrectly treats trailing_stop as a hard stop. This helper is intentional
    and narrower for rebuy policy.
    """
    s = str(signal or "").strip().upper()
    if not s:
        return False
    # Exclude trailing / take-profit paths first
    if "TRAILING" in s or "TAKE_PROFIT" in s or "TRAIL_TP" in s:
        return False
    if s in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL") or s.startswith("SELL_STOP"):
        return True
    if s == "STOP" or s.endswith("_STOP_LOSS") or "STOP_LOSS" in s or "STOPLOSS" in s:
        return True
    return False


def pick_exit_label(
    *,
    last_exit_source: str | None = None,
    last_sell_signal: str | None = None,
    order_signal: str | None = None,
) -> str:
    for cand in (last_exit_source, last_sell_signal, order_signal):
        if cand and str(cand).strip():
            return str(cand).strip()
    return ""


def normalize_exit_key(*labels: str) -> str:
    parts = [str(x or "").strip().lower() for x in labels if x and str(x).strip()]
    if not parts:
        return "default"
    # Prefer first non-empty label alone for exact aliases
    primary = parts[0]
    if primary in _EXIT_ALIASES:
        return _EXIT_ALIASES[primary]
    s = " ".join(parts)
    if s in _EXIT_ALIASES:
        return _EXIT_ALIASES[s]
    for key, pats in _EXIT_RULES:
        if key == "trailing_stop" and "take_profit" in s:
            continue
        if any(p in s for p in pats):
            return key
    return "default"


def _regime_key(regime: str | None) -> str:
    r = str(regime or "NEUTRAL").strip().upper() or "NEUTRAL"
    return r if r in _REGIMES else "NEUTRAL"


def _map_mult(table: Any, key: str, default: float = 1.0) -> float:
    if not isinstance(table, dict):
        return default
    if key in table:
        return _cfg_f(table, key, default)
    return _cfg_f(table, "default", default) if "default" in table else default


@dataclass(frozen=True)
class RebuyCooldownResult:
    hours: float
    reasons: list[str] = field(default_factory=list)
    factors: dict[str, Any] = field(default_factory=dict)
    stop_sell: bool = False


def _apply_band(
    mult: float,
    value: float,
    *,
    high: float,
    low: float,
    high_mult: float,
    low_mult: float,
    high_tag: str,
    low_tag: str,
    reasons: list[str],
    prefer_high_first: bool = True,
) -> float:
    """Apply high/low band multiplier; only one branch fires."""
    if prefer_high_first:
        if value >= high:
            reasons.append(high_tag)
            return mult * high_mult
        if value <= low:
            reasons.append(low_tag)
            return mult * low_mult
    else:
        if value <= low:
            reasons.append(low_tag)
            return mult * low_mult
        if value >= high:
            reasons.append(high_tag)
            return mult * high_mult
    return mult


def resolve_memory_mult(
    profile: Any | None,
    mem_cfg: dict,
    *,
    exit_key: str = "default",
) -> tuple[float, list[str], dict[str, Any]]:
    """CoinProfile → multiplier (fail-open on missing profile)."""
    reasons: list[str] = []
    factors: dict[str, Any] = {}
    if not mem_cfg.get("enabled", True):
        return 1.0, ["memory_disabled"], {"memory_mult": 1.0}
    if profile is None:
        m = _cfg_f(mem_cfg, "missing_profile_mult", 1.0)
        return m, ["missing_profile"], {"memory_mult": m}

    mult = 1.0
    n = _attr_i(profile, "sells_30d") or _attr_i(profile, "trades_30d")
    min_n = int(mem_cfg.get("min_samples", 3) or 3)
    wr = _attr_f(profile, "win_rate")
    total_pnl = _attr_f(profile, "total_pnl_usdt")
    avg_pnl = _attr_f(profile, "avg_pnl_usdt")
    risk_score = _attr_f(profile, "risk_score", 0.5)
    size_bias = _attr_f(profile, "size_bias", 1.0)
    dca_n = _attr_i(profile, "dca_count_30d")
    bias = str(getattr(profile, "entry_bias", None) or "neutral").lower()

    factors.update(
        {
            "sells_30d": n,
            "win_rate": round(wr, 4),
            "total_pnl_usdt": round(total_pnl, 2),
            "avg_pnl_usdt": round(avg_pnl, 2),
            "risk_score": round(risk_score, 3),
            "size_bias": round(size_bias, 4),
            "entry_bias": bias,
            "dca_count_30d": dca_n,
        }
    )

    if n < min_n:
        reasons.append("wr_under_sample")
    else:
        mult = _apply_band(
            mult,
            wr,
            high=_cfg_f(mem_cfg, "win_rate_high", 0.55),
            low=_cfg_f(mem_cfg, "win_rate_low", 0.4),
            high_mult=_cfg_f(mem_cfg, "high_wr_mult", 0.75),
            low_mult=_cfg_f(mem_cfg, "low_wr_mult", 1.4),
            high_tag="high_wr",
            low_tag="low_wr",
            reasons=reasons,
        )
        mult = _apply_band(
            mult,
            total_pnl,
            high=_cfg_f(mem_cfg, "pnl_strong_usdt", 20.0),
            low=_cfg_f(mem_cfg, "pnl_weak_usdt", -50.0),
            high_mult=_cfg_f(mem_cfg, "pnl_strong_mult", 0.9),
            low_mult=_cfg_f(mem_cfg, "pnl_weak_mult", 1.25),
            high_tag="pnl_strong",
            low_tag="pnl_weak",
            reasons=reasons,
        )
        if avg_pnl <= _cfg_f(mem_cfg, "avg_pnl_weak_usdt", -15.0):
            mult *= _cfg_f(mem_cfg, "avg_pnl_weak_mult", 1.15)
            reasons.append("avg_pnl_weak")

    if risk_score >= _cfg_f(mem_cfg, "risk_score_high", 0.65):
        mult *= _cfg_f(mem_cfg, "risk_score_high_mult", 1.2)
        reasons.append("risk_score_high")
    elif risk_score <= _cfg_f(mem_cfg, "risk_score_low", 0.35) and n >= min_n:
        mult *= _cfg_f(mem_cfg, "risk_score_low_mult", 0.9)
        reasons.append("risk_score_low")

    if bias == "prefer":
        mult *= _cfg_f(mem_cfg, "prefer_mult", 0.85)
        reasons.append("bias_prefer")
    elif bias == "soft_block":
        mult *= _cfg_f(mem_cfg, "soft_block_mult", 1.5)
        reasons.append("bias_soft_block")

    weight = _cfg_f(mem_cfg, "size_bias_weight", 0.15)
    if weight:
        mult *= 1.0 + weight * (1.0 - size_bias)
        reasons.append("size_bias")

    if dca_n >= int(mem_cfg.get("dca_heavy_count", 4) or 4):
        mult *= _cfg_f(mem_cfg, "dca_heavy_mult", 1.1)
        reasons.append("dca_heavy")

    feats = getattr(profile, "features", None)
    if isinstance(feats, dict):
        if feats.get("structure_risk") or feats.get("hard_negative"):
            mult *= _cfg_f(mem_cfg, "structure_risk_mult", 1.35)
            reasons.append("structure_risk")
        factors["has_last_loss"] = bool(
            feats.get("last_loss_at") or feats.get("soft_block_until")
        )
        if feats.get("last_loss_source"):
            factors["last_loss_source"] = str(feats["last_loss_source"])

        by_src = feats.get("by_source")
        if isinstance(by_src, dict) and by_src:
            ch_pnl = 0.0
            ch_sells = 0
            for tag in _CHANNEL_TAGS.get(exit_key, ()):
                row = by_src.get(tag) or {}
                if not isinstance(row, dict):
                    continue
                ch_pnl += _num(row.get("pnl_usdt"))
                ch_sells += int(_num(row.get("sells")))
            factors["channel_pnl_usdt"] = round(ch_pnl, 2)
            factors["channel_sells"] = ch_sells
            if ch_sells >= 2:
                mult = _apply_band(
                    mult,
                    ch_pnl,
                    high=_cfg_f(mem_cfg, "channel_pnl_strong_usdt", 30.0),
                    low=_cfg_f(mem_cfg, "channel_pnl_weak_usdt", -30.0),
                    high_mult=_cfg_f(mem_cfg, "channel_pnl_strong_mult", 0.9),
                    low_mult=_cfg_f(mem_cfg, "channel_pnl_weak_mult", 1.2),
                    high_tag="channel_pnl_strong",
                    low_tag="channel_pnl_weak",
                    reasons=reasons,
                    prefer_high_first=False,
                )

        venue = feats.get("venue")
        if isinstance(venue, dict):
            thin_n = int(_num(venue.get("entries_thin_30d")))
            pnl_thin = _num(venue.get("pnl_when_thin_usdt"))
            factors["venue_thin_entries"] = thin_n
            factors["venue_pnl_thin"] = round(pnl_thin, 2)
            if thin_n >= int(mem_cfg.get("venue_thin_min_entries", 2) or 2) and pnl_thin < 0:
                mult *= _cfg_f(mem_cfg, "venue_thin_loss_mult", 1.2)
                reasons.append("venue_thin_loss")

    factors["memory_mult"] = round(mult, 4)
    return mult, reasons, factors


def _clamp_hours(h: float, lo: float, hi: float) -> float:
    if hi < lo:
        hi = lo
    return max(lo, min(hi, h))


def resolve_rebuy_cooldown_hours(
    *,
    regime: str | None = None,
    last_sell_signal: str = "",
    last_exit_source: str | None = None,
    order_signal: str | None = None,
    volatility_tier: str | None = None,
    signal_quality: str = "default",
    profile: Any | None = None,
    config: dict | None = None,
    block_rebuy_if_last_sell_was_stop: bool | None = None,
    fallback_hours: float = 4.0,
) -> RebuyCooldownResult:
    """Pure hours resolver. Caller enforces elapsed < hours."""
    cfg = dict(config or {})
    reasons: list[str] = []
    factors: dict[str, Any] = {}

    exit_label = pick_exit_label(
        last_exit_source=last_exit_source,
        last_sell_signal=last_sell_signal,
        order_signal=order_signal,
    )
    stop_sell = is_hard_stop_sell(exit_label) or is_hard_stop_sell(last_sell_signal)
    factors.update(
        {
            "last_sell_signal": str(last_sell_signal or ""),
            "last_exit_source": str(last_exit_source or ""),
            "exit_label": exit_label,
            "stop_sell": stop_sell,
        }
    )

    block_stop = (
        bool(cfg.get("block_rebuy_if_last_sell_was_stop", True))
        if block_rebuy_if_last_sell_was_stop is None
        else bool(block_rebuy_if_last_sell_was_stop)
    )
    if stop_sell and block_stop:
        hours = _cfg_f(cfg, "stop_loss_hours", 24.0)
        factors["hours"] = hours
        return RebuyCooldownResult(
            hours=hours, reasons=["stop_loss"], factors=factors, stop_sell=True
        )

    if not cfg.get("enabled"):
        hours = float(fallback_hours)
        factors["hours"] = hours
        return RebuyCooldownResult(
            hours=hours, reasons=["disabled_fallback"], factors=factors
        )

    rk = _regime_key(regime)
    base_map = cfg.get("base_hours_by_regime") if isinstance(cfg.get("base_hours_by_regime"), dict) else {}
    base = _cfg_f(base_map, rk, _cfg_f(base_map, "NEUTRAL", fallback_hours))
    factors["regime"] = rk
    factors["base_hours"] = base
    reasons.append(f"regime={rk}")

    qkey = str(signal_quality or "default")
    qmult = _map_mult(cfg.get("quality_mult"), qkey, 1.0)
    factors["quality"] = qkey
    factors["quality_mult"] = qmult

    vt = str(volatility_tier or "").strip().lower() or "default"
    vol_table = cfg.get("vol_tier_mult") if isinstance(cfg.get("vol_tier_mult"), dict) else {}
    vmult = _map_mult(vol_table, vt, 1.0) if vt in vol_table else 1.0
    factors["vol_tier"] = vt
    factors["vol_tier_mult"] = vmult

    ekey = normalize_exit_key(exit_label, last_sell_signal, last_exit_source or "")
    emult = _map_mult(cfg.get("exit_source_mult"), ekey, 1.0)
    factors["exit_key"] = ekey
    factors["exit_mult"] = emult
    reasons.append(f"exit={ekey}")

    mem_cfg = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
    mmult, mreasons, mfactors = resolve_memory_mult(profile, mem_cfg, exit_key=ekey)
    reasons.extend(mreasons)
    factors.update(mfactors)

    h = base * qmult * vmult * mmult * emult
    lo = _cfg_f(cfg, "min_hours", 0.75)
    hi = _cfg_f(cfg, "max_hours", 8.0)
    h = _clamp_hours(h, lo, hi)

    # Floors may exceed max_hours (soft_block / gross loss)
    if str(factors.get("entry_bias") or "") == "soft_block":
        floor_sb = _cfg_f(mem_cfg, "soft_block_min_hours", 4.0)
        if h < floor_sb:
            h = floor_sb
            reasons.append("soft_block_floor")

    feats = getattr(profile, "features", None) if profile is not None else None
    if isinstance(feats, dict) and mem_cfg.get("enabled", True):
        worst_u = abs(_num(feats.get("worst_loss_usdt")))
        worst_p = abs(_num(feats.get("worst_loss_pct")))
        thr_u = _cfg_f(mem_cfg, "gross_loss_usdt", 500.0)
        thr_p = abs(_cfg_f(mem_cfg, "gross_loss_pct", -8.0))
        if (feats.get("last_loss_at") or feats.get("soft_block_until")) and (
            worst_u >= thr_u or worst_p >= thr_p
        ):
            gl_h = _cfg_f(mem_cfg, "gross_loss_cooloff_hours", 12.0)
            if h < gl_h:
                h = gl_h
                reasons.append("gross_loss_floor")

    factors["hours"] = round(h, 4)
    reasons.append(f"hours={h:.2f}")
    return RebuyCooldownResult(hours=float(h), reasons=reasons, factors=factors, stop_sell=False)


def format_rebuy_reject_message(*, elapsed_h: float, result: RebuyCooldownResult) -> str:
    label = "Stop-loss rebuy cooldown" if result.stop_sell else "Rebuy cooldown"
    f = result.factors
    bits = []
    if f.get("regime"):
        bits.append(f"regime={f['regime']}")
    if f.get("exit_key"):
        bits.append(f"exit={f['exit_key']}")
    if "win_rate" in f:
        bits.append(f"wr={f['win_rate']}")
    if "sells_30d" in f:
        bits.append(f"n={f['sells_30d']}")
    bias = f.get("entry_bias")
    if bias and bias != "neutral":
        bits.append(f"bias={bias}")
    extra = f"; {' '.join(bits)}" if bits else ""
    return (
        f"{label}: {elapsed_h:.1f}h since last SELL "
        f"(min {result.hours:.1f}h after sell{extra})"
    )


def prepare_dynamic_config(cfg: dict, arch: dict) -> dict:
    """Merge architecture stop defaults into a copy of rebuy_cooldown cfg."""
    out = dict(cfg)
    if "block_rebuy_if_last_sell_was_stop" not in out:
        out["block_rebuy_if_last_sell_was_stop"] = bool(
            arch.get("block_rebuy_if_last_sell_was_stop", True)
        )
    out.setdefault(
        "stop_loss_hours",
        float(arch.get("rebuy_after_stop_loss_hours", 24.0) or 24.0),
    )
    return out


def signal_quality_from_confidence(confidence: Any, high_threshold: float = 75.0) -> str:
    """Map order confidence → quality bucket for quality_mult."""
    try:
        if confidence is not None and float(confidence) >= high_threshold:
            return "high_conviction_entry"
    except (TypeError, ValueError):
        pass
    return "default"
