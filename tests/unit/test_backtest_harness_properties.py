"""Property tests (hypothesis) for the backtest harness invariants."""

from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backtest.harness import stats
from backtest.harness.costs import CostModel
from tests.unit.test_backtest_harness import T0, _engine, _fill, _policy, _run

prices = st.lists(st.floats(min_value=0.05, max_value=5.0, allow_nan=False), min_size=4, max_size=60)
rets = st.lists(st.floats(min_value=-0.5, max_value=0.5, allow_nan=False), min_size=3, max_size=120)
SET = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SET
@given(mid=st.floats(min_value=1e-8, max_value=1e6), tier=st.sampled_from(["base", "stress", "hard_stress"]),
       bot_tier=st.sampled_from(["volatile", "stable", None]), symbol=st.sampled_from(["BTC/USDT", "PEPE/USDT"]))
def test_costs_never_favour_the_bot(mid, tier, bot_tier, symbol):
    c = CostModel(tier=tier)
    slip = c.slippage_pct(symbol, bot_tier)
    assert slip >= 0
    assert c.buy_price(mid, slip) >= mid >= c.sell_price(mid, slip)


@SET
@given(closes=prices, usdt=st.floats(min_value=50, max_value=5000), escalate=st.booleans())
def test_lot_accounting_invariants(closes, usdt, escalate):
    r = _run(_engine([1.0] + closes), [_fill(0, "buy", 1.0, usdt)], _policy(escalate=escalate))
    for lot in r.lots:
        assert lot.invested > 0 and lot.proceeds >= 0
        assert lot.ret >= -1.0
        assert lot.stop_partial <= 1  # one hard partial per lot
        assert lot.stop_full + lot.escalated <= 1
        if lot.open_at_end:
            assert lot.end_cost_basis > 0


@SET
@given(closes=st.lists(st.floats(min_value=0.0, max_value=0.2), min_size=4, max_size=40))
def test_rising_path_never_stops(closes):
    path, p = [1.0], 1.0
    for step in closes:
        p *= 1.0 + step
        path.append(p)
    lot = _run(_engine(path), [_fill(0, "buy", 1.0, 1000)], _policy()).lots[0]
    assert lot.stop_full == lot.stop_partial == lot.escalated == 0


@SET
@given(closes=prices)
def test_escalation_only_removes_rejections(closes):
    fills = [_fill(0, "buy", 1.0, 350)]
    off = _run(_engine([1.0] + closes), fills, _policy(escalate=False)).lots[0]
    on = _run(_engine([1.0] + closes), fills, _policy(escalate=True)).lots[0]
    assert on.partial_rejects <= off.partial_rejects
    assert on.stop_partial == off.stop_partial or on.escalated == 1


@SET
@given(r=rets, n=st.integers(min_value=1, max_value=200))
def test_dsr_is_a_probability_and_monotone(r, n):
    d1, dn = stats.deflated_sharpe(r, 1), stats.deflated_sharpe(r, n)
    assert 0.0 <= dn <= 1.0
    assert dn <= d1 + 1e-12


@SET
@given(st.lists(st.lists(st.floats(min_value=-0.1, max_value=0.1), min_size=3, max_size=3), min_size=16, max_size=64))
def test_pbo_is_a_probability(matrix):
    res = stats.pbo_cscv(matrix, n_blocks=4)
    assert 0.0 <= res.pbo <= 1.0
    assert all(0.0 < w < 1.0 for w in res.oos_rank_of_is_best)


@SET
@given(r=rets)
def test_bootstrap_bounds_ordered(r):
    ci = stats.bootstrap_ci(r, resamples=200, block=3)
    assert ci.low <= ci.high
    assert min(r) - 1e-12 <= ci.low and ci.high <= max(r) + 1e-12


def test_horizon_is_respected():
    r = _run(_engine([1.0] * 300), [_fill(0, "buy", 1.0, 100)], _policy())
    assert max(r.pnl_curve) < int((T0 + timedelta(hours=200)).timestamp())
