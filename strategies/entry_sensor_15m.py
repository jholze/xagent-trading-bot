"""Pure 15m volume/movement entry sensor — no positions, no sells."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.actions import BUY, BUY_STRONG

ENTRY_SENSOR_SOURCE = "entry_sensor_15m"

_pending_results: dict[str, "EntrySensor15mResult"] = {}
_pending_metrics: dict[str, dict] = {}


@dataclass
class EntrySensor15mResult:
    triggered: bool
    action: str = "HOLD"
    confidence_boost: float = 0.0
    rationale: str = ""
    shadow_only: bool = True
    volume_spike_ratio: float = 0.0


def set_pending_sensor_metrics(symbol: str, metrics: dict) -> None:
    """Loop hands off fresh 15m metrics; DecisionEngine re-evaluates with live 4h RSI."""
    _pending_metrics[symbol] = dict(metrics)


def consume_pending_sensor_metrics(symbol: str) -> dict | None:
    return _pending_metrics.pop(symbol, None)


def set_pending_sensor_result(symbol: str, result: EntrySensor15mResult) -> None:
    """Test helper — prefer set_pending_sensor_metrics in production paths."""
    _pending_results[symbol] = result


def consume_pending_sensor_result(symbol: str) -> EntrySensor15mResult | None:
    return _pending_results.pop(symbol, None)


def clear_pending_for_tests() -> None:
    _pending_results.clear()
    _pending_metrics.clear()


def evaluate_entry_sensor_15m(
    *,
    watched: bool,
    metrics: dict | None,
    cfg: dict,
    rsi_4h: float,
    hours_since_reject: float | None = None,
    tech_already_buy: bool = False,
    now: datetime | None = None,
) -> EntrySensor15mResult:
    """Evaluate 15m sensor; pure function — no I/O or position side effects."""
    _ = now
    mode = str(cfg.get("mode", "shadow")).strip().lower()
    shadow_only = mode != "active"

    if not cfg.get("enabled", True):
        return EntrySensor15mResult(triggered=False, shadow_only=shadow_only)
    if not watched or not metrics:
        return EntrySensor15mResult(triggered=False, shadow_only=shadow_only)

    cooldown_h = float(cfg.get("cooldown_after_reject_hours", 2))
    if hours_since_reject is not None and hours_since_reject < cooldown_h:
        return EntrySensor15mResult(
            triggered=False,
            shadow_only=shadow_only,
            rationale=f"cooldown {hours_since_reject:.1f}h < {cooldown_h}h",
        )

    vol_mult = float(cfg.get("vol_spike_mult", 2.0))
    spike = float(metrics.get("volume_spike_ratio", 0))
    if spike < vol_mult:
        return EntrySensor15mResult(
            triggered=False,
            shadow_only=shadow_only,
            volume_spike_ratio=spike,
            rationale=f"vol spike {spike:.2f}x < {vol_mult}x",
        )

    rsi_cap = float(cfg.get("block_buy_if_rsi_4h_above", 75))
    if rsi_4h > rsi_cap:
        return EntrySensor15mResult(
            triggered=False,
            shadow_only=shadow_only,
            volume_spike_ratio=spike,
            rationale=f"4h RSI {rsi_4h:.1f} > {rsi_cap}",
        )

    min_body = float(cfg.get("fakeout_min_body_atr_ratio", 0.3))
    body_ratio = float(metrics.get("body_atr_ratio", 0))
    if body_ratio < min_body:
        return EntrySensor15mResult(
            triggered=False,
            shadow_only=shadow_only,
            volume_spike_ratio=spike,
            rationale=f"body/atr {body_ratio:.2f} < {min_body}",
        )

    if cfg.get("require_ema_breakout", False) and not metrics.get("price_momentum"):
        return EntrySensor15mResult(
            triggered=False,
            shadow_only=shadow_only,
            volume_spike_ratio=spike,
            rationale="EMA breakout required but not met",
        )

    action = BUY_STRONG if tech_already_buy else BUY
    boost = min(15.0, (spike - vol_mult) * 5.0)
    rationale = f"15m vol spike {spike:.2f}x"
    if metrics.get("price_momentum"):
        rationale += ", EMA9 breakout"

    return EntrySensor15mResult(
        triggered=True,
        action=action,
        confidence_boost=boost,
        rationale=rationale,
        shadow_only=shadow_only,
        volume_spike_ratio=spike,
    )