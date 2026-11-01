"""DCA caps, cooldown, time stop and bar-exit hooks of the harness engine (2026-11-01, long walk-forward).

Pure tests: no Mongo, no network.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backtest.harness.engine import Engine, _Indicators
from tests.unit.test_backtest_harness import T0, _candles, _engine, _fill, _policy, _run

FLAT = [1.0] * 40


def _adds(n, usdt=1000.0, price=0.9):
    return [_fill(2 + i, "buy", price, usdt, signal="BUY_DCA", source="dca") for i in range(n)]


def test_defaults_replay_every_tape_add():
    lot = _run(_engine([1.0] * 3 + [0.9] * 37), [_fill(0, "buy", 1.0, 1000)] + _adds(4), _policy()).lots[0]
    assert lot.dca_adds == 4 and lot.capped_adds == 0


def test_max_adds_caps_tape_adds():
    lot = _run(_engine([1.0] * 3 + [0.9] * 37), [_fill(0, "buy", 1.0, 1000)] + _adds(4), _policy(max_adds=2)).lots[0]
    assert lot.dca_adds == 2 and lot.skipped_adds == 2


def test_budget_truncates_last_add():
    lot = _run(_engine([1.0] * 3 + [0.9] * 37), [_fill(0, "buy", 1.0, 1000)] + _adds(3, usdt=800),
               _policy(dca_budget_mult=2.0)).lots[0]
    assert lot.dca_adds == 3
    assert lot.add_usdt == pytest.approx(2000.0)  # 800 + 800 + 400 (truncated)


def test_dca_gate_blocks_adds():
    lot = _run(_engine([1.0] * 3 + [0.9] * 37), [_fill(0, "buy", 1.0, 1000)] + _adds(2),
               _policy(dca_gate=lambda ts: False)).lots[0]
    assert lot.dca_adds == 0 and lot.skipped_adds == 2


def test_time_stop_closes_old_loser_only():
    closes = [1.0] * 2 + [0.8] * 400
    eng = _engine(closes)
    eps_fills = [_fill(0, "buy", 1.0, 1000)]
    from backtest.harness.engine import build_episodes

    eps = build_episodes(eps_fills)
    pol = _policy(time_limit_days=14.0, time_limit_loss_pct=15.0, stops_enabled=False)
    lot = eng.run(pol, eps, T0, T0 + timedelta(hours=400), carry_in=False).lots[0]
    assert lot.time_stops == 1 and not lot.open_at_end and lot.exit_reason == "time"
    assert lot.exit_ts - int(lot.entry_ts.timestamp()) >= 14 * 86400 - 3600
    winner = _engine([1.0] * 400)
    lot2 = winner.run(pol, eps, T0, T0 + timedelta(hours=400), carry_in=False).lots[0]
    assert lot2.time_stops == 0 and lot2.open_at_end


def test_cooldown_after_stop_filters_reentry():
    from backtest.harness.engine import build_episodes

    eng = _engine([1.0] * 2 + [0.5] * 60)
    fills = [_fill(0, "buy", 1.0, 1000), _fill(20, "sell", 0.5, 1000), _fill(30, "buy", 0.5, 1000)]
    eps = build_episodes(fills)
    assert len(eps) == 2
    end = T0 + timedelta(hours=60)
    on = eng.run(_policy(early_exit_pct=10.0, cooldown_after_stop_h=48.0), eps, T0, end, carry_in=False)
    off = eng.run(_policy(early_exit_pct=10.0), eps, T0, end, carry_in=False)
    assert on.lots[0].exit_reason in ("stop", "early")
    assert len(on.lots) == 1 and on.filtered_out == 1
    assert len(off.lots) == 2
    short = eng.run(_policy(early_exit_pct=10.0, cooldown_after_stop_h=12.0), eps, T0, end, carry_in=False)
    assert len(short.lots) == 2


def test_bar_exit_hook_sells_at_close():
    eng = _engine([1.0] * 5 + [1.2] * 10)
    lot = _run(eng, [_fill(0, "buy", 1.0, 1000)], _policy(bar_exit=lambda s, i, c, pos: c[i][4] >= 1.2)).lots[0]
    assert lot.bar_exits == 1 and lot.exit_reason == "bar_exit" and lot.pnl > 0


def test_fast_indicators_match_slow():
    import math

    closes = [1.0 + 0.1 * math.sin(i / 3.0) + 0.002 * i for i in range(200)]
    ind = _Indicators(_candles(closes))
    for i in (21, 50, 199):
        a, b = ind.at(i), ind.at_fast(i)
        for k in a:
            assert b[k] == pytest.approx(a[k], rel=1e-9, abs=1e-9)
    assert Engine  # imported for the public surface
