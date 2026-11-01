"""Backtest harness (backtest/harness): stats, clock shim, engine, verdicts.

Pure tests: no Mongo, no network, no git history (CI clones depth 1).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from backtest.harness import stats
from backtest.harness.costs import CostModel
from backtest.harness.data import BookLot, CandleCache, Fill, TenantParams, load_fills
from backtest.harness.engine import Engine, Policy, build_episodes
from backtest.harness.metrics import decide, guardrails_worse, summarize
from backtest.harness.policy import SimClock, import_real, load_module
from tools import entry_filter_replay as efr

UTC = timezone.utc
T0 = datetime(2026, 10, 1, tzinfo=UTC)
H = 3600


# --------------------------------------------------------------------- stats
@pytest.mark.parametrize("n_trials", [1, 12, 31])
def test_deflated_sharpe_matches_entry_filter_replay(n_trials):
    rng = random.Random(n_trials)
    for _ in range(20):
        r = [rng.gauss(0.002, 0.03) + (0.05 if rng.random() < 0.05 else 0.0) for _ in range(rng.randint(5, 200))]
        assert stats.deflated_sharpe(r, n_trials) == pytest.approx(efr.deflated_sharpe(r, n_trials), abs=1e-9)


def test_deflated_sharpe_falls_with_more_trials_and_undefined_is_zero():
    rng = random.Random(1)
    r = [rng.gauss(0.01, 0.02) for _ in range(60)]
    assert stats.deflated_sharpe(r, 1) >= stats.deflated_sharpe(r, 12) >= stats.deflated_sharpe(r, 100)
    assert stats.deflated_sharpe([0.1, 0.1, 0.1], 5) == 0.0
    assert stats.deflated_sharpe([0.1], 5) == 0.0


def test_pbo_dominant_strategy_is_zero():
    rng = random.Random(2)
    m = [[rng.gauss(0.0, 0.01) + (0.02 if k == 0 else 0.0) for k in range(4)] for _ in range(64)]
    assert stats.pbo_cscv(m, n_blocks=8).pbo == 0.0


def test_pbo_flipping_winner_is_high():
    # strategy 0 wins in the first half, loses in the second; 1 the reverse
    import statistics

    m = [[0.02, -0.02] if t < 32 else [-0.02, 0.02] for t in range(64)]
    res = stats.pbo_cscv(m, n_blocks=8, metric=statistics.fmean)
    assert res.n_splits == 70
    assert res.pbo > 0.5


def test_pbo_rejects_bad_input():
    with pytest.raises(ValueError):
        stats.pbo_cscv([[0.1]] * 10)
    with pytest.raises(ValueError):
        stats.pbo_cscv([])


def test_bootstrap_is_seeded_and_brackets_a_clear_effect():
    v = [0.01 + 0.001 * ((i * 37) % 11 - 5) for i in range(80)]
    a = stats.bootstrap_ci(v, block=5)
    b = stats.bootstrap_ci(v, block=5)
    assert a == b
    assert 0 < a.low <= a.estimate <= a.high
    assert a.p_le_zero == 0.0
    assert stats.bootstrap_ci([]).n == 0


# ----------------------------------------------------------------- clock shim
def _vol_params() -> dict:
    return {
        "stop_loss_pct": 50.0,
        "partial_stop_pct": 25,
        "dca": {"enabled": True, "max_rounds": 4, "interval_hours": 12, "grace_hours_after_dca": 12,
                "widen_stop_pct_per_round": 6, "loss_pct_min": -25},
    }


def test_sim_clock_drives_real_dca_grace():
    clock = SimClock(datetime(2026, 10, 10, 12, tzinfo=UTC))
    dca = load_module("strategies.dca", clock=clock)
    pos = {"amount": 10.0, "average_entry": 1.0, "dca_rounds": 1,
           "last_dca_at": "2026-10-10T13:00:00", "hard_partial_stop_done": False}  # naive Berlin = 11:00 UTC
    kw = dict(price=0.70, entry=1.0, position=pos, strategy_params=_vol_params(), base_stop_loss_pct=50.0)
    assert dca.evaluate_long_hard_stop(**kw) is None  # inside the 12 h grace
    clock.set(datetime(2026, 10, 11, 0, 30, tzinfo=UTC))
    assert dca.evaluate_long_hard_stop(**kw) == ("SELL_STOP_PARTIAL", "stop_loss")
    import strategies.dca as real

    assert real.datetime is not dca.datetime  # the bot's own module is untouched


# --------------------------------------------------------------------- engine
def _fill(ts_h: float, side: str, price: float, usdt: float, *, signal: str = "", source: str = "x",
          exit_source: str | None = None, symbol: str = "AAA/USDT") -> Fill:
    return Fill("o", "t1", symbol, "1h", side, signal or ("BUY" if side == "buy" else "SELL_FULL"), source,
                exit_source, T0 + timedelta(hours=ts_h), price, usdt / price, usdt)


def _candles(closes: list[float]) -> list[tuple]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append((int(T0.timestamp()) + i * H, o, max(o, c), min(o, c), c, 1.0))
        prev = c
    return out


def _engine(closes, *, limits=None, lock=None, tier="volatile") -> Engine:
    risk = {"min_position_usdt_for_partial_sell": 300, "min_sell_notional_usdt": 150,
            "block_partial_sell_if_sold_percent_above": 0.99}
    risk.update(limits or {})
    body = {"stop_loss_pct": 50, "volatile_altcoin": {"partial_stop_pct": 25, "dca": _vol_params()["dca"]}, "risk": risk}
    book = {}
    if lock is not None:
        book[("t1", "AAA/USDT", "1h")] = BookLot("t1", "AAA/USDT", "1h", tier, lock, ("no_auto_sell",))
    candles = _candles(closes)
    return Engine(candles_1h=lambda s: candles, tenants={"t1": TenantParams("t1", body), "default": TenantParams("default", body)},
                  book=book, positions_mod=import_real("strategies.positions"), costs=CostModel())


def _policy(escalate=True, **kw) -> Policy:
    clock = SimClock()
    return Policy("p", load_module("strategies.dca", clock=clock), clock, escalate_rejected_hard_partial=escalate, **kw)


def _run(engine, fills, policy):
    eps = build_episodes(fills)
    return engine.run(policy, eps, T0, T0 + timedelta(hours=200), carry_in=False)


DROP = [1.0] * 5 + [0.95, 0.85, 0.72, 0.70, 0.70, 0.70, 0.71]


def test_build_episodes_splits_on_flat():
    f = [_fill(0, "buy", 1.0, 100), _fill(1, "buy", 1.0, 50, signal="BUY_DCA", source="dca"),
         _fill(2, "sell", 1.0, 150), _fill(3, "buy", 1.0, 100)]
    eps = build_episodes(f)
    assert [len(e.events) for e in eps] == [3, 1]
    assert eps[0].closed_at == f[2].ts and eps[1].closed_at is None


def test_partial_stop_fires_when_lot_is_big_enough():
    r = _run(_engine(DROP), [_fill(0, "buy", 1.0, 2000)], _policy())
    lot = r.lots[0]
    assert (lot.stop_partial, lot.escalated, lot.partial_rejects) == (1, 0, 0)
    assert lot.open_at_end


def test_small_lot_rejected_without_738_and_escalated_with_it():
    fills = [_fill(0, "buy", 1.0, 350)]  # 350 * 0.75 < 300 min_position at the stop
    off = _run(_engine(DROP), fills, _policy(escalate=False)).lots[0]
    on = _run(_engine(DROP), fills, _policy(escalate=True)).lots[0]
    assert off.partial_rejects > 0 and off.stop_partial == 0 and off.open_at_end
    assert on.escalated == 1 and not on.open_at_end
    assert on.pnl > off.pnl  # price kept falling below the escalation level


def test_lock_blocks_stops():
    lock = T0 - timedelta(days=1)
    lot = _run(_engine(DROP, lock=lock), [_fill(0, "buy", 1.0, 2000)], _policy()).lots[0]
    assert lot.stop_partial == 0 and lot.locked_blocks > 0


def test_tape_sell_is_replayed_as_fraction_and_costs_apply():
    flat = [1.0] * 10
    fills = [_fill(0, "buy", 1.0, 1000), _fill(3, "sell", 1.0, 500, exit_source="trail")]
    lot = _run(_engine(flat), fills, _policy()).lots[0]
    assert lot.tape_sells_applied == 1 and lot.open_at_end
    # round trip at an unchanged price costs fee + slippage on both sides
    cost = CostModel()
    slip = cost.slippage_pct("AAA/USDT", "volatile") / 100
    expected = 1000 * ((1 - slip) / (1 + slip) * (1 - cost.fee_pct / 100) - (1 + cost.fee_pct / 100))
    assert lot.pnl == pytest.approx(expected, rel=1e-6)


def test_entry_filter_and_size_multiplier():
    fills = [_fill(0, "buy", 1.0, 2000)]
    eng = _engine(DROP)
    assert _run(eng, fills, _policy(entry_filter=lambda ep: False)).filtered_out == 1
    half = _run(eng, fills, _policy(size_multiplier=lambda ep: 0.5)).lots[0]
    full = _run(eng, fills, _policy()).lots[0]
    assert half.invested == pytest.approx(full.invested / 2)


def test_partial_guard_order_matches_risk_manager():
    g = Engine._partial_guard
    lim = {"min_position_usdt": 300, "min_notional_usdt": 150, "max_sold_percent": 0.5}
    assert g({"amount": 1000, "sold_percent": 0}, 0.5, 1.0, {**lim, "max_sold_percent": None}) == "sold_cap_unconfigured"
    assert g({"amount": 1000, "sold_percent": 0.6}, 0.5, 1.0, lim) == "sold_cap"
    assert g({"amount": 200, "sold_percent": 0}, 0.5, 1.0, lim) == "min_position"
    assert g({"amount": 400, "sold_percent": 0}, 0.25, 1.0, lim) == "min_notional"
    assert g({"amount": 400, "sold_percent": 0}, 0.5, 1.0, lim) is None


# ------------------------------------------------------------------- verdicts
def test_decide_rules():
    pos = [0.002 + 0.0005 * ((i * 13) % 7 - 3) for i in range(90)]
    assert decide(window_wins=[True, False, False], excess_daily=pos, n_trials=12, worse=[]).label == "KILL"
    assert decide(window_wins=[True, True, False], excess_daily=pos, n_trials=12, worse=["max_dd_pct"]).label == "KILL"
    assert decide(window_wins=[True, True, True], excess_daily=pos, n_trials=12, worse=[]).label == "GO"
    noise = [0.01 * ((i * 17) % 9 - 4) for i in range(90)]
    assert decide(window_wins=[True, True, False], excess_daily=noise, n_trials=12, worse=[]).label == "OBSERVE"


def test_guardrail_tolerance():
    r = _run(_engine(DROP), [_fill(0, "buy", 1.0, 2000)], _policy())
    m = summarize(r)
    from dataclasses import replace

    assert guardrails_worse(m, replace(m, max_dd_pct=m.max_dd_pct * 1.005)) == []
    assert guardrails_worse(m, replace(m, max_dd_pct=m.max_dd_pct * 1.05 + 0.1)) == ["max_dd_pct"]


# ----------------------------------------------------------------------- data
def test_candle_cache_roundtrip_and_gate_import(tmp_path):
    cache = CandleCache(tmp_path / "c")
    cache.put("AAA/USDT", "1h", [(7200, 1, 2, 0.5, 1.5, 9), (3600, 1, 1, 1, 1, 1)])
    assert [c[0] for c in cache.get("AAA/USDT", "1h")] == [3600, 7200]
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "BBB_USDT.json").write_text(json.dumps([["3600", "10", "1.1", "1.2", "0.9", "1.0", "9", "true"],
                                                   ["7200", "10", "1.1", "1.2", "0.9", "1.0", "9", "false"]]))
    cache.import_gate_json_dir(raw)
    got = cache.get("BBB/USDT", "1h")
    assert len(got) == 1 and got[0][1:5] == (1.0, 1.2, 0.9, 1.1)


def test_load_fills_reads_filled_long_orders(tmp_path):
    rows = [
        {"_id": "1", "status": "filled", "side": "buy", "tenant_id": "t1", "symbol": "AAA/USDT", "timeframe": "1h",
         "signal": "BUY", "source": "x", "timestamps": {"filled": "2026-10-01T12:00:00"},
         "execution": {"price": 2.0, "amount": 5.0, "usdt": 10.0}},
        {"_id": "2", "status": "cancelled", "side": "buy", "tenant_id": "t1", "symbol": "AAA/USDT",
         "timestamps": {"filled": "2026-10-01T12:00:00"}, "execution": {"price": 2.0, "amount": 5.0}},
        {"_id": "3", "status": "filled", "side": "buy", "tenant_id": "decisions", "symbol": "AAA/USDT",
         "timestamps": {"filled": "2026-10-01T12:00:00"}, "execution": {"price": 2.0, "amount": 5.0}},
    ]
    p = tmp_path / "o.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    fills = load_fills(p)
    assert len(fills) == 1
    assert fills[0].ts == datetime(2026, 10, 1, 10, tzinfo=UTC)  # naive Berlin CEST
