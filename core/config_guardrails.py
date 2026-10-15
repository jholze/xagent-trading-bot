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


# Acceptance ceiling for risk.long_hard_stop_max_loss_pct (#695).
# The live stop comes only from the config value. This bound is the reject
# line: missing, non-numeric, <= 0, or above the ceiling is invalid.
LONG_HARD_STOP_CAP_PATH = "risk.long_hard_stop_max_loss_pct"
LONG_HARD_STOP_CAP_MAX = 50.0


# Operator-pinned shorts keys: Hermes / patch_config must not mention them (#614).
# Leaf names kept for callers that inspect the set. The pin walks dotted paths
# below; extending this set alone does not pin anything.
FROZEN_SHORTS_PATCH_KEYS = frozenset(
    ("allow_live", "leverage_cap", "auto_after_sell", "climax_fade")
)

# Save pin and patch pin share this list. Floor paths are added only where
# R6 says so (patch: every tenant; save: default branch only).
PINNED_SHORTS_PATHS = (
    "shorts.allow_live",
    "shorts.leverage_cap",
    "shorts.auto_after_sell",
    "shorts.climax_fade",
)

_PIN_REASON = "operator-pinned; Hermes cannot write"


def _liquidity_floor_paths() -> tuple[str, str]:
    from services.venue_quality import DEPTH_FLOOR_PATH, VOLUME_FLOOR_PATH

    return (DEPTH_FLOOR_PATH, VOLUME_FLOOR_PATH)


def pinned_patch_paths() -> tuple[str, ...]:
    """Paths a patch body must not mention (shorts plus the two floors)."""
    return PINNED_SHORTS_PATHS + _liquidity_floor_paths()


def reject_pinned_patch(updates: Any, paths: tuple[str, ...] | list[str]) -> None:
    """Raise if ``updates`` contains any pinned path."""
    if not isinstance(updates, dict):
        return
    for path in paths:
        value = _get_path(updates, path)
        if value is _MISSING:
            continue
        raise ConfigValidationError(path, value, _PIN_REASON)


def reject_frozen_shorts_patch(updates: Any) -> None:
    """Raise if a patch body writes a pinned shorts or liquidity-floor path."""
    reject_pinned_patch(updates, pinned_patch_paths())


def frozen_shorts_changed(
    new_cfg: Any,
    previous: Any,
    paths: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    """Return the path if a pinned value changed vs ``previous``.

    ``paths`` defaults to the shorts save list. Callers add floor paths only
    on the default-tenant save (R6). Present-before / missing-now is a change.
    """
    if not isinstance(new_cfg, dict):
        return None
    prev = previous if isinstance(previous, dict) else {}
    checked = PINNED_SHORTS_PATHS if paths is None else paths
    for path in checked:
        nv = _get_path(new_cfg, path)
        pv = _get_path(prev, path)
        # Present-before / missing-now is a change: a /config restore of a
        # pre-#614 snapshot must not drop shorts.auto_after_sell (default true).
        if pv is not _MISSING and nv is _MISSING:
            return path
        if nv is not _MISSING and pv is not _MISSING and nv != pv:
            return path
    return None


def _base_liquidity_floors() -> dict[str, float]:
    """Floors from base config.json. Unreadable or invalid rejects the save (R7)."""
    from services.venue_quality import parse_liquidity_floor

    import data_manager

    floor_paths = _liquidity_floor_paths()
    try:
        loaded = data_manager._load_default_config_from_disk()
    except Exception as exc:
        raise ConfigValidationError(
            floor_paths[0],
            None,
            "liquidity guard floor missing",
        ) from exc
    if not isinstance(loaded, dict):
        raise ConfigValidationError(
            floor_paths[0],
            type(loaded).__name__,
            "liquidity guard floor missing",
        )
    floors: dict[str, float] = {}
    for path in floor_paths:
        raw = _get_path(loaded, path)
        parsed = None if raw is _MISSING else parse_liquidity_floor(raw)
        if parsed is None:
            shown = None if raw is _MISSING else raw
            raise ConfigValidationError(path, shown, "liquidity guard floor missing")
        floors[path] = parsed
    return floors


def assert_liquidity_multiples_within_base_floor(config: Any) -> None:
    """Reject a present multiple that is not a finite number, or sits below the base floor.

    The floor always comes from disk, never from ``config``. A base read
    error or an invalid base floor rejects the save. A present multiple must
    be an int or float (not bool), finite, and at least the base floor.
    ``True == 1`` must not let a bool through. Strings and NaN are rejected
    here; the buy lock already fail-closes them.
    """
    from services.venue_quality import (
        DEPTH_FLOOR_PATH,
        DEPTH_MULTIPLE_PATH,
        VOLUME_FLOOR_PATH,
        VOLUME_MULTIPLE_PATH,
    )

    if not isinstance(config, dict):
        return
    floors = _base_liquidity_floors()
    pairs = (
        (DEPTH_MULTIPLE_PATH, DEPTH_FLOOR_PATH),
        (VOLUME_MULTIPLE_PATH, VOLUME_FLOOR_PATH),
    )
    for multiple_path, floor_path in pairs:
        raw = _get_path(config, multiple_path)
        if raw is _MISSING:
            continue
        # Bool and str are not numbers. NaN/inf are not usable multiples.
        # Compare only after that, so True is not treated as 1.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ConfigValidationError(
                multiple_path, raw, "liquidity guard multiple invalid"
            )
        number = float(raw)
        floor = floors[floor_path]
        if number <= 0 or number < floor:
            raise ConfigValidationError(multiple_path, raw, f"below floor {floor}")


