"""Shadow tags and volatile shadow mode must not turn a live stop into HOLD.

Synthetic fixtures only. No coin names or tenants from config.

Stops (stop_loss, partial_stop, SELL_STOP_FULL, x_stop_loss) execute under a
shadow tag and under volatile shadow, including when volatile_altcoin.mode is
missing. Missing mode otherwise keeps the previous shadow behaviour: BUY,
BUY_STRONG, BUY_DCA, and non-stop sells are held. Explicit mode=shadow holds
buys the same way. shadow_action is set only for a held action, and it is that
action. Every audit row carries shadow_suppressed.

The cap-stop case stays xfail until #696 names that stop.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.actions import SELL_FULL, SELL_PARTIAL_20, SELL_PARTIAL_30, SELL_PARTIAL_50
from core.models import MarketContext, SignalAnalysis
from services.audit_trail import AuditTrail
from services.signal_orchestrator import SignalOrchestrator
from strategies.dca import DCACandidate
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
    engine.config.raw.setdefault("coin_filters", {})["enabled"] = False
    # Buy fixtures must not hit the exchange watchlist lookup.
    engine.config.raw.setdefault("entry_sensor_15m", {})["enabled"] = False
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
    extra_shadows=None,
    live_trail=None,
    x_signals=None,
    cmc_signals=None,
    params=None,
    has_position=True,
    dca_candidate=None,
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
    shadows = []
    if shadow is not None:
        shadows.append(shadow)
    shadows.extend(extra_shadows or [])
    for kind, cand in shadows:
        if kind == "exit":
            exit_ret.append(cand)
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
        has_position=has_position,
        average_entry=1.0 if has_position else 0.0,
        open_positions=0,
        rsi=40.0,
        strategy_params=dict(params or {}),
        sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 40.0},
    )

    class _Strat:
        def analyze(self, coin, market, x_signals=None):
            return technical

    stack = [
        patch("strategies.decision_engine.get_position", return_value=position),
        patch("strategies.decision_engine.get_strategy", return_value=_Strat()),
    ]
    if dca_candidate is not None:
        stack.extend([
            patch(
                "strategies.decision_engine.evaluate_dca_addon",
                return_value=dca_candidate,
            ),
            patch(
                "strategies.dca_portfolio.should_defer_per_coin_dca",
                return_value=False,
            ),
            patch(
                "services.dca_sniper.config.sniper_owns_cycle_dca",
                return_value=False,
            ),
        ])
    with stack[0], stack[1], patch(
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
        # dca patches sit outside the long with so the context stays readable.
        from contextlib import ExitStack

        with ExitStack() as extra:
            for ctx in stack[2:]:
                extra.enter_context(ctx)
            yield engine._evaluate_internal(
                {"symbol": technical.symbol, "timeframe": "1h"},
                market,
                x_signals,
                cmc_signals,
            )


def _run(
    technical,
    *,
    shadow=None,
    extra_shadows=None,
    live_trail=None,
    position=None,
    params=None,
    x_signals=None,
    cmc_signals=None,
    engine=None,
    has_position=True,
    dca_candidate=None,
):
    engine = engine or _engine()
    with _fixtures(
        engine,
        technical,
        position if position is not None else _position(),
        shadow=shadow,
        extra_shadows=extra_shadows,
        live_trail=live_trail,
        x_signals=x_signals,
        cmc_signals=cmc_signals,
        params=params,
        has_position=has_position,
        dca_candidate=dca_candidate,
    ) as analysis:
        return analysis


def _audit_row(analysis) -> dict:
    """The decision row AuditTrail.record writes for this analysis."""
    captured: dict = {}

    def _capture(entry):
        captured["entry"] = dict(entry)

    class _Cfg:
        raw = {"observability": {"decisions_audit": True}}
        trading_mode = "demo"

    with patch("services.audit_trail.log_decision", side_effect=_capture), patch(
        "services.observability_store.persist_decision"
    ), patch(
        "services.observability_store.mongo_sync_enabled", return_value=False
    ), patch(
        "strategies.positions.get_position", return_value={"amount": 0}
    ):
        AuditTrail(config=_Cfg()).record(
            {"symbol": analysis.symbol, "timeframe": analysis.timeframe},
            analysis,
            price=1.0,
        )
    return captured["entry"]


def _flat_position() -> dict:
    return {"amount": 0.0, "average_entry": 0.0, "strategy_tier": None}


def _assert_no_order(analysis) -> None:
    """HOLD decisions return before the orchestrator builds an order."""
    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.trading = MagicMock()
    result = orch.execute_if_needed(
        analysis,
        {"symbol": analysis.symbol, "timeframe": analysis.timeframe},
        1.0,
    )
    assert result is None
    orch.trading.execute_order.assert_not_called()
    orch.trading.refresh.assert_not_called()


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
    assert analysis.shadow_action == ""
    assert analysis.shadow_suppressed is False


@pytest.mark.parametrize("case", _SHADOW_CASES, ids=[c[0] for c in _SHADOW_CASES])
def test_t2_shadow_source_sell_stays_hold_and_logs_shadow(case):
    """Winning sell is the shadow source → HOLD, and the log is that sell."""
    _tag, kind, action, source, expected_shadow = case
    symbol = _symbol()
    technical = _technical(symbol, "HOLD", ["technical"])
    analysis = _run(technical, shadow=(kind, _cand(action, source, 4, shadow=True)))
    assert analysis.action == "HOLD"
    assert analysis.shadow_action == expected_shadow
    assert analysis.sell_source == source
    assert analysis.shadow_suppressed is True


def test_t4_live_trailing_executes_without_foreign_shadow_action():
    """A live trailing sell executes. The shadow candidate is not logged."""
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "HOLD", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 2, shadow=True)),
        live_trail=_cand(SELL_FULL, "trailing_stop", 6, shadow=False),
    )
    assert analysis.sell_source == "trailing_stop"
    assert analysis.action == "SELL_STOP_FULL"
    assert analysis.shadow_action == ""
    assert analysis.shadow_suppressed is False
    assert "exit_sensor_shadow" in analysis.sources


_LOCK_REASONS = ("stop_loss", "partial_stop", "x_stop_loss", "trailing_stop")
_LOCK_CONTEXTS = (
    "no_tag",
    "exit_sensor_shadow",
    "time_profit_shadow",
    "volatile_shadow",
    "volatile_missing",
)


@pytest.mark.parametrize("reason", _LOCK_REASONS)
@pytest.mark.parametrize("context", _LOCK_CONTEXTS)
def test_t5_locked_stop_stays_hold_with_zero_orders(reason, context):
    """A real position lock holds every stop. Shadow mode must not reopen it.

    The orchestrator builds no order because the decision is HOLD.
    """
    from x_analyzer import XSignal

    engine = _engine()
    params: dict = {}
    shadow = None
    if context == "exit_sensor_shadow":
        shadow = ("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 1, shadow=True))
    elif context == "time_profit_shadow":
        shadow = ("time", _cand(SELL_PARTIAL_50, "time_profit_exit", 1, shadow=True))
    elif context == "volatile_shadow":
        _volatile_mode(engine, "shadow")
        params = {"strategy_profile": "volatile_altcoin"}
    elif context == "volatile_missing":
        _volatile_mode(engine, None)
        params = {"strategy_profile": "volatile_altcoin"}

    symbol = _symbol()
    live_trail = None
    x_signals = None
    if reason == "stop_loss":
        technical = _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"])
    elif reason == "partial_stop":
        technical = _technical(symbol, "SELL_STOP_PARTIAL", ["technical", "stop_loss"])
    elif reason == "x_stop_loss":
        technical = _technical(symbol, "HOLD", ["technical"])
        x_signals = [XSignal("fixture", symbol.split("/")[0], "HOLD", 70, stop_loss=1.05)]
    else:
        technical = _technical(symbol, "HOLD", ["technical"])
        live_trail = _cand(SELL_FULL, "trailing_stop", 6, shadow=False)

    locked = _position(lock=build_lock(reason="fixture", locked_by="test"))
    analysis = _run(
        technical,
        shadow=shadow,
        live_trail=live_trail,
        x_signals=x_signals,
        position=locked,
        params=params,
        engine=engine,
    )
    assert analysis.action == "HOLD"
    assert "position_locked" in analysis.sources
    _assert_no_order(analysis)


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
    assert technical.shadow_action == "SELL_10"
    assert technical.shadow_suppressed is True

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
    assert rsi.shadow_action == "SELL_STOP_FULL"
    assert rsi.shadow_action != "SELL_20"
    assert rsi.shadow_suppressed is True


def test_t6_missing_volatile_mode_hard_stop_executes():
    """Missing volatile_altcoin.mode keeps shadow for non-stops, not for stops."""
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
    assert hard.shadow_suppressed is False


def test_missing_mode_stop_executes_on_tag_path_and_volatile_path():
    """Mode missing + a stop executes in both shadow places.

    Tag path: a shadow tag is present and the profile is not volatile.
    Volatile path: no tag, profile volatile_altcoin, mode key absent.
    """
    engine = _engine()
    _volatile_mode(engine, None)

    symbol = _symbol()
    tagged = _run(
        _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"]),
        shadow=_shadow_for(_SHADOW_CASES[-1]),
        params={},
        engine=engine,
    )
    assert tagged.action == "SELL_STOP_FULL"
    assert tagged.sell_source == "stop_loss"
    assert tagged.shadow_action == ""
    assert tagged.shadow_suppressed is False
    assert "exit_sensor_shadow" in tagged.sources

    symbol = _symbol()
    volatile = _run(
        _technical(symbol, "SELL_STOP_FULL", ["technical", "stop_loss"]),
        params={"strategy_profile": "volatile_altcoin"},
        engine=engine,
    )
    assert volatile.action == "SELL_STOP_FULL"
    assert volatile.sell_source == "stop_loss"
    assert volatile.shadow_action == ""
    assert volatile.shadow_suppressed is False
    assert "exit_sensor_shadow" not in volatile.sources


def test_b1_explicit_shadow_holds_buy():
    """Explicit volatile_altcoin.mode=shadow holds a BUY, as on staging.

    Restoring ``or not is_sell`` (buys pass through) turns this red.
    """
    engine = _engine()
    _volatile_mode(engine, "shadow")
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "BUY", ["technical"]),
        params={"strategy_profile": "volatile_altcoin"},
        engine=engine,
        has_position=False,
        position=_flat_position(),
    )
    assert analysis.action == "HOLD"
    assert analysis.shadow_action == "BUY"
    assert analysis.shadow_suppressed is True


@pytest.mark.parametrize("kind", ["BUY", "BUY_STRONG", "BUY_DCA", "SELL_30"])
def test_missing_mode_holds_buy_buy_strong_buy_dca_and_nonstop_sell(kind):
    """Missing mode matches base: buys and non-stop sells are held to shadow.

    Changing the missing-key default from shadow to live turns this red.
    """
    from data.cmc_community_provider import CMCCommunitySignal
    from x_analyzer import XSignal

    engine = _engine()
    _volatile_mode(engine, None)
    engine._x_buy_threshold = lambda *_a, **_k: 0.0
    engine._cmc_buy_threshold = lambda *_a, **_k: 0.0
    params = {"strategy_profile": "volatile_altcoin"}
    symbol = _symbol()
    x_signals = None
    cmc_signals = None
    dca_candidate = None
    has_position = True
    position = _position()
    if kind == "BUY":
        technical = _technical(symbol, "BUY", ["technical"])
        has_position = False
        position = _flat_position()
        expected_shadow = "BUY"
    elif kind == "BUY_STRONG":
        technical = _technical(symbol, "BUY", ["technical"])
        has_position = False
        position = _flat_position()
        base = symbol.split("/")[0]
        x_sig = XSignal("fixture", base, "BUY", 90)
        x_sig.effective_confidence = 90
        cmc = CMCCommunitySignal(base, "BUY", 90, votes_bullish=90, votes_bearish=5)
        cmc.effective_confidence = 90
        x_signals = [x_sig]
        cmc_signals = [cmc]
        expected_shadow = "BUY"
    elif kind == "BUY_DCA":
        technical = _technical(symbol, "HOLD", ["technical"])
        dca_candidate = DCACandidate(
            action="BUY_DCA",
            source="dca",
            rationale="fixture",
            usdt_amount=25.0,
            shadow_only=False,
        )
        expected_shadow = "BUY_DCA"
    else:
        technical = _technical(symbol, "SELL_30", ["technical"])
        expected_shadow = "SELL_30"

    analysis = _run(
        technical,
        params=params,
        engine=engine,
        has_position=has_position,
        position=position,
        x_signals=x_signals,
        cmc_signals=cmc_signals,
        dca_candidate=dca_candidate,
    )
    assert analysis.action == "HOLD"
    assert analysis.shadow_action == expected_shadow
    assert analysis.shadow_suppressed is True
    if kind == "BUY_STRONG":
        assert "multi_source" in analysis.sources


@pytest.mark.parametrize("mode", ["shadow", None], ids=["explicit-shadow", "mode-missing"])
def test_partial_stop_executes_under_volatile_shadow(mode):
    """partial_stop on the volatile path executes.

    Removing partial_stop from the stop set turns this red. Changing the
    exemption from ``or`` to ``and`` turns it red too: the normalized action
    is SELL_PARTIAL_50, which is not in the set, so only the source matches.
    """
    engine = _engine()
    _volatile_mode(engine, mode)
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "SELL_STOP_PARTIAL", ["technical", "stop_loss"]),
        params={"strategy_profile": "volatile_altcoin"},
        engine=engine,
    )
    assert analysis.sell_source == "partial_stop"
    assert analysis.action == "SELL_STOP_PARTIAL"
    assert analysis.shadow_action == ""
    assert analysis.shadow_suppressed is False


@pytest.mark.parametrize("mode", ["shadow", None], ids=["explicit-shadow", "mode-missing"])
def test_x_stop_loss_executes_under_volatile_shadow(mode):
    """x_stop_loss on the volatile path executes.

    Removing x_stop_loss from the stop set turns this red. The winner is the
    x stop itself, so a foreign live sell cannot hide the mutation.
    """
    from x_analyzer import XSignal

    engine = _engine()
    _volatile_mode(engine, mode)
    symbol = _symbol()
    x_sig = XSignal("fixture", symbol.split("/")[0], "HOLD", 70, stop_loss=1.05)
    analysis = _run(
        _technical(symbol, "HOLD", ["technical"]),
        params={"strategy_profile": "volatile_altcoin"},
        x_signals=[x_sig],
        engine=engine,
    )
    assert analysis.sell_source == "x_stop_loss"
    assert analysis.action == "SELL_STOP_FULL"
    assert analysis.shadow_action == ""
    assert analysis.shadow_suppressed is False


def test_manual_sell_is_held_under_volatile_shadow():
    """manual is not a stop. Explicit shadow holds it.

    Putting manual back into the stop set turns this red.
    """
    engine = _engine()
    _volatile_mode(engine, "shadow")
    symbol = _symbol()
    analysis = _run(
        _technical(symbol, "HOLD", ["technical"]),
        live_trail=_cand(SELL_FULL, "manual", 8, shadow=False),
        params={"strategy_profile": "volatile_altcoin"},
        engine=engine,
    )
    assert analysis.sell_source == "manual"
    assert analysis.action == "HOLD"
    assert analysis.shadow_action == "SELL_STOP_FULL"
    assert analysis.shadow_suppressed is True


def test_literal_sell_stop_full_is_protected_without_a_stop_source():
    """The exemption OR protects a literal SELL_STOP_FULL whose source is not a stop.

    Changing ``or`` to ``and`` turns this red. A trailing full close stays
    SELL_FULL after normalize, so this case is the action side of the OR.
    """
    engine = _engine()
    _volatile_mode(engine, "shadow")
    normalized, action, shadow = engine._apply_shadow_mode(
        "SELL_STOP_FULL",
        "SELL_STOP_FULL",
        {"strategy_profile": "volatile_altcoin"},
        sources=[],
        sell_source="grid",
    )
    assert normalized == "SELL_STOP_FULL"
    assert action == "SELL_STOP_FULL"
    assert shadow == ""


def test_shadow_log_row_records_suppressed_flag_and_own_sell():
    """One row for an executed sell, one for a suppressed sell.

    Executed: shadow_suppressed is false and the foreign shadow candidate is
    not the logged action. Suppressed: shadow_suppressed is true and the row
    carries that sell's own action and reason, not the other candidate.
    """
    symbol = _symbol()
    executed = _run(
        _technical(symbol, "HOLD", ["technical"]),
        shadow=("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 2, shadow=True)),
        live_trail=_cand(SELL_FULL, "trailing_stop", 6, shadow=False),
    )
    assert executed.action == "SELL_STOP_FULL"
    assert executed.sell_source == "trailing_stop"
    assert executed.shadow_action == ""
    assert executed.shadow_suppressed is False
    assert "exit_sensor_shadow" in executed.sources
    executed_row = _audit_row(executed)
    assert executed_row["shadow_suppressed"] is False
    assert executed_row["shadow_action"] == ""
    assert executed_row["sell_source"] == "trailing_stop"
    assert executed_row["shadow_action"] != "SELL_20"

    symbol = _symbol()
    suppressed = _run(
        _technical(symbol, "HOLD", ["technical"]),
        shadow=("time", _cand(SELL_PARTIAL_50, "time_profit_exit", 5, shadow=True)),
        extra_shadows=[
            ("exit", _cand(SELL_PARTIAL_20, "exit_15m_weakness", 2, shadow=True)),
        ],
    )
    assert suppressed.action == "HOLD"
    assert suppressed.sell_source == "time_profit_exit"
    assert suppressed.shadow_action == "SELL_STOP_PARTIAL"
    assert suppressed.shadow_action != "SELL_20"
    assert suppressed.shadow_suppressed is True
    assert "exit_sensor_shadow" in suppressed.sources
    assert "time_profit_shadow" in suppressed.sources
    suppressed_row = _audit_row(suppressed)
    assert suppressed_row["shadow_suppressed"] is True
    assert suppressed_row["shadow_action"] == "SELL_STOP_PARTIAL"
    assert suppressed_row["sell_source"] == "time_profit_exit"
    assert suppressed_row["shadow_action"] != "SELL_20"


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
