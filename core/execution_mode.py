"""Resolve live.execution → shadow | testnet | real.

``trading_mode`` stays ``live | paper | demo`` (#312 deliberate deviation from
phase1-kasse.md §3.1). ``live.dry_run: true`` is a deprecated alias for
``execution: shadow``. Asking for ``real`` never silently downgrades.

Single source of truth (#410): callers must not read ``live.dry_run`` to decide
whether fills are real money. Use ``resolve_execution_mode(cfg).places_real_orders``
/ ``.adapter_mode`` or the accounting helper ``places_real_orders(cfg)``;
``data_manager.is_live_dry_run`` and ``core.simulated_trading`` derive from it.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import Literal, Mapping

from logger import log

AdapterMode = Literal["shadow", "testnet", "real"]
_VALID_MODES: frozenset[str] = frozenset(("shadow", "testnet", "real"))
_SIMULATED_TRADING_MODES: frozenset[str] = frozenset(("paper", "demo", "gate_testnet"))

_DRY_RUN_WARNED = False


@dataclass(frozen=True)
class ExecutionMode:
    adapter_mode: AdapterMode
    places_real_orders: bool  # True only for "real"
    reason: str  # human-readable resolution trace


def _warn_dry_run_deprecated() -> None:
    global _DRY_RUN_WARNED
    if _DRY_RUN_WARNED:
        return
    _DRY_RUN_WARNED = True
    msg = (
        "live.dry_run is deprecated; use live.execution=\"shadow\" instead. "
        "dry_run=true is honoured as an alias for execution=shadow."
    )
    log(msg, "WARNING")
    warnings.warn(msg, DeprecationWarning, stacklevel=3)


def _shadow(reason: str) -> ExecutionMode:
    return ExecutionMode(adapter_mode="shadow", places_real_orders=False, reason=reason)


def _creds_present(live: dict, env: Mapping[str, str]) -> tuple[bool, str, str]:
    key_env = str(live.get("api_key_env") or "GATE_API_KEY")
    secret_env = str(live.get("api_secret_env") or "GATE_API_SECRET")
    has_key = bool(str(env.get(key_env, "") or "").strip())
    has_secret = bool(str(env.get(secret_env, "") or "").strip())
    return has_key and has_secret, key_env, secret_env


def places_real_orders(
    config_raw: dict,
    env: Mapping[str, str] = os.environ,
) -> bool:
    """Accounting truth (#410): True only when execution resolves to ``real``.

    Shadow and testnet are both not-real-money — testnet fills must never be
    booked as live PnL or valued against the real Gate spot wallet.

    ``live.dry_run: true`` short-circuits to False *before* delegating to
    ``resolve_execution_mode``: the deprecated alias forces shadow, and an
    unknown ``live.execution`` value must not flip accounting to real-money
    while the operator has dry_run on (resolve raises on the unknown value
    before it reaches the dry_run alias, so this check cannot live there).

    Fail-closed: when ``resolve_execution_mode`` refuses (``real`` without
    ``live_confirmed``/creds/with DEMO_MODE, ``testnet`` without creds, unknown
    value) and dry_run is not true, the mode is treated as real-money — nothing
    lands in the simulated ledger and every live gate stays in place (same
    direction as the risk manager's short gate). A WARNING is logged so the
    operator sees the unresolvable execution; the adapter factory still refuses
    to start there.
    """
    raw = config_raw if isinstance(config_raw, dict) else {}
    live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
    if live.get("dry_run") is True:
        return False
    try:
        return resolve_execution_mode(config_raw, env).places_real_orders
    except RuntimeError as exc:
        log(
            f"places_real_orders: execution mode unresolvable ({exc}); "
            "fail-closed → treating as real-money",
            "WARNING",
        )
        return True


def resolve_execution_mode(
    config_raw: dict,
    env: Mapping[str, str] = os.environ,
) -> ExecutionMode:
    raw = config_raw if isinstance(config_raw, dict) else {}
    trading_mode = str(raw.get("trading_mode") or "").strip().lower()
    live = raw.get("live") if isinstance(raw.get("live"), dict) else {}

    if trading_mode in _SIMULATED_TRADING_MODES:
        return _shadow(f"trading_mode={trading_mode} → shadow")
    if trading_mode != "live":
        return _shadow(f"trading_mode={trading_mode or 'unset'} → shadow")

    exec_raw = live.get("execution", None)
    if exec_raw is None or str(exec_raw).strip() == "":
        requested = "shadow"
        exec_source = "default"
    else:
        requested = str(exec_raw).strip().lower()
        exec_source = "key"

    if requested not in _VALID_MODES:
        raise RuntimeError(
            f"Unknown live.execution={exec_raw!r}; expected shadow|testnet|real"
        )

    if live.get("dry_run") is True:
        _warn_dry_run_deprecated()
        return _shadow(
            "trading_mode=live dry_run=true "
            "(deprecated alias for live.execution=shadow) → shadow"
        )

    if requested == "shadow":
        return _shadow(
            f"trading_mode=live execution=shadow ({exec_source}) → shadow"
        )

    has_creds, key_env, secret_env = _creds_present(live, env)
    demo = str(env.get("DEMO_MODE", "") or "") == "1"
    confirmed = bool(raw.get("live_confirmed", False))

    if requested == "testnet":
        if not has_creds:
            raise RuntimeError(
                f"live.execution=testnet requires {key_env} and {secret_env} to be set"
            )
        return ExecutionMode(
            adapter_mode="testnet",
            places_real_orders=False,
            reason="trading_mode=live execution=testnet creds=set → testnet",
        )

    # real — refuse to start if any guard fails (never silently downgrade)
    failed: list[str] = []
    if not confirmed:
        failed.append("live_confirmed is not true")
    if not has_creds:
        failed.append(f"{key_env} and {secret_env} must be set")
    if demo:
        failed.append("DEMO_MODE=1 (refusing real orders on simulated cash)")
    if failed:
        raise RuntimeError("live.execution=real refused: " + "; ".join(failed))
    return ExecutionMode(
        adapter_mode="real",
        places_real_orders=True,
        reason=(
            "trading_mode=live execution=real live_confirmed=true "
            "creds=set DEMO_MODE unset → real"
        ),
    )
