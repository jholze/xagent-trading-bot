"""#719 stop resolver gap, pinned to the measured table.

The issue decides no resolver change until a production count and a choice
of which resolver is the truth. These tests lock that measurement:

- S is the stop ``resolve_strategy_params`` + ``evaluate_long_hard_stop`` fire.
- R is ``RiskManager._effective_stop_loss_pct`` (config only, no Hermes).
- The full stop fires only when loss is strictly greater than S (the 12.5
  figures in the issue table are grid points, not a firing offset).

Symbols come from the case or from ``config.strategies``. No live orders.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from core.config import BotConfig, get_bot_config
from core.models import MarketContext
from hermes.memory.store import DEFAULT_PARAMS
from risk.risk_manager import RiskManager
from services.exit_realtime.shadow_eval import evaluate_would_sells
from strategies.dca import _partial_stop_ratio_from_config, evaluate_long_hard_stop
from strategies.registry import resolve_strategy_params
from strategies.technical_rsi_bb import TechnicalRSIStrategy

_UNLISTED = "GAP/USDT"
_TF = "4h"
_ENTRY = 1.0
_FRESH = {
    "dca_rounds": 0,
    "dca_recovery_rounds": 0,
    "last_dca_at": None,
    "last_dca_recovery_at": None,
}


def _repo_raw() -> dict:
    return copy.deepcopy(get_bot_config().raw)


def _listed_symbol(raw: dict) -> str:
    """A configured strategies[] symbol whose stop matches the global stop."""
    global_stop = float(raw["stop_loss_pct"])
    for entry in raw.get("strategies") or []:
        if entry.get("timeframe") != _TF:
            continue
        try:
            stop = float(entry.get("stop_loss_pct"))
        except (TypeError, ValueError):
            continue
        if stop == global_stop and entry.get("symbol"):
            return str(entry["symbol"])
    raise AssertionError("committed config has no 4h strategy at the global stop")


def _price(loss_pct: float) -> float:
    return _ENTRY * (1.0 - loss_pct / 100.0)


def _cases() -> dict:
    repo = _repo_raw()
    global_stop = float(repo["stop_loss_pct"])
    listed = _listed_symbol(repo)
    hermes = dict(DEFAULT_PARAMS)
    return {
        "C1_global_only": dict(
            raw={"stop_loss_pct": global_stop}, hermes=None, coin={}, sym=_UNLISTED
        ),
        "C2_config_entry_30": dict(
            raw={
                "stop_loss_pct": global_stop,
                "strategies": [{"symbol": _UNLISTED, "timeframe": _TF, "stop_loss_pct": 30}],
            },
            hermes=None,
            coin={},
            sym=_UNLISTED,
        ),
        "C3_hermes_default_profile": dict(
            raw={"stop_loss_pct": global_stop}, hermes=hermes, coin={}, sym=_UNLISTED
        ),
        "C4_hermes_plus_config_entry_30": dict(
            raw={
                "stop_loss_pct": global_stop,
                "strategies": [{"symbol": _UNLISTED, "timeframe": _TF, "stop_loss_pct": 30}],
            },
            hermes=hermes,
            coin={},
            sym=_UNLISTED,
        ),
        "C5_volatile_tier_frozen": dict(
            raw={"stop_loss_pct": global_stop},
            hermes=None,
            coin={},
            sym=_UNLISTED,
            frozen="volatile",
        ),
        "C6_cmc_trending_micro": dict(
            raw={"stop_loss_pct": global_stop},
            hermes=None,
            coin={"source": "cmc_trending", "market_cap_tier": "micro"},
            sym=_UNLISTED,
        ),
        "C7_volatile_cfg_stop25_frozen": dict(
            raw={
                "stop_loss_pct": global_stop,
                "volatile_altcoin": {"enabled": True, "stop_loss_pct": 25.0},
            },
            hermes=None,
            coin={},
            sym=_UNLISTED,
            frozen="volatile",
        ),
        "C8_repo_cfg_unlisted_volatile_frozen": dict(
            raw=repo, hermes=None, coin={}, sym=_UNLISTED, frozen="volatile"
        ),
        "C9_repo_cfg_unlisted_hermes_default": dict(
            raw=repo, hermes=hermes, coin={}, sym=_UNLISTED
        ),
        "C10_repo_cfg_listed_hermes_default": dict(
            raw=repo, hermes=hermes, coin={}, sym=listed
        ),
    }


def _measure(case: dict) -> dict:
    sym = case["sym"]
    cfg = BotConfig(case["raw"])
    prof = (
        {"params": dict(case["hermes"]), "updated_at": "2026-01-01T00:00:00"}
        if case["hermes"]
        else {}
    )
    with patch("core.config.get_bot_config", return_value=cfg), patch(
        "strategies.registry.get_bot_config", return_value=cfg
    ), patch("strategies.entry_recipe.load_personal_params", return_value=None), patch(
        "hermes.memory.store.load_profile", return_value=prof
    ):
        params = resolve_strategy_params(
            {"symbol": sym, "timeframe": _TF, **case["coin"]},
            has_position=True,
            frozen_tier=case.get("frozen"),
            cfg=cfg,
        )
        base = params.get("stop_loss_pct", cfg.stop_loss_pct)
        from strategies.dca import effective_stop_loss_thresholds

        s_full, s_partial, _ = effective_stop_loss_thresholds(
            _FRESH, params, float(base), cfg=cfg
        )
        r_effective = RiskManager(cfg)._effective_stop_loss_pct(sym, _TF)
    return {
        "params": params,
        "s_full": float(s_full),
        "s_partial": None if s_partial is None else float(s_partial),
        "r": float(r_effective),
        "cfg": cfg,
        "sym": sym,
    }


def _expected(name: str, case: dict) -> dict:
    """Numbers from the issue table, derived from config and Hermes defaults."""
    global_stop = float(case["raw"]["stop_loss_pct"])
    ratio = _partial_stop_ratio_from_config(BotConfig(case["raw"]))
    hermes_stop = float(DEFAULT_PARAMS["stop_loss_pct"])
    config_partial = global_stop * ratio
    if name == "C2_config_entry_30":
        return {"s_full": 30.0, "s_partial": 30.0 * ratio, "r": 30.0 * ratio}
    if name == "C3_hermes_default_profile":
        return {
            "s_full": hermes_stop,
            "s_partial": hermes_stop * ratio,
            "r": config_partial,
        }
    if name == "C4_hermes_plus_config_entry_30":
        return {"s_full": 30.0, "s_partial": 30.0 * ratio, "r": 30.0 * ratio}
    if name == "C7_volatile_cfg_stop25_frozen":
        return {"s_full": 25.0, "s_partial": 25.0 * ratio, "r": config_partial}
    if name == "C8_repo_cfg_unlisted_volatile_frozen":
        volatile_partial = float(case["raw"]["volatile_altcoin"]["partial_stop_pct"])
        return {"s_full": global_stop, "s_partial": volatile_partial, "r": volatile_partial}
    if name == "C9_repo_cfg_unlisted_hermes_default":
        volatile_partial = float(case["raw"]["volatile_altcoin"]["partial_stop_pct"])
        return {
            "s_full": hermes_stop,
            "s_partial": hermes_stop * ratio,
            "r": volatile_partial,
        }
    if name == "C10_repo_cfg_listed_hermes_default":
        volatile_partial = float(case["raw"]["volatile_altcoin"]["partial_stop_pct"])
        return {
            "s_full": global_stop,
            "s_partial": global_stop * ratio,
            "r": volatile_partial,
        }
    return {"s_full": global_stop, "s_partial": config_partial, "r": config_partial}


def _fire(params: dict, base: float, loss_pct: float):
    return evaluate_long_hard_stop(
        price=_price(loss_pct),
        entry=_ENTRY,
        position=dict(_FRESH),
        strategy_params=params,
        base_stop_loss_pct=base,
    )


@pytest.mark.parametrize("name", list(_cases()))
def test_ac_719_measured_resolver_gap(name):
    """AC-719-C1..C10: S, R, and the strict-greater firing point match the issue table."""
    case = _cases()[name]
    measured = _measure(case)
    expected = _expected(name, case)
    assert measured["s_full"] == pytest.approx(expected["s_full"])
    assert measured["s_partial"] == pytest.approx(expected["s_partial"])
    assert measured["r"] == pytest.approx(expected["r"])
    # Gap cases: the risk resolver is not the stop the exit fires.
    if name in {
        "C3_hermes_default_profile",
        "C7_volatile_cfg_stop25_frozen",
        "C9_repo_cfg_unlisted_hermes_default",
    }:
        assert measured["s_full"] != pytest.approx(measured["r"])
    base = float(measured["params"].get("stop_loss_pct", measured["cfg"].stop_loss_pct))
    below = _fire(measured["params"], base, measured["s_full"] - 0.01)
    above = _fire(measured["params"], base, measured["s_full"] + 0.01)
    assert (below[0] if below else None) != "SELL_STOP_FULL"
    assert above == ("SELL_STOP_FULL", "stop_loss")


@pytest.mark.parametrize("path", ["cycle", "ws"])
def test_ac_719_hermes_default_fires_strictly_above_stop(path):
    """AC-719-boundary: Hermes default stops out strictly above its stop, on cycle and WS.

    The issue table's 12.5 is a probe grid point. The boundary is the Hermes
    ``stop_loss_pct`` itself.
    """
    case = _cases()["C3_hermes_default_profile"]
    measured = _measure(case)
    stop = float(DEFAULT_PARAMS["stop_loss_pct"])
    assert measured["s_full"] == pytest.approx(stop)
    params = measured["params"]
    just_below = _price(stop - 0.01)
    just_above = _price(stop + 0.01)
    if path == "cycle":
        market_below = _market(just_below, params)
        market_above = _market(just_above, params)
        below = TechnicalRSIStrategy().analyze(
            {"symbol": case["sym"], "timeframe": _TF}, market_below
        )
        above = TechnicalRSIStrategy().analyze(
            {"symbol": case["sym"], "timeframe": _TF}, market_above
        )
        assert below.action != "SELL_STOP_FULL"
        assert above.action == "SELL_STOP_FULL"
        return
    assert _ws(just_below, params, stop) != "SELL_STOP_FULL"
    assert _ws(just_above, params, stop) == "SELL_STOP_FULL"


def _market(price: float, params: dict) -> MarketContext:
    return MarketContext(
        symbol=_UNLISTED,
        timeframe=_TF,
        current_price=price,
        rsi=45.0,
        lower_bb=price * 0.5,
        vol_multiplier=1.0,
        has_position=True,
        open_positions=1,
        average_entry=_ENTRY,
        sim_state=dict(_FRESH),
        strategy_params=params,
    )


def _ws(price: float, params: dict, base: float) -> str:
    events = evaluate_would_sells(
        symbol=_UNLISTED,
        timeframe=_TF,
        price=price,
        position={"average_entry": _ENTRY, "recent_high": _ENTRY, **_FRESH},
        strategy_params=params,
        sources=frozenset({"stop_loss"}),
        bid=price,
        base_stop_loss_pct=base,
    )
    for ev in events:
        if ev.get("action"):
            return ev["action"]
    return "HOLD"
