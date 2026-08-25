"""P1 exit sensor — early weakness signals before fixed gain thresholds."""

from __future__ import annotations

from dataclasses import dataclass

from core.actions import SELL_PARTIAL_20
from core.models import MarketContext


EXIT_SENSOR_SOURCES = frozenset({
    "exit_15m_weakness",
    "exit_volume_climax",
    "exit_pullback",
    "exit_btc_rs",
    "exit_1h_rsi_rollover",
})


@dataclass
class ExitSensorCandidate:
    action: str
    source: str
    priority: int
    rationale: str
    shadow_only: bool = False


def exit_sensor_config(config: dict | None = None) -> dict:
    from core.config import get_bot_config

    if config is None:
        return get_bot_config().exit_sensor_config
    defaults = get_bot_config().exit_sensor_config
    raw = config.get("exit_sensor") or {}
    merged = {**defaults, **raw}
    for key in (
        "weakness_15m",
        "volume_climax",
        "pullback",
        "btc_rs",
        "rsi_rollover_1h",
    ):
        if key in raw:
            merged[key] = {**defaults.get(key, {}), **raw[key]}
    return merged


def _gain_pct(market: MarketContext) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    return (market.current_price / entry - 1) * 100


def _drop_from_high_pct(market: MarketContext, position: dict) -> float:
    recent_high = float(position.get("recent_high") or 0) or market.current_price
    if recent_high <= 0:
        return 0.0
    return (1 - market.current_price / recent_high) * 100


def _near_recent_high(market: MarketContext, position: dict, tolerance_pct: float) -> bool:
    recent_high = float(position.get("recent_high") or 0) or market.current_price
    if recent_high <= 0:
        return False
    floor = recent_high * (1 - tolerance_pct / 100.0)
    return market.current_price >= floor


def _shadow_only(cfg: dict) -> bool:
    mode = str(cfg.get("mode", "shadow")).strip().lower()
    return mode not in ("live", "active")


def _candidate(
    source: str,
    rationale: str,
    *,
    shadow: bool,
    priority: int = 4,
) -> ExitSensorCandidate:
    return ExitSensorCandidate(
        action=SELL_PARTIAL_20,
        source=source,
        priority=priority,
        rationale=rationale,
        shadow_only=shadow,
    )


def evaluate_exit_sensor_sells(
    market: MarketContext,
    position: dict,
    cfg: dict,
    *,
    metrics_15m: dict | None,
    metrics_1h: dict | None,
    btc_rs_delta: float | None,
    config_raw: dict | None = None,
) -> list[ExitSensorCandidate]:
    if not cfg.get("enabled", True):
        return []
    if not market.has_position or market.average_entry <= 0:
        return []

    shadow = _shadow_only(cfg)
    gain = _gain_pct(market)
    candidates: list[ExitSensorCandidate] = []

    wcfg = cfg.get("weakness_15m") or {}
    if wcfg.get("enabled", True) and metrics_15m:
        min_gain = float(wcfg.get("min_gain_pct", cfg.get("min_gain_pct", 7)))
        if (
            gain >= min_gain
            and metrics_15m.get("lower_high")
            and metrics_15m.get("close_below_ema")
        ):
            candidates.append(
                _candidate(
                    "exit_15m_weakness",
                    f"15m weakness (lower high, close<EMA, gain={gain:.1f}%)",
                    shadow=shadow,
                )
            )

    vcfg = cfg.get("volume_climax") or {}
    if vcfg.get("enabled", True) and metrics_15m:
        min_gain = float(vcfg.get("min_gain_pct", cfg.get("min_gain_pct", 7)))
        near_pct = float(vcfg.get("near_high_tolerance_pct", 2.0))
        if gain >= min_gain and _near_recent_high(market, position, near_pct):
            vol_spike = float(metrics_15m.get("volume_spike_ratio", 0) or 0)
            wick = float(metrics_15m.get("upper_wick_pct", 0) or 0)
            body = float(metrics_15m.get("body_atr_ratio", 1) or 1)
            if (
                vol_spike >= float(vcfg.get("vol_spike_min", 3.0))
                and wick >= float(vcfg.get("upper_wick_min_pct", 55))
                and body <= float(vcfg.get("max_body_atr_ratio", 0.35))
            ):
                candidates.append(
                    _candidate(
                        "exit_volume_climax",
                        f"Vol climax (spike={vol_spike:.1f}x, wick={wick:.0f}%, gain={gain:.1f}%)",
                        shadow=shadow,
                    )
                )

    pcfg = cfg.get("pullback") or {}
    if pcfg.get("enabled", True) and metrics_15m:
        min_gain = float(pcfg.get("min_gain_pct", 6))
        drop = _drop_from_high_pct(market, position)
        vol_ok = (
            not pcfg.get("require_vol_above_avg", True)
            or bool(metrics_15m.get("vol_above_avg"))
        )
        if gain >= min_gain and drop >= float(pcfg.get("min_drop_pct", 3.5)) and vol_ok:
            candidates.append(
                _candidate(
                    "exit_pullback",
                    f"Pullback from high (-{drop:.1f}%, gain={gain:.1f}%)",
                    shadow=shadow,
                )
            )

    bcfg = cfg.get("btc_rs") or {}
    if bcfg.get("enabled", True) and btc_rs_delta is not None:
        min_gain = float(bcfg.get("min_gain_pct", cfg.get("min_gain_pct", 7)))
        threshold = float(bcfg.get("min_underperformance_pct", 2.0))
        if gain >= min_gain and btc_rs_delta <= -threshold:
            candidates.append(
                _candidate(
                    "exit_btc_rs",
                    f"BTC RS lag (delta={btc_rs_delta:+.1f}%, gain={gain:.1f}%)",
                    shadow=shadow,
                )
            )

    rcfg = cfg.get("rsi_rollover_1h") or {}
    if rcfg.get("enabled", True) and metrics_1h:
        try:
            from strategies.indicator_regime import apply_rollover_overlay

            rcfg = apply_rollover_overlay(rcfg, config_raw)
        except Exception:
            pass
        min_gain = float(rcfg.get("min_gain_pct", cfg.get("min_gain_pct", 7)))
        peak_min = float(rcfg.get("peak_rsi_min", 70))
        current_max = float(rcfg.get("current_rsi_max", 60))
        rsi_cur = metrics_1h.get("rsi")
        rsi_peak = metrics_1h.get("rsi_peak_5")
        rollover = bool(metrics_1h.get("rsi_rollover"))
        try:
            if rsi_cur is not None and rsi_peak is not None:
                rollover = float(rsi_peak) >= peak_min and float(rsi_cur) < current_max
        except (TypeError, ValueError):
            pass
        if gain >= min_gain and rollover:
            candidates.append(
                _candidate(
                    "exit_1h_rsi_rollover",
                    (
                        f"1h RSI rollover (rsi={metrics_1h.get('rsi', 0):.0f}, "
                        f"peak={metrics_1h.get('rsi_peak_5', 0):.0f}, gain={gain:.1f}%)"
                    ),
                    shadow=shadow,
                )
            )

    return candidates