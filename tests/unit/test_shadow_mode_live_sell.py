"""Shadow tags and volatile shadow mode must not turn a live stop into HOLD.

Synthetic fixtures only. No coin names or tenants from config. T1, T6, and the
hard-stop T7 are red on dffd8a84 (shadow suppresses the stop) and green after
the fix. The cap-stop T7 stays xfail until #696 names that stop.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.actions import SELL_FULL, SELL_PARTIAL_20, SELL_PARTIAL_30, SELL_PARTIAL_50
from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.position_lock import build_lock
from strategies.sell_rotation_policy import SellPolicyAudit

# (tag, which evaluator, shadow action, shadow source, expected shadow_action)
_SHADOW_CASES = [
    ("trailing_shadow", "trail", SELL_FULL, "trailing_stop", "SELL_STOP_FULL"),
    (
        "trailing_take_profit_shadow",
        "ttp",
        SELL_PARTIAL_30,
        "trailing_take_profit",
        "SELL_30",
    ),
    (
        "profit_max_lifetime_shadow",
        "life",
        SELL_FULL,
        "profit_max_lifetime",
        "SELL_STOP_FULL",
    ),
    (
        "time_profit_shadow",
        "time",
        SELL_PARTIAL_50,
        "time_profit_exit",
        "SELL_STOP_PARTIAL",
    ),
    (
        "exit_sensor_shadow",
        "exit",
        SELL_PARTIAL_20,
        "exit_15m_weakness",
        "SELL_20",
    ),
]

_seq = 0


def _symbol() -> str:
    global _seq
    _seq += 1
    return f"SHD{_seq}/USDT"


def _cand(action: str, source: str, priority: int, *, shadow: bool) -> SimpleNamespace:
    return SimpleNamespace(
        action=action,
        source=source,
        priority=priority,
        rationale="fixture",
        shadow_only=shadow,
    )


def _technical(symbol: str, action: str, sources: list[str]) -> SignalAnalysis:
    return SignalAnalysis(
        action=action,
        symbol=symbol,
        timeframe="1h",
        rsi=40.0,
        lower_bb=0.2,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=list(sources),
        normalized_action=action,
        rationale="fixture",
        confidence=80.0,
    )


def _engine() -> DecisionEngine:
    engine = DecisionEngine(market_service=MagicMock())
    engine.config._raw = copy.deepcopy(engine.config.raw)
    engine.config.raw.setdefault("regime_detector", {})["enabled"] = False
    engine.config.raw.setdefault("strategy_allocator", {})["enabled"] = False
    return engine


def _volatile_mode(engine: DecisionEngine, mode: str | None) -> None:
    va = dict(engine.config.raw.get("volatile_altcoin") or {})
    if mode is None:
        va.pop("mode", None)
    else:
        va["mode"] = mode
    engine.config.raw["volatile_altcoin"] = va


def _position(**extra) -> dict:
    pos = {"amount": 10.0, "average_entry": 1.0, "strategy_tier": None}
    pos.update(extra)
    return pos


@contextmanager
def _fixtures(
    engine: DecisionEngine,
    technical: SignalAnalysis,
    position: dict,
    *,
    shadow=None,
    live_trail=None,
    x_signals=None,
    params=None,
):
    """Drive _evaluate_internal with one optional shadow candidate.

    Rotation and the entry guard are passed through so the winner is the
    candidate this fixture built. The position lock and _apply_shadow_mode
    stay on the real path.
    """

    def _rotation(candidates, *args, **kwargs):
        return list(candidates), SellPolicyAudit()

    def _guard(candidates, **kwargs):
        return list(candidates), []

    exit_ret = []
    trail_ret = live_trail
    ttp_ret = None
    life_ret = None
    time_ret = None
    if shadow is not None:
        kind = shadow[0]
        cand = shadow[1]
        if kind == "exit":
            exit_ret = [cand]
        elif kind == "trail":
            trail_ret = cand
        elif kind == "ttp":
            ttp_ret = cand
        elif kind == "life":
            life_ret = cand
        elif kind == "time":
            time_ret = cand

    market = MarketContext(
        symbol=technical.symbol,
        timeframe="1h",
        current_price=1.0,
        has_position=True,
        average_entry=1.0,
        rsi=40.0,
        strategy_params=dict(params or {}),
        sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 40.0},
    )

    class _Strat:
        def analyze(self, coin, market, x_signals=None):
            return technical

    with patch("strategies.decision_engine.get_position", return_value=position), patch(
        "strategies.decision_engine.get_strategy", return_value=_Strat()
    ), patch(
        "strategies.decision_engine.evaluate_market_structure_sells", return_value=[]
    ), patch(
        "strategies.decision_engine.sync_profit_armed_at", return_value=False
    ), patch(
        "strategies.decision_engine.evaluate_trailing_take_profit", return_value=ttp_ret
    ), patch(
        "strategies.decision_engine.evaluate_trailing_stop", return_value=trail_ret
    ), patch(
        "strategies.decision_engine.evaluate_time_profit_exit", return_value=time_ret
    ), patch(
        "strategies.decision_engine.evaluate_profit_max_lifetime", return_value=life_ret
    ), patch(
        "strategies.decision_engine.evaluate_exit_sensor_sells", return_value=exit_ret
    ), patch.object(
        engine, "_exit_sensor_cfg",
        return_value={"enabled": bool(exit_ret), "btc_rs": {"enabled": False}},
    ), patch.object(
        engine, "_oracle_climax_state", return_value=(None, {})
    ), patch(
        "strategies.decision_engine.apply_rotation_sell_filters", side_effect=_rotation
    ), patch(
        "strategies.decision_engine.filter_sell_candidates", side_effect=_guard
    ):
        yield engine._evaluate_internal(
            {"symbol": technical.symbol, "timeframe": "1h"},
            market,
            x_signals,
        )


def _run(technical, *, shadow=None, live_trail=None, position=None, params=None, x_signals=None, engine=None):
    engine = engine or _engine()
    with _fixtures(
        engine,
        technical,
        position or _position(),
        shadow=shadow,
        live_trail=live_trail,
        x_signals=x_signals,
        params=params,
    ) as analysis:
        return analysis


def _shadow_for(case) -> tuple:
    _tag, kind, action, source, _expected = case
    return kind, _cand(action, source, 3, shadow=True)


@pytest.mark.parametrize("case", _SHADOW_CASES, ids=[c[0] for c in _SHADOW_CASES])
def test_t1_hard_stop_wins_over_shadow_tag(case):
    """Tag present + hard stop wins → SELL_STOP_FULL, not HOLD."""
    symbol = _symbol()
    technical = _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"])
    analysis = _run(technical, shadow=_shadow_for(case))
    assert analysis.action == "SELL_STOP_FULL"
    assert analysis.normalized_action != "HOLD"
    assert analysis.sell_source == "stop_loss"


@pytest.mark.parametrize("case", _SHADOW_CASES, ids=[c[0] for c in _SHADOW_CASES])
def test_t2_shadow_source_sell_stays_hold_and_logs_shadow(case):
    """Winning sell is the shadow source → HOLD, shadow_action set."""
    _tag, kind, action, source, expected_shadow = case
    symbol = _symbol()
    technical = _technical(symbol, "HOLD", ["technical"])
    analysis = _run(technical, shadow=(kind, _cand(action, source, 4, shadow=True)))
    assert analysis.action == "HOLD"
    assert analysis.shadow_action == expected_shadow
    assert analysis.sell_source == source


def test_t3_manual_and_x_stop_loss_execute_under_shadow_tag():
    symbol = _symbol()
    manual = _run(
        _technical(symbol, "HOLD", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 2, shadow=True)),
        live_trail=_cand(SELL_FULL, "manual", 8, shadow=False),
    )
    assert manual.sell_source == "manual"
    assert manual.action == "SELL_STOP_FULL"
    assert manual.action != "HOLD"

    from x_analyzer import XSignal

    symbol = _symbol()
    x_sig = XSignal("fixture", symbol.split("/")[0], "HOLD", 70, stop_loss=1.05)
    x_stop = _run(
        _technical(symbol, "HOLD", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 2, shadow=True)),
        x_signals=[x_sig],
    )
    assert x_stop.sell_source == "x_stop_loss"
    assert x_stop.action == "SELL_STOP_FULL"


def test_t4_live_trailing_stop_executes_and_logs_shadow():
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "HOLD", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 2, shadow=True)),
        live_trail=_cand(SELL_FULL, "trailing_stop", 6, shadow=False),
    )
    assert analysis.sell_source == "trailing_stop"
    assert analysis.action == "SELL_STOP_FULL"
    assert analysis.shadow_action == "SELL_20"


def test_t5_locked_lot_hard_stop_stays_blocked():
    symbol = _symbol()
    locked = _position(lock=build_lock(reason="fixture", locked_by="test"))
    analysis = _run(
        _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"]),
        shadow=_shadow_for(_SHADOW_CASES[-1]),
        position=locked,
    )
    assert analysis.action == "HOLD"
    assert "position_locked" in analysis.sources


def test_partial_stop_under_shadow_tag_executes():
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "SELL_STOP_PARTIAL", ["technical", "stop_loss"]),
        shadow=_shadow_for(_SHADOW_CASES[-1]),
    )
    assert analysis.sell_source == "partial_stop"
    assert analysis.action == "SELL_STOP_PARTIAL"


def test_technical_rsi_sell_under_shadow_tag_stays_hold():
    """Technical/RSI profit sells stay HOLD. Adding `technical` to the stop set turns this red."""
    symbol = _symbol()
    engine = _engine()
    regime = engine.config.raw.setdefault("sell_policy", {}).setdefault("indicator_regime", {})
    regime["enabled"] = False
    technical = _run(
        _technical(symbol, "SELL_10", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 1, shadow=True)),
        engine=engine,
    )
    assert technical.sell_source == "technical"
    assert technical.action == "HOLD"
    assert technical.shadow_action

    symbol = _symbol()
    engine = _engine()
    regime = engine.config.raw.setdefault("sell_policy", {}).setdefault("indicator_regime", {})
    regime["enabled"] = True
    regime["trail_allow_rsi"] = True
    rsi = _run(
        _technical(symbol, "SELL_30", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 1, shadow=True)),
        engine=engine,
    )
    assert rsi.sell_source == "rsi_sell"
    assert rsi.action == "HOLD"
    assert rsi.shadow_action


def test_t6_missing_volatile_mode_key_hard_stop_executes():
    """Missing volatile_altcoin.mode defaults to live (profile volatile_altcoin).

    Hard stop → SELL_STOP_FULL. A non-stop sell also stays live: the default is
    live, not a stop-only fail-closed. M4 (default back to shadow) turns the
    non-stop assertion red; the hard stop stays protected by the stop exemption.
    """
    engine = _engine()
    _volatile_mode(engine, None)
    params = {"strategy_profile": "volatile_altcoin"}

    symbol = _symbol()
    hard = _run(
        _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"]),
        params=params,
        engine=engine,
    )
    assert hard.action == "SELL_STOP_FULL"
    assert hard.sell_source == "stop_loss"
    assert hard.shadow_action == ""

    symbol = _symbol()
    plain = _run(
        _technical(symbol, "SELL_30", ["technical"]),
        params=params,
        engine=engine,
    )
    assert plain.action != "HOLD"
    assert "SELL" in plain.action


def test_t7_explicit_volatile_shadow_hard_stop_executes():
    """volatile_altcoin.mode=shadow must not turn a hard stop into HOLD."""
    engine = _engine()
    _volatile_mode(engine, "shadow")
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"]),
        params={"strategy_profile": "volatile_altcoin"},
        engine=engine,
    )
    assert analysis.action == "SELL_STOP_FULL"
    assert analysis.sell_source == "stop_loss"
    assert analysis.normalized_action != "HOLD"


@pytest.mark.xfail(
    strict=True,
    reason="cap stop does not exist until #696; drop this mark in that rebase",
)
def test_t7_cap_stop_explicit_volatile_shadow_is_sell_stop_full():
    """25% loss with full stop 50 and cap 20, volatile mode explicitly shadow.

    The 20% cap is not on staging. This must become SELL_STOP_FULL once #696
    lands, and shadow mode must not turn that cap stop into HOLD.
    TODO(#696): cap-stop label.
    """
    engine = _engine()
    _volatile_mode(engine, "shadow")
    engine.config.raw.setdefault("risk", {})["long_hard_stop_max_loss_pct"] = 20
    symbol = _symbol()
    params = {
        "strategy_profile": "volatile_altcoin",
        "stop_loss_pct": 50.0,
        "partial_stop_pct": 45.0,
        "long_hard_stop_max_loss_pct": 20,
        "rsi_sell_mode": "level",
        "rsi_sell_30": 99,
        "rsi_sell_20": 99,
        "rsi_sell_min_gain_pct": 100,
        "dca": {},
    }
    technical_holder = {}

    class _Real:
        def analyze(self, coin, market, x_signals=None):
            from strategies.technical_rsi_bb import TechnicalRSIStrategy

            found = TechnicalRSIStrategy().analyze(coin, market, x_signals=x_signals)
            technical_holder["analysis"] = found
            return found

    market = MarketContext(
        symbol=symbol,
        timeframe="1h",
        current_price=0.75,
        has_position=True,
        average_entry=1.0,
        rsi=40.0,
        strategy_params=params,
        sim_state={
            "amount": 10.0,
            "average_entry": 1.0,
            "rsi_sell_tiers_done": {},
            "last_rsi": 40.0,
            "dca_rounds": 0,
        },
    )

    def _rotation(candidates, *args, **kwargs):
        return list(candidates), SellPolicyAudit()

    def _guard(candidates, **kwargs):
        return list(candidates), []

    with patch("strategies.decision_engine.get_position", return_value=_position()), patch(
        "strategies.decision_engine.get_strategy", return_value=_Real()
    ), patch(
        "strategies.decision_engine.evaluate_market_structure_sells", return_value=[]
    ), patch(
        "strategies.decision_engine.sync_profit_armed_at", return_value=False
    ), patch(
        "strategies.decision_engine.evaluate_trailing_take_profit", return_value=None
    ), patch(
        "strategies.decision_engine.evaluate_trailing_stop", return_value=None
    ), patch(
        "strategies.decision_engine.evaluate_time_profit_exit", return_value=None
    ), patch(
        "strategies.decision_engine.evaluate_profit_max_lifetime", return_value=None
    ), patch(
        "strategies.decision_engine.evaluate_exit_sensor_sells", return_value=[]
    ), patch.object(
        engine, "_exit_sensor_cfg", return_value={"enabled": False}
    ), patch.object(
        engine, "_oracle_climax_state", return_value=(None, {})
    ), patch(
        "strategies.decision_engine.apply_rotation_sell_filters", side_effect=_rotation
    ), patch(
        "strategies.decision_engine.filter_sell_candidates", side_effect=_guard
    ):
        analysis = engine._evaluate_internal(
            {"symbol": symbol, "timeframe": "1h"}, market
        )

    assert analysis.action == "SELL_STOP_FULL"
    assert analysis.normalized_action != "HOLD"
