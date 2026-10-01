"""#625 Macro/Stress pre-buy observe gate (shadow).

M1: fire_enabled false must not change buy vs skip vs the same path without the hook.
M2: open-lot and pure BUY_DCA must not emit a first-buy hard-block proposal.
M3: venue_liquidity_block and long_mcap stay authoritative; observe proposals
    do not clear or skip those RiskManager rejects.

M4/M5 (hit-rate, PF, drawdown, sample window) are Lena measurement after merge —
not this diff.

No live HTTP. Tests turn observe on. fire_enabled stays false. Nothing is
written under data/.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.actions import BUY, BUY_DCA, HOLD
from core.config import BotConfig
from core.models import MarketContext, SignalAnalysis
from risk.risk_manager import RiskManager
from strategies.macro_stress_observe import (
    REASON_CODE,
    _reset_ist_warn_for_tests,
    format_macro_stress_observe_log,
    macro_stress_observe_config,
    observe_macro_stress,
)
from tests.unit.test_long_mcap_venue_563 import (
    _EMPTY_BOOK,
    _buy,
    _cfg,
    _eval_env,
)


def _observe_cfg(*, observe: bool = True, fire: bool = False) -> dict:
    return {
        "macro_stress_observe": {
            "observe_enabled": observe,
            "fire_enabled": fire,
        }
    }


def _bias(regime: str | None = "RISK_OFF") -> dict:
    return {"regime": regime, "active": True, "block_buys": False}


def _mults(
    *,
    calendar: float = 1.0,
    session: float = 1.0,
    pm: float = 1.0,
    block_new_entries: bool = False,
) -> dict:
    return {
        "calendar_mult": calendar,
        "session_mult": session,
        "pm_mult": pm,
        "block_new_entries": block_new_entries,
    }


def _observe(
    *,
    regime: str | None = "RISK_OFF",
    calendar: float = 1.0,
    session: float = 1.0,
    pm: float = 1.0,
    block_new_entries: bool = False,
    has_position: bool = False,
    action: str = BUY,
    observe: bool = True,
    fire: bool = False,
):
    return observe_macro_stress(
        _observe_cfg(observe=observe, fire=fire),
        has_position=has_position,
        action=action,
        get_bias=lambda _c: _bias(regime),
        get_multipliers=lambda _c: _mults(
            calendar=calendar,
            session=session,
            pm=pm,
            block_new_entries=block_new_entries,
        ),
    )


def test_defaults_observe_and_fire_false():
    flags = macro_stress_observe_config({})
    assert flags["observe_enabled"] is False
    assert flags["fire_enabled"] is False
    assert observe_macro_stress({}, has_position=False, action=BUY) is None
    assert observe_macro_stress(None, has_position=False, action=BUY) is None


def test_observe_disabled_does_not_call_ist_sources():
    called = {"bias": 0, "mm": 0}

    def get_bias(_c):
        called["bias"] += 1
        return _bias("RISK_OFF")

    def get_mm(_c):
        called["mm"] += 1
        return _mults()

    rec = observe_macro_stress(
        _observe_cfg(observe=False, fire=False),
        has_position=False,
        action=BUY,
        get_bias=get_bias,
        get_multipliers=get_mm,
    )
    assert rec is None
    assert called == {"bias": 0, "mm": 0}


def test_record_includes_required_fields():
    rec = _observe(regime="RISK_OFF", calendar=0.8, block_new_entries=True)
    assert rec is not None
    assert rec["reason"] == REASON_CODE
    assert rec["reason"] == "macro_stress_observe"
    assert rec["regime"] == "RISK_OFF"
    assert rec["calendar_mult"] == 0.8
    assert rec["session_mult"] == 1.0
    assert rec["pm_mult"] == 1.0
    assert rec["block_new_entries"] is True
    assert rec["would_block"] is True
    assert rec["would_cut"] is True
    assert rec["observe_enabled"] is True
    assert rec["fire_enabled"] is False
    line = format_macro_stress_observe_log(rec, symbol="BTC/USDT")
    assert "macro_stress_observe" in line
    assert "regime=RISK_OFF" in line
    assert "would_block=True" in line
    assert "fire_enabled=False" in line


def test_block_new_entries_is_ist_log_only_not_would_block():
    rec = _observe(regime="RISK_ON", block_new_entries=True)
    assert rec is not None
    assert rec["block_new_entries"] is True
    assert rec["would_block"] is False


def test_crash_is_same_stress_class_as_risk_off_proposal():
    rec = _observe(regime="CRASH")
    assert rec is not None
    assert rec["regime"] == "CRASH"
    assert rec["would_block"] is True
    assert rec["would_cut"] is False


def test_would_cut_only_when_ist_multiplier_below_one():
    assert _observe()["would_cut"] is False
    assert _observe(calendar=0.99)["would_cut"] is True
    assert _observe(session=0.5)["would_cut"] is True
    assert _observe(pm=0.0)["would_cut"] is True
    rec = _observe(calendar=1.5, session=1.2, pm=1.1, regime="RISK_OFF")
    assert rec["would_cut"] is False
    assert rec["would_block"] is True


def test_observe_does_not_widen_size_under_risk_off():
    rec = _observe(regime="RISK_OFF", calendar=1.5, session=1.4, pm=1.3)
    assert rec is not None
    assert rec["would_cut"] is False
    assert "would_widen" not in rec
    assert rec["calendar_mult"] == 1.5
    assert rec["session_mult"] == 1.4
    assert rec["pm_mult"] == 1.3


def test_successful_ist_read_sets_sources_ok():
    rec = _observe(regime="NEUTRAL")
    assert rec["sources_ok"] is True
    assert rec["bias_error"] is False
    assert rec["mults_error"] is False


def test_raising_get_bias_marks_failed_read_not_calm_success():
    _reset_ist_warn_for_tests()
    warnings: list[tuple[str, str]] = []

    def _capture(msg, level="INFO"):
        warnings.append((str(msg), str(level)))

    def boom(_c):
        raise RuntimeError("bias-down")

    with patch("strategies.macro_stress_observe.log", side_effect=_capture):
        rec = observe_macro_stress(
            _observe_cfg(),
            has_position=False,
            action=BUY,
            get_bias=boom,
            get_multipliers=lambda _c: _mults(),
        )
    assert rec is not None
    assert rec["sources_ok"] is False
    assert rec["bias_error"] is True
    assert rec["mults_error"] is False
    assert rec["regime"] is None
    # would_block is False because regime is unknown — not because Ist said calm.
    assert rec["would_block"] is False
    calm = _observe(regime="NEUTRAL")
    assert calm["would_block"] is False
    assert calm["sources_ok"] is True
    assert calm["bias_error"] is False
    assert rec["sources_ok"] is not True
    assert rec["bias_error"] is not calm["bias_error"]
    assert any(
        level == "WARNING" and "bias" in msg and "read failed" in msg
        for msg, level in warnings
    )


def test_raising_get_multipliers_marks_failed_read_not_no_cut():
    _reset_ist_warn_for_tests()
    warnings: list[tuple[str, str]] = []

    def _capture(msg, level="INFO"):
        warnings.append((str(msg), str(level)))

    def boom(_c):
        raise RuntimeError("mm-down")

    with patch("strategies.macro_stress_observe.log", side_effect=_capture):
        rec = observe_macro_stress(
            _observe_cfg(),
            has_position=False,
            action=BUY,
            get_bias=lambda _c: _bias("RISK_OFF"),
            get_multipliers=boom,
        )
    assert rec is not None
    assert rec["sources_ok"] is False
    assert rec["mults_error"] is True
    assert rec["bias_error"] is False
    assert rec["calendar_mult"] == 1.0
    assert rec["would_cut"] is False
    ok_cut = _observe(regime="RISK_OFF", calendar=1.0)
    assert ok_cut["would_cut"] is False
    assert ok_cut["sources_ok"] is True
    assert rec["mults_error"] is not ok_cut["mults_error"]
    assert any(
        level == "WARNING" and "multipliers" in msg and "read failed" in msg
        for msg, level in warnings
    )


def test_unparseable_multiplier_does_not_look_like_no_cut():
    _reset_ist_warn_for_tests()
    warnings: list[tuple[str, str]] = []

    def _capture(msg, level="INFO"):
        warnings.append((str(msg), str(level)))

    with patch("strategies.macro_stress_observe.log", side_effect=_capture):
        rec = observe_macro_stress(
            _observe_cfg(),
            has_position=False,
            action=BUY,
            get_bias=lambda _c: _bias("RISK_ON"),
            get_multipliers=lambda _c: {
                "calendar_mult": "not-a-number",
                "session_mult": 1.0,
                "pm_mult": 1.0,
            },
        )
    assert rec is not None
    assert rec["calendar_mult"] == 1.0
    assert rec["would_cut"] is False
    assert rec["sources_ok"] is False
    assert rec["mults_error"] is True
    assert rec["bias_error"] is False
    ok = _observe(regime="RISK_ON", calendar=1.0)
    assert ok["would_cut"] is False
    assert ok["sources_ok"] is True
    assert rec["sources_ok"] is not ok["sources_ok"]
    assert any(
        level == "WARNING" and "multipliers" in msg and "calendar_mult" in msg
        for msg, level in warnings
    )


# --- M1: fire_enabled false does not change buy vs skip ---


def test_m1_observe_on_fire_false_does_not_change_first_buy_vs_skip():
    on = _run_engine_first_buy(observe=True, fire=False, regime="RISK_OFF")
    off = _run_engine_first_buy(observe=False, fire=False, regime="RISK_OFF")
    assert on.normalized_action == off.normalized_action == BUY
    assert on.action == off.action
    assert (on.shadow_action or "") == (off.shadow_action or "")


def test_m1_observe_on_fire_false_does_not_change_hold_vs_skip():
    on = _run_engine_first_buy(
        observe=True, fire=False, regime="RISK_OFF", merge_action=HOLD
    )
    off = _run_engine_first_buy(
        observe=False, fire=False, regime="RISK_OFF", merge_action=HOLD
    )
    assert on.normalized_action == off.normalized_action == HOLD
    assert on.action == off.action


def test_m1_fire_true_still_does_not_apply_in_this_ticket():
    rec = _observe(regime="RISK_OFF", fire=True)
    assert rec is not None
    assert rec["fire_enabled"] is True
    assert rec["would_block"] is True
    on = _run_engine_first_buy(observe=True, fire=True, regime="RISK_OFF")
    off = _run_engine_first_buy(observe=False, fire=False, regime="RISK_OFF")
    assert on.normalized_action == off.normalized_action == BUY


def test_m1_engine_logs_observe_record_and_leaves_buy():
    logs: list[str] = []

    def _capture(msg, level="INFO"):
        logs.append(str(msg))

    analysis = _run_engine_first_buy(
        observe=True,
        fire=False,
        regime="RISK_OFF",
        calendar=0.7,
        log_side_effect=_capture,
    )
    assert analysis.normalized_action == BUY
    assert analysis.action == "BUY"
    assert not analysis.shadow_action
    matched = [m for m in logs if "macro_stress_observe" in m and "would_block=" in m]
    assert matched
    line = matched[-1]
    assert "reason=macro_stress_observe" in line
    assert "regime=RISK_OFF" in line
    assert "calendar_mult=0.7" in line
    assert "would_block=True" in line
    assert "would_cut=True" in line
    assert "observe_enabled=True" in line
    assert "fire_enabled=False" in line


# --- M2: open-lot / BUY_DCA must not emit first-buy hard-block ---


def test_m2_open_lot_does_not_emit_first_buy_hard_block_proposal():
    rec = _observe(regime="RISK_OFF", calendar=0.5, has_position=True, action=BUY)
    assert rec is not None
    assert rec["would_block"] is False
    assert rec["would_cut"] is True


def test_m2_buy_dca_does_not_emit_first_buy_hard_block_proposal():
    rec = _observe(
        regime="CRASH",
        calendar=0.5,
        has_position=False,
        action=BUY_DCA,
    )
    assert rec is not None
    assert rec["would_block"] is False
    assert rec["would_cut"] is True

    rec_open = _observe(
        regime="RISK_OFF",
        has_position=True,
        action=BUY_DCA,
    )
    assert rec_open["would_block"] is False


def test_m2_engine_does_not_call_observe_on_open_lot():
    with patch(
        "strategies.macro_stress_observe.observe_macro_stress"
    ) as mocked:
        _run_engine_open_lot(merge_action=HOLD)
        mocked.assert_not_called()


def test_m2_engine_does_not_call_observe_on_buy_dca():
    with patch(
        "strategies.macro_stress_observe.observe_macro_stress"
    ) as mocked:
        analysis = _run_engine_open_lot(dca=True)
        assert analysis.normalized_action == BUY_DCA
        mocked.assert_not_called()


# --- M3: venue / long_mcap rejects stay authoritative ---


def test_m3_venue_liquidity_block_not_cleared_by_observe_proposal():
    rec = _observe(regime="RISK_OFF", calendar=0.6)
    assert rec["would_block"] is True
    assert rec["would_cut"] is True
    rm = RiskManager(_cfg())
    with _eval_env(rm, metrics=_EMPTY_BOOK, mcap=6_000_000):
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "venue_liquidity_block"
    assert dec.order is None


def test_m3_long_mcap_not_cleared_by_observe_proposal():
    rec = _observe(regime="RISK_OFF", calendar=0.6)
    assert rec["would_block"] is True
    assert rec["would_cut"] is True
    rm = RiskManager(_cfg())
    with _eval_env(rm, mcap=4_900_000):
        dec = rm.evaluate(_buy(source="cmc"), "4h", source="cmc")
    assert dec.approved is False
    assert dec.code == "long_mcap"
    assert dec.order is None
    assert "long mcap" in dec.message


# --- DecisionEngine helpers ---


def _engine(*, observe: bool, fire: bool = False):
    from strategies.decision_engine import DecisionEngine

    raw = {
        "regime_detector": {"enabled": False},
        "strategy_allocator": {"enabled": False},
        "volatile_altcoin": {"mode": "live"},
        "sell_rotation": {"mode": "off"},
        "macro_stress_observe": {
            "observe_enabled": observe,
            "fire_enabled": fire,
        },
        "risk": {"fail_closed_guards": "log", "position_locks": {"enabled": False}},
    }
    engine = DecisionEngine()
    engine.config = BotConfig(raw)
    return engine


def _first_buy_market():
    return MarketContext(
        symbol="SOL/USDT",
        timeframe="4h",
        current_price=1.0,
        rsi=40.0,
        lower_bb=0.9,
        has_position=False,
        open_positions=0,
        strategy_params={"strategy_profile": "grid", "volatility_tier": "normal"},
    )


def _open_lot_market():
    return MarketContext(
        symbol="SOL/USDT",
        timeframe="4h",
        current_price=1.0,
        rsi=40.0,
        lower_bb=0.9,
        has_position=True,
        average_entry=1.0,
        open_positions=1,
        strategy_params={"strategy_profile": "grid", "volatility_tier": "normal"},
    )


def _technical(*, action: str = HOLD):
    return SignalAnalysis(
        action=action,
        symbol="SOL/USDT",
        timeframe="4h",
        rsi=40.0,
        lower_bb=0.9,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical"],
    )


@contextmanager
def _engine_stack(
    engine,
    technical,
    *,
    position: dict,
    merge_buy=None,
    merge_sell=None,
    dca=None,
    regime: str = "RISK_OFF",
    calendar: float = 1.0,
    log_side_effect=None,
):
    strategy = MagicMock()
    strategy.analyze.return_value = technical
    with ExitStack() as stack:
        stack.enter_context(
            patch("strategies.decision_engine.get_strategy", return_value=strategy)
        )
        stack.enter_context(
            patch("strategies.decision_engine.get_position", return_value=position)
        )
        stack.enter_context(patch.object(engine, "_sync_watch_15m_state"))
        stack.enter_context(
            patch.object(
                engine,
                "_apply_shadow_mode",
                side_effect=lambda n, e, p, s=None: (n, e, ""),
            )
        )
        stack.enter_context(
            patch(
                "strategies.decision_engine.policy_shadow_active",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "core.coin_eligibility.passes_coin_filters",
                return_value=(True, ""),
            )
        )
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value=_bias(regime),
            )
        )
        stack.enter_context(
            patch(
                "intelligence.macro.snapshot.get_risk_multipliers",
                return_value=_mults(calendar=calendar),
            )
        )
        if merge_buy is not None:
            stack.enter_context(patch.object(engine, "_merge_buy", return_value=merge_buy))
            stack.enter_context(
                patch.object(
                    engine,
                    "_apply_entry_sensor_buy",
                    side_effect=lambda n, s, c, *a, **k: (n, s, c, "", ""),
                )
            )
        if merge_sell is not None:
            stack.enter_context(
                patch.object(engine, "_merge_sell", return_value=merge_sell)
            )
        if dca is not None:
            stack.enter_context(
                patch(
                    "services.dca_sniper.config.sniper_owns_cycle_dca",
                    return_value=False,
                )
            )
            stack.enter_context(
                patch("strategies.decision_engine.evaluate_dca_addon", return_value=dca)
            )
            stack.enter_context(
                patch(
                    "strategies.dca_portfolio.should_defer_per_coin_dca",
                    return_value=False,
                )
            )
        if log_side_effect is not None:
            stack.enter_context(
                patch("strategies.decision_engine.log", side_effect=log_side_effect)
            )
        yield


def _run_engine_first_buy(
    *,
    observe: bool,
    fire: bool = False,
    regime: str = "RISK_OFF",
    calendar: float = 1.0,
    merge_action: str = BUY,
    log_side_effect=None,
):
    engine = _engine(observe=observe, fire=fire)
    market = _first_buy_market()
    coin = {
        "symbol": market.symbol,
        "timeframe": market.timeframe,
        "strategy_params": market.strategy_params,
    }
    merge = (merge_action, ["technical"], 80.0)
    with _engine_stack(
        engine,
        _technical(),
        position={"amount": 0, "average_entry": 0},
        merge_buy=merge,
        regime=regime,
        calendar=calendar,
        log_side_effect=log_side_effect,
    ):
        return engine.evaluate_with_market(coin, market)


def _run_engine_open_lot(*, dca: bool = False, merge_action: str = HOLD):
    engine = _engine(observe=True, fire=False)
    market = _open_lot_market()
    coin = {
        "symbol": market.symbol,
        "timeframe": market.timeframe,
        "strategy_params": market.strategy_params,
    }
    dca_obj = None
    if dca:
        dca_obj = MagicMock(
            shadow_only=False,
            source="dca",
            rationale="test-dca",
            usdt_amount=50.0,
        )
    with _engine_stack(
        engine,
        _technical(),
        position={"amount": 1.0, "average_entry": 1.0},
        merge_sell=(merge_action, ["technical"], 50.0, [], "", {}),
        dca=dca_obj,
        regime="RISK_OFF",
        calendar=0.5,
    ):
        return engine.evaluate_with_market(coin, market)


def test_neutral_regime_first_buy_would_block_false():
    rec = _observe(regime="NEUTRAL")
    assert rec["would_block"] is False
    rec_on = _observe(regime="RISK_ON")
    assert rec_on["would_block"] is False
    rec_none = _observe(regime=None)
    assert rec_none["would_block"] is False
    assert rec_none["regime"] is None
