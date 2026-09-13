"""Schema + bounds for trading-critical ``config.json`` keys (#330, slice 1).

``data_manager.save_config`` calls :func:`validate_config_for_save` before it
writes anything (default tenant → ``config.json``; other tenants → Mongo via
``tenant_meta_store``). An invalid value raises :class:`ConfigValidationError`
and nothing is written.

Only keys that are *present* are checked — partial tenant bodies such as
``{"virtual_trading": True}`` are valid. Only keys the code already reads are
guarded; no new keys are invented. Units follow the existing consumers:

* ``costs.*.fee_*_pct`` / ``slippage_pct`` / ``slippage_by_tier.*`` are percent
  of notional (``core/costs.py`` divides by 100) → bounded to ``[0, 100]``.
* ``risk.*_pct`` keys are percent → ``[0, 100]``.
* ``*_usdt`` caps and multipliers are non-negative finite numbers.
* ``max_open_positions`` (and ``risk.position_capacity.*``) are ints in
  ``[0, MAX_OPEN_POSITIONS_CAP]``.
"""

from __future__ import annotations

import math
from typing import Any, Callable

# Enums the code already reads (see core/execution_mode.py, core/config.py).
# "off" is the kill switch written by ``/mode off``
# (notifications/telegram_commands/mode_commands.py); rejecting it would leave
# the bot in its previous mode.
TRADING_MODES: frozenset[str] = frozenset(("live", "paper", "demo", "gate_testnet", "off"))
LIVE_EXECUTION_MODES: frozenset[str] = frozenset(("shadow", "testnet", "real"))

# Live config runs 36 with position_capacity.max_ceiling 44; 200 leaves
# headroom without accepting a typo like 3600.
MAX_OPEN_POSITIONS_CAP = 200

_MISSING = object()


class ConfigValidationError(ValueError):
    """Raised by ``save_config`` when a trading-critical key is out of bounds."""

    def __init__(self, path: str, value: Any, reason: str):
        self.path = path
        self.value = value
        self.reason = reason
        super().__init__(f"config.{path}={value!r} rejected: {reason}")


def _get_path(cfg: dict, path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(path: str, value: Any) -> float:
    if not _is_number(value):
        raise ConfigValidationError(path, value, "must be a number")
    if not math.isfinite(value):
        raise ConfigValidationError(path, value, "must be finite")
    return float(value)


def _int_in_range(lo: int, hi: int) -> Callable[[str, Any], None]:
    def check(path: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            if _is_number(value) and float(value).is_integer():
                value = int(value)
            else:
                raise ConfigValidationError(path, value, "must be an integer")
        if not (lo <= value <= hi):
            raise ConfigValidationError(path, value, f"must be in [{lo}, {hi}]")

    return check


def _number_in_range(lo: float, hi: float) -> Callable[[str, Any], None]:
    def check(path: str, value: Any) -> None:
        num = _finite_number(path, value)
        if not (lo <= num <= hi):
            raise ConfigValidationError(path, value, f"must be in [{lo:g}, {hi:g}]")

    return check


def _positive_number(path: str, value: Any) -> None:
    num = _finite_number(path, value)
    if num <= 0:
        raise ConfigValidationError(path, value, "must be > 0")


def _non_negative_number(path: str, value: Any) -> None:
    num = _finite_number(path, value)
    if num < 0:
        raise ConfigValidationError(path, value, "must be >= 0")


def _enum(allowed: frozenset[str], *, allow_unset: bool = False) -> Callable[[str, Any], None]:
    """Exact match only. Consumers compare case-sensitively (``risk_manager``,
    registry ``live_enabled``), so ``"LIVE"`` / ``" paper "`` are rejected, not
    normalized — a rewrite-on-save would certify a value the consumers split on.
    """

    def check(path: str, value: Any) -> None:
        if allow_unset and (value is None or (isinstance(value, str) and value.strip() == "")):
            return
        if not isinstance(value, str) or value not in allowed:
            raise ConfigValidationError(
                path, value, "must be one of " + "|".join(sorted(allowed))
            )

    return check


_int_positions = _int_in_range(0, MAX_OPEN_POSITIONS_CAP)
_int_non_negative = _int_in_range(0, 10**7)
_pct = _number_in_range(0.0, 100.0)

# path → validator. Only paths that already exist in config.json / consumers.
GUARDED_KEYS: dict[str, Callable[[str, Any], None]] = {
    "max_open_positions": _int_positions,
    "max_usdt_per_trade": _positive_number,
    "live.max_usdt_per_trade": _positive_number,
    "trading_mode": _enum(TRADING_MODES),
    "live.execution": _enum(LIVE_EXECUTION_MODES),
    # costs.* — percent of notional (core/costs.py divides by 100)
    "costs.gate.spot.fee_maker_pct": _pct,
    "costs.gate.spot.fee_taker_pct": _pct,
    "costs.gate.spot.slippage_pct": _pct,
    "costs.gate.swap.fee_maker_pct": _pct,
    "costs.gate.swap.fee_taker_pct": _pct,
    "costs.gate.swap.slippage_pct": _pct,
    "costs.slippage_by_tier.volatile": _pct,
    "costs.slippage_by_tier.mid": _pct,
    "costs.slippage_by_tier.stable": _pct,
    # risk.* — existing caps / limits (risk_manager, position_capacity, slot_eviction)
    "risk.max_daily_loss_pct": _pct,
    "risk.cash_floor_pct": _pct,
    "risk.dca_reserve_pct": _pct,
    "risk.drawdown_throttle_pct": _pct,
    "risk.max_daily_buys": _int_non_negative,
    "risk.max_daily_dca_buys": _int_non_negative,
    "risk.max_daily_sells": _int_non_negative,
    "risk.max_daily_dca_usdt": _non_negative_number,
    "risk.min_trade_usdt": _non_negative_number,
    "risk.min_sell_notional_usdt": _non_negative_number,
    "risk.position_capacity.base": _int_positions,
    "risk.position_capacity.min_floor": _int_positions,
    "risk.position_capacity.max_ceiling": _int_positions,
    "risk.slot_eviction.max_evict_notional_usdt": _non_negative_number,
    "risk.slot_eviction.max_evictions_per_hour": _int_non_negative,
    "risk.slot_eviction.max_evictions_per_day": _int_non_negative,
    "risk.moderate_deploy.max_total_multiplier": _non_negative_number,
    "risk.moderate_deploy.max_boost": _non_negative_number,
    "risk.cash_policy.floor_pct_base": _pct,
    "risk.cash_policy.floor_pct_min": _pct,
    "risk.cash_policy.floor_pct_max": _pct,
}


def validate_config_for_save(config: Any) -> None:
    """Raise :class:`ConfigValidationError` on the first out-of-bounds key.

    ``config`` must be a dict; missing keys are fine, ``None`` is treated as
    "unset" only for keys that consumers already default (all guarded keys
    use ``.get(key, default)``), so ``None`` is rejected to keep the written
    file unambiguous.
    """
    if not isinstance(config, dict):
        raise ConfigValidationError("<root>", type(config).__name__, "config must be a dict")
    for path, check in GUARDED_KEYS.items():
        value = _get_path(config, path)
        if value is _MISSING:
            continue
        check(path, value)
