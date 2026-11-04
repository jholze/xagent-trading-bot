"""Profit-floor exit of the harness engine (2026-11-04, Kay PR3). Pure tests: no Mongo, no network."""

from __future__ import annotations

import pytest

from backtest.harness import trials
from backtest.harness.engine import Engine
from tests.unit.test_backtest_harness import H, T0, _engine, _fill, _policy, _run

ENTRY = [_fill(0, "buy", 1.0, 1000)]
UP_THEN_BACK = [1.0] * 2 + [1.05] * 3 + [1.0] * 10


def _net(lot) -> float:
    return lot.profit_floor_net_sum / lot.profit_floor_exits


def _engine_bars(bars):
    eng = _engine([1.0])
    candles = [(int(T0.timestamp()) + i * H, *b, 1.0) for i, b in enumerate(bars)]
    eng.candles_1h = lambda s: candles
    return eng


def test_default_off_keeps_old_behaviour():
    eng = _engine(UP_THEN_BACK)
    old = _run(eng, ENTRY, _policy()).lots[0]
    assert old.open_at_end and old.profit_floor_exits == 0 and old.profit_armed == 0


def test_round_trip_winner_closes_at_net_floor():
    lot = _run(_engine(UP_THEN_BACK), ENTRY, _policy(profit_arm_pct=4.0, profit_floor_net_pct=0.5)).lots[0]
    assert lot.exit_reason == "profit_floor" and not lot.open_at_end
    assert lot.profit_armed == 1 and lot.profit_floor_below == 0
    assert _net(lot) == pytest.approx(0.5, abs=1e-6)
    assert lot.pnl > 0  # without the floor the lot sits at about -0.9 % after costs


def test_not_armed_below_threshold():
    lot = _run(_engine([1.0] * 2 + [1.03] * 3 + [1.0] * 10), ENTRY, _policy(profit_arm_pct=4.0)).lots[0]
    assert lot.profit_armed == 0 and lot.open_at_end


def test_no_arm_and_sell_on_the_same_bar():
    # bar 2 spikes to +6 % and dips to -1 % inside one bar: the floor may only act from bar 3
    bars = [(1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0), (1.0, 1.06, 0.99, 1.0)] + [(1.0, 1.0, 1.0, 1.0)] * 5
    lot = _run(_engine_bars(bars), ENTRY, _policy(profit_arm_pct=4.0)).lots[0]
    assert lot.exit_reason == "profit_floor"
    assert lot.exit_ts == int(T0.timestamp()) + 3 * H


def test_gap_below_floor_fills_at_open_and_is_counted():
    bars = [(1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0), (1.0, 1.06, 1.0, 1.05), (0.97, 0.97, 0.95, 0.96)] + [(0.96, 0.96, 0.96, 0.96)] * 3
    lot = _run(_engine_bars(bars), ENTRY, _policy(profit_arm_pct=4.0, profit_floor_net_pct=0.5)).lots[0]
    assert lot.exit_reason == "profit_floor" and lot.profit_floor_below == 1
    assert _net(lot) < 0  # filled at the 0.97 open, not at the floor


def test_close_fill_sells_at_close_below_floor():
    lot = _run(_engine(UP_THEN_BACK), ENTRY, _policy(profit_arm_pct=4.0, profit_floor_net_pct=0.5, profit_fill="close")).lots[0]
    assert lot.exit_reason == "profit_floor" and lot.profit_floor_below == 1
    assert _net(lot) < 0.5


def test_trail_level_follows_peak():
    lot = _run(_engine([1.0] * 2 + [1.2] * 3 + [1.1] * 5), ENTRY,
               _policy(profit_arm_pct=8.0, profit_floor_net_pct=3.0, profit_trail_pct=3.0)).lots[0]
    assert lot.exit_reason == "profit_floor"
    level = 1.2 * 0.97
    avg = 1.0 * (1 + 0.35 / 100)
    expected = (level * (1 - 0.35 / 100) * (1 - 0.002)) / (avg * 1.002) - 1
    assert _net(lot) == pytest.approx(expected * 100, abs=1e-6)


def test_floor_level_is_net_of_costs():
    pol = _policy(profit_arm_pct=4.0, profit_floor_net_pct=0.5)
    lv = Engine._profit_floor_level(100.0, pol, 0.002, 0.35, 0.0)
    assert lv * (1 - 0.0035) * (1 - 0.002) / (100.0 * 1.002) - 1 == pytest.approx(0.005)


def test_trials_registered_honestly():
    assert trials.SCENARIO_TRIALS["p"].trials == 11  # 9 grid + trail variant + close-fill variant
    assert trials.SCENARIO_TRIALS["p_t8_rerun"].trials == 0 and trials.SCENARIO_TRIALS["p_oct"].trials == 0


def test_scenario_variants_match_register():
    from backtest.harness import scenarios as sc

    names = [p.name for p, _ in sc._profit_variants()]
    assert names[0] == "baseline" and len(names) - 1 == trials.SCENARIO_TRIALS["p"].trials
    assert sc.PROFIT_PRIMARY in names and sc.PROFIT_T8 in names



def test_fee_sweep_break_even_and_sign_changes():
    from backtest.harness.fee_sweep import break_even, sign_changes

    mono = {0.0: {"x": 100.0}, 0.5: {"x": 50.0}, 1.0: {"x": -50.0}}
    assert break_even(mono, "x") == 0.75 and sign_changes(mono, "x") == 1
    flip = {0.0: {"x": 10.0}, 0.5: {"x": -10.0}, 1.0: {"x": 30.0}}
    assert break_even(flip, "x") == 0.25 and sign_changes(flip, "x") == 2
    assert break_even({0.0: {"x": -1.0}, 1.0: {"x": 2.0}}, "x") == 0.0
    assert break_even({0.0: {"x": 1.0}, 1.0: {"x": 2.0}}, "x") is None