def assert_default_config_floors_present(config: Any) -> None:
    """Default-branch presence rule (R7): both floors in the body, valid per R3."""
    from services.venue_quality import parse_liquidity_floor

    if not isinstance(config, dict):
        raise ConfigValidationError("<root>", type(config).__name__, "config must be a dict")
    for path in _liquidity_floor_paths():
        raw = _get_path(config, path)
        parsed = None if raw is _MISSING else parse_liquidity_floor(raw)
        if parsed is None:
            shown = None if raw is _MISSING else raw
            raise ConfigValidationError(path, shown, "liquidity guard floor missing")


def _long_hard_stop_cap_reason(config: Any) -> str | None:
    """Reason the cap is unusable, or None when it is a number in (0, 50]."""
    if not isinstance(config, dict):
        return "config must be a dict"
    value = _get_path(config, LONG_HARD_STOP_CAP_PATH)
    if value is _MISSING:
        return "missing"
    if not _is_number(value):
        return "must be a number"
    num = float(value)
    if not math.isfinite(num) or num <= 0 or num > LONG_HARD_STOP_CAP_MAX:
        return f"must be > 0 and <= {LONG_HARD_STOP_CAP_MAX:g}"
    return None


def long_hard_stop_cap_error(config: Any) -> str | None:
    """None when ``risk.long_hard_stop_max_loss_pct`` is valid.

    There is no code fallback. Callers that start the bot, reload config, or
    apply a tenant overlay treat any other return as fail-closed.
    """
    reason = _long_hard_stop_cap_reason(config)
    if reason:
        return reason
    return None


def _guard_long_hard_stop_cap(path: str, value: Any) -> None:
    """Save-path check for a present cap. Absence stays valid for partial bodies."""
    probe = {"risk": {"long_hard_stop_max_loss_pct": value}}
    err = _long_hard_stop_cap_reason(probe)
    if err and err != "missing":
        raise ConfigValidationError(path, value, err)


# Present values only. A missing key is rejected at start, reload, and
# tenant-overlay load (#695), not on a partial tenant save body.
GUARDED_KEYS["risk.long_hard_stop_max_loss_pct"] = _guard_long_hard_stop_cap


def stop_cap_applies(config: Any) -> bool:
    """True for a bot config that carries a top-level ``stop_loss_pct``.

    Partial stubs in unit tests do not, and are not this gate.
    """
    return isinstance(config, dict) and "stop_loss_pct" in config


def stop_cap_is_valid(config: Any) -> bool:
    return isinstance(config, dict) and long_hard_stop_cap_error(config) is None


def refuse_start_if_stop_cap_invalid(config: Any) -> None:
    """Process start: a missing or invalid cap aborts before the bot runs."""
    err = long_hard_stop_cap_error(config)
    if err:
        from logger import log

        log(
            "config risk.long_hard_stop_max_loss_pct rejected at start: "
            f"{err}",
            "ERROR",
        )
        raise SystemExit(1)


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
    # #631: shadow/enforce membership revise requires the named Ersatz-Cap.
    # Absent section and mode=off stay valid. Lazy import avoids a cycle.
    from services.universe.membership_revise import assert_membership_revise_config

    assert_membership_revise_config(config)
    assert_liquidity_multiples_within_base_floor(config)
