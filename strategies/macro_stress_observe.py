"""#625 Macro/Stress pre-buy observe gate (shadow).

Pure function. Reads Fusion/oracle regime and macro multipliers (Ist).
Logs proposals only. Never places orders, never changes size, never
calls RiskManager. A later hard-block vs size-cut is out of this ticket.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, TypedDict

from core.actions import BUY_DCA, is_buy
from logger import log

REASON_CODE = "macro_stress_observe"
_STRESS_REGIMES = frozenset({"RISK_OFF", "CRASH"})
_CONFIG_KEY = "macro_stress_observe"
_WARN_INTERVAL_SEC = 60.0
_last_warn_at: dict[str, float] = {}

GetBias = Callable[[dict | None], Mapping[str, Any]]
GetMultipliers = Callable[[dict | None], Mapping[str, Any]]


class MacroStressObserveRecord(TypedDict):
    reason: str
    regime: str | None
    calendar_mult: float
    session_mult: float
    pm_mult: float
    block_new_entries: bool
    would_block: bool
    would_cut: bool
    observe_enabled: bool
    fire_enabled: bool
    sources_ok: bool
    bias_error: bool
    mults_error: bool


def macro_stress_observe_config(config: dict | None) -> dict[str, bool]:
    """Viktor R1 defaults: both flags false. Not set in config.json."""
    raw = (config or {}).get(_CONFIG_KEY)
    if not isinstance(raw, dict):
        raw = {}
    return {
        "observe_enabled": bool(raw.get("observe_enabled", False)),
        "fire_enabled": bool(raw.get("fire_enabled", False)),
    }


def _reset_ist_warn_for_tests() -> None:
    _last_warn_at.clear()


def _warn_ist_failure(source: str, detail: object) -> None:
    now = time.monotonic()
    last = _last_warn_at.get(source, 0.0)
    if now - last < _WARN_INTERVAL_SEC:
        return
    _last_warn_at[source] = now
    log(f"[macro_stress_observe] {source} read failed: {detail}", "WARNING")


def _float_mult(value: Any, *, field: str, default: float = 1.0) -> tuple[float, bool]:
    try:
        if value is None:
            return default, True
        return float(value), True
    except (TypeError, ValueError) as exc:
        _warn_ist_failure("multipliers", f"{field}={value!r}: {exc}")
        return default, False


def _is_first_buy(*, has_position: bool, action: str) -> bool:
    return (not has_position) and is_buy(action) and action != BUY_DCA


def observe_macro_stress(
    config: dict | None,
    *,
    has_position: bool = False,
    action: str = "BUY",
    get_bias: GetBias | None = None,
    get_multipliers: GetMultipliers | None = None,
) -> MacroStressObserveRecord | None:
    """Build the shadow observe record, or None when observe_enabled is false.

    Ist sources (injectable):
      - services.market_policy_fusion.get_global_market_bias(config)["regime"]
      - intelligence.macro.snapshot.get_risk_multipliers(config)

    would_block / would_cut are proposals only. fire_enabled is logged and
    never applied here. This layer does not widen size under RISK_OFF.
    """
    flags = macro_stress_observe_config(config)
    if not flags["observe_enabled"]:
        return None

    if get_bias is None:
        from services.market_policy_fusion import get_global_market_bias as get_bias
    if get_multipliers is None:
        from intelligence.macro.snapshot import get_risk_multipliers as get_multipliers

    bias_error = False
    try:
        bias = dict(get_bias(config) or {})
    except Exception as exc:
        _warn_ist_failure("bias", exc)
        bias = {}
        bias_error = True
    mults_error = False
    try:
        mm = dict(get_multipliers(config) or {})
    except Exception as exc:
        _warn_ist_failure("multipliers", exc)
        mm = {}
        mults_error = True

    regime_raw = bias.get("regime")
    regime = str(regime_raw) if regime_raw else None
    calendar_mult, cal_ok = _float_mult(mm.get("calendar_mult"), field="calendar_mult")
    session_mult, sess_ok = _float_mult(mm.get("session_mult"), field="session_mult")
    pm_mult, pm_ok = _float_mult(mm.get("pm_mult"), field="pm_mult")
    if not (cal_ok and sess_ok and pm_ok):
        mults_error = True
    block_new_entries = bool(mm.get("block_new_entries", False))
    sources_ok = not bias_error and not mults_error

    first_buy = _is_first_buy(has_position=has_position, action=action)
    # Proposal only: empty-book non-DCA first buy + fusion RISK_OFF/CRASH.
    would_block = first_buy and regime in _STRESS_REGIMES
    # Proposal only: true iff an Ist multiplier is below 1.0. Never a widen.
    would_cut = any(m < 1.0 for m in (calendar_mult, session_mult, pm_mult))

    return {
        "reason": REASON_CODE,
        "regime": regime,
        "calendar_mult": calendar_mult,
        "session_mult": session_mult,
        "pm_mult": pm_mult,
        "block_new_entries": block_new_entries,
        "would_block": would_block,
        "would_cut": would_cut,
        "observe_enabled": True,
        "fire_enabled": flags["fire_enabled"],
        "sources_ok": sources_ok,
        "bias_error": bias_error,
        "mults_error": mults_error,
    }


def format_macro_stress_observe_log(
    record: Mapping[str, Any],
    *,
    symbol: str = "",
) -> str:
    return (
        f"[{REASON_CODE}] {symbol} "
        f"reason={record.get('reason')} "
        f"regime={record.get('regime')} "
        f"calendar_mult={record.get('calendar_mult')} "
        f"session_mult={record.get('session_mult')} "
        f"pm_mult={record.get('pm_mult')} "
        f"block_new_entries={record.get('block_new_entries')} "
        f"would_block={record.get('would_block')} "
        f"would_cut={record.get('would_cut')} "
        f"observe_enabled={record.get('observe_enabled')} "
        f"fire_enabled={record.get('fire_enabled')} "
        f"sources_ok={record.get('sources_ok')} "
        f"bias_error={record.get('bias_error')} "
        f"mults_error={record.get('mults_error')}"
    )
