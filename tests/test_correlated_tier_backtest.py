#!/usr/bin/env python3
"""Fixture tests for scripts/backtest_correlated_tier_90d.py — NO network.

Mirrors tests/test_volume_ignition_backtest.py: the four places a backtest
typically lies, plus config isolation for the baseline-vs-experiment split.

  python3.13 -m pytest tests/test_correlated_tier_backtest.py -v
  python3.13 tests/test_correlated_tier_backtest.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_correlated_tier_90d as BT  # noqa: E402

HOUR = 3600
T0 = 1_700_000_000


def _bars(n: int, start_px: float = 100.0, vol: float = 10_000.0, drift: float = 0.0) -> list[list[float]]:
    bars: list[list[float]] = []
    px = start_px
    for i in range(n):
        o = px
        c = px * (1.0 + drift)
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        bars.append([T0 + i * HOUR, o, h, l, c, vol])
        px = c
    return bars


def _knobs(**over) -> BT.SimKnobs:
    kw = dict(
        fee_rt=0.002,
        slip_bps=25.0,
        ticket=500.0,
        max_open=6,
        participation=0.02,
        min_ticket=50.0,
        start_equity=10_000.0,
        cash_floor=0.0,
        timeframe="1h",
    )
    kw.update(over)
    return BT.SimKnobs(**kw)


def _cfg(*, ct: bool = False, stagnant: bool = False) -> dict:
    return {
        "max_open_positions": 6,
        "max_usdt_per_trade": 500,
        "initial_capital_usdt": 10_000,
        "sell_policy": {
            "mode": "active",
            "rotation": {
                "stagnant_rotation_enabled": stagnant,
                "stagnant_gain_pct": 8.0,
                "stagnant_idle_hours": 24.0,
                "stagnant_slack_slots": 2,
            },
            "correlated_tier": {
                "enabled": ct,
                "groups": {
                    "us_stock": {
                        "proxy_symbols": ["CRWVG/USDT"],
                        "member_symbols": ["CRWVG/USDT"],
                    },
                    "crypto_market": {
                        "proxy_symbols": ["BTC/USDT"],
                        "member_symbols": "*",
                    },
                },
            },
        },
    }


def _buy_on_ts(target_ts: int):
    def fn(payload):
        if payload["has_position"]:
            return None
        if int(payload["row"]["ts"]) == int(target_ts):
            return {"action": "BUY"}
        return None

    return fn


def _always_buy_if_flat(payload):
    if payload["has_position"]:
        return None
    return {"action": "BUY"}


def _always_hold(_payload):
    return None


# ------------------------------------------------------------------ tests ---

def test_causality_no_lookahead():
    """Cutting everything after the signal bar must yield the same signal.

    Entry price is the OPEN of the next bar — not the signal bar's close.
    """
    bars = _bars(30, start_px=100.0, drift=0.002)
    signal_idx = 22
    # Gap the next open away from this close so we can tell them apart.
    bars[signal_idx + 1][1] = bars[signal_idx][4] * 1.05
    signal_ts = bars[signal_idx][0]
    data = {"AAA/USDT": bars}
    r = BT.simulate_portfolio(
        data, _cfg(), _knobs(), decision_fn=_buy_on_ts(signal_ts), warmup_ts=0,
    )
    sigs = [s for s in r["signals"] if s["symbol"] == "AAA/USDT"]
    assert sigs, "expected a buy signal"
    sig = sigs[0]
    assert sig["ts"] == signal_ts
    assert sig["entry_idx"] == signal_idx + 1
    assert sig["entry_price"] == bars[signal_idx + 1][1]
    assert sig["signal_close"] == bars[signal_idx][4]
    assert sig["entry_price"] != sig["signal_close"]

    trunc = {"AAA/USDT": bars[: signal_idx + 1]}
    again = BT.simulate_portfolio(
        trunc, _cfg(), _knobs(), decision_fn=_buy_on_ts(signal_ts), warmup_ts=0,
    )
    again_sigs = [s for s in again["signals"] if s["symbol"] == "AAA/USDT"]
    assert again_sigs, "truncated series must still produce the signal"
    assert again_sigs[0]["ts"] == sig["ts"]
    # No next bar in the truncated series, so the engine must NOT fill at
    # the signal close (that would be lookahead).
    buys = [t for t in again["trades"] if t.get("type") == "BUY"]
    assert buys == []


def test_entry_is_next_bar_open():
    """Fill uses the next bar's OPEN (plus slip), never this bar's close."""
    bars = _bars(30, start_px=100.0, drift=0.003)
    signal_idx = 22
    bars[signal_idx + 1][1] = bars[signal_idx][4] * 1.04
    signal_ts = bars[signal_idx][0]
    next_open = bars[signal_idx + 1][1]
    r = BT.simulate_portfolio(
        {"AAA/USDT": bars},
        _cfg(),
        _knobs(slip_bps=0.0),
        decision_fn=_buy_on_ts(signal_ts),
        warmup_ts=0,
    )
    buys = [t for t in r["trades"] if t.get("type") == "BUY"]
    assert buys
    assert buys[0]["raw_open"] == next_open
    assert buys[0]["fill_price"] == next_open
    assert buys[0]["signal_close"] == bars[signal_idx][4]


def test_cost_model_on_synthetic_fill():
    """fee + slip + participation match the volume-ignition formulas."""
    raw = 100.0
    buy_px = BT.fill_price("buy", raw, 25.0)
    sell_px = BT.fill_price("sell", raw, 25.0)
    assert abs(buy_px - 100.25) < 1e-9
    assert abs(sell_px - 99.75) < 1e-9

    # 2% of a 1_000 USDT bar cannot fill a 500 ticket
    assert BT.realizable_notional(500.0, 1_000.0, 0.02, 50.0) == 0.0
    # 2% of a 40_000 USDT bar = 800, ticket 500 → 500
    assert BT.realizable_notional(500.0, 40_000.0, 0.02, 50.0) == 500.0
    # 2% of a 10_000 USDT bar = 200
    assert BT.realizable_notional(500.0, 10_000.0, 0.02, 50.0) == 200.0

    net = BT.round_trip_net(100.25, 99.75, 0.002)
    assert net < 0
    # gross = 99.75/100.25 - 1 ≈ -0.004987; minus 0.002 fee
    assert abs(net - ((99.75 / 100.25 - 1.0) - 0.002)) < 1e-12


def test_max_open_is_enforced():
    """Portfolio never exceeds max_open_positions."""
    a = _bars(28, start_px=10.0, vol=1_000_000.0)
    b = _bars(28, start_px=20.0, vol=1_000_000.0)
    data = {"AAA/USDT": a, "BBB/USDT": b}
    r = BT.simulate_portfolio(
        data,
        _cfg(),
        _knobs(max_open=1, ticket=100.0, min_ticket=10.0, participation=1.0),
        decision_fn=_always_buy_if_flat,
        warmup_ts=0,
    )
    assert r["peak_open"] <= 1
    assert r["skipped_no_slot"] >= 1
    buys = [t for t in r["trades"] if t.get("type") == "BUY"]
    # one slot: at most one concurrent position; end-of-window mark-out may
    # free the slot but peak_open is the invariant that matters
    assert r["peak_open"] == 1
    assert len(buys) >= 1


def test_baseline_vs_experiment_config_isolation():
    """Baseline pass never sees enabled=true; experiment never sees false."""
    bars = _bars(26, start_px=50.0, vol=1_000_000.0)
    data = {"AAA/USDT": bars}
    raw = {
        "max_open_positions": 6,
        "max_usdt_per_trade": 500,
        "initial_capital_usdt": 10_000,
        "sell_policy": {
            "mode": "active",
            "rotation": {
                "stagnant_rotation_enabled": False,
                "stagnant_gain_pct": 8.0,
                "stagnant_idle_hours": 24.0,
                "stagnant_slack_slots": 2,
            },
            "correlated_tier": {
                "enabled": False,
                "groups": {
                    "us_stock": {
                        "proxy_symbols": ["CRWVG/USDT"],
                        "member_symbols": ["CRWVG/USDT"],
                    },
                    "crypto_market": {
                        "proxy_symbols": ["BTC/USDT"],
                        "member_symbols": "*",
                    },
                },
            },
        },
        "entry_sensor_15m": {"enabled": True},
        "exit_sensor": {"enabled": True},
        "dca_sniper": {"enabled": True},
    }
    baseline, experiment = BT.build_pass_configs(raw)
    # persisted-shaped source is not mutated
    assert raw["sell_policy"]["correlated_tier"]["enabled"] is False
    assert raw["sell_policy"]["rotation"]["stagnant_rotation_enabled"] is False
    assert baseline["sell_policy"]["correlated_tier"]["enabled"] is False
    assert baseline["sell_policy"]["rotation"]["stagnant_rotation_enabled"] is False
    assert experiment["sell_policy"]["correlated_tier"]["enabled"] is True
    assert experiment["sell_policy"]["rotation"]["stagnant_rotation_enabled"] is True
    # tuned values survive the copy
    assert experiment["sell_policy"]["rotation"]["stagnant_gain_pct"] == 8.0
    assert experiment["sell_policy"]["rotation"]["stagnant_idle_hours"] == 24.0

    rb = BT.simulate_portfolio(
        data, baseline, _knobs(), decision_fn=_always_hold, warmup_ts=0, spy_overlay=True,
    )
    re = BT.simulate_portfolio(
        data, experiment, _knobs(), decision_fn=_always_hold, warmup_ts=0, spy_overlay=True,
    )
    assert rb["overlay_enabled_seen"], "baseline must call overlay so we can spy it"
    assert re["overlay_enabled_seen"], "experiment must call overlay so we can spy it"
    assert all(v is False for v in rb["overlay_enabled_seen"])
    assert all(v is True for v in re["overlay_enabled_seen"])
    assert all(v is False for v in rb["rotation_stagnant_seen"])
    assert all(v is True for v in re["rotation_stagnant_seen"])


def test_zero_trades_report_does_not_crash():
    """Skip counters and report helpers survive n=0."""
    bars = _bars(25, start_px=1.0)
    r = BT.simulate_portfolio(
        {"AAA/USDT": bars},
        _cfg(),
        _knobs(),
        decision_fn=_always_hold,
        warmup_ts=0,
    )
    assert r["n"] == 0
    assert r["note"] == "keine Trades"
    assert "skipped_no_slot" in r
    stripped = BT.strip_trades(r)
    assert "trades" not in stripped
    report = {
        "baseline": stripped,
        "experiment": stripped,
        "limitations": BT.LIMITATIONS,
        "benchmark": {"btc_buy_hold": {"btc_buy_hold_pct": 0.0}},
    }
    dumped = __import__("json").dumps(report, default=str)
    assert "keine Trades" in dumped
    assert "limitations" in dumped


def test_build_pass_configs_does_not_alias_nested_dicts():
    raw = _cfg(ct=False, stagnant=False)
    raw["sell_policy"]["rotation"]["stagnant_gain_pct"] = 8.0
    b, e = BT.build_pass_configs(raw)
    e["sell_policy"]["correlated_tier"]["enabled"] = True
    e["sell_policy"]["rotation"]["stagnant_gain_pct"] = 99.0
    assert b["sell_policy"]["correlated_tier"]["enabled"] is False
    assert b["sell_policy"]["rotation"]["stagnant_gain_pct"] == 8.0
    assert raw["sell_policy"]["rotation"]["stagnant_gain_pct"] == 8.0


# ---------------------------------------------------------- Phase 2 ---------

def test_regime_label_thresholds():
    """7d BTC return buckets: < -10% risk_off, -10..+5 chop, > +5 risk_on."""
    assert BT.regime_label(None) == "unknown_bucket"
    assert BT.regime_label(-0.1001) == "risk_off_bucket"
    assert BT.regime_label(-0.10) == "chop_bucket"
    assert BT.regime_label(0.0) == "chop_bucket"
    assert BT.regime_label(0.05) == "chop_bucket"
    assert BT.regime_label(0.0501) == "risk_on_bucket"


def test_regime_from_btc_bars_uses_7d_lookback():
    """Label at ts uses close(ts) / close(ts-7d) - 1, not the whole-window return."""
    # 14 days of 1h bars so a 7d lookback always lands on a real bar.
    # days 0-6: 100, 7-10: 85, 11-13: 110
    bars: list[list[float]] = []
    for i in range(14 * 24):
        day = i // 24
        if day < 7:
            px = 100.0
        elif day < 11:
            px = 85.0
        else:
            px = 110.0
        ts = T0 + i * HOUR
        bars.append([ts, px, px, px, px, 1.0])

    # Day 10 (crash): 7d ago is day 3 at 100 → 85/100 - 1 = -15% → risk_off
    ts_crash = T0 + 10 * 24 * HOUR
    ret = BT.rolling_btc_return(bars, ts_crash, lookback_sec=7 * 86400)
    assert ret is not None
    assert abs(ret - (85.0 / 100.0 - 1.0)) < 1e-9
    assert BT.regime_label(ret) == "risk_off_bucket"

    # Day 13 (bounce): 7d ago is day 6 at 100 → 110/100 - 1 = +10% → risk_on
    ts_bounce = T0 + 13 * 24 * HOUR
    ret_b = BT.rolling_btc_return(bars, ts_bounce, lookback_sec=7 * 86400)
    assert abs(ret_b - 0.10) < 1e-9
    assert BT.regime_label(ret_b) == "risk_on_bucket"

    # Before any 7d history exists the lookback is missing, not a fabricated 0.
    assert BT.rolling_btc_return(bars, T0 + 2 * 24 * HOUR, lookback_sec=7 * 86400) is None


def test_aggregate_trades_by_regime_splits_pnl():
    """Post-hoc join: SELL pnl is bucketed by fill_ts against BTC 7d return."""
    # 16 days flat 100 then a 20% dump so later fills land in risk_off.
    bars: list[list[float]] = []
    for i in range(16 * 24):
        px = 100.0 if i < 10 * 24 else 80.0
        bars.append([T0 + i * HOUR, px, px, px, px, 1.0])
    early = T0 + 8 * 24 * HOUR   # 8d in, 7d lookback still ~100 → 0% chop
    late = T0 + 15 * 24 * HOUR   # 15d in, 7d lookback is 80/100 or 80/80
    trades = [
        {"type": "SELL", "group": "us_stock", "fill_ts": early, "pnl": 10.0, "net_pct": 2.0},
        {"type": "SELL", "group": "us_stock", "fill_ts": late, "pnl": -30.0, "net_pct": -6.0},
        {"type": "SELL", "group": "crypto_market", "fill_ts": early, "pnl": 1.0, "net_pct": 0.1},
        {"type": "BUY", "group": "us_stock", "fill_ts": early, "pnl": 0.0},
    ]
    got = BT.aggregate_trades_by_regime(trades, bars)
    assert "chop_bucket" in got
    chop = got["chop_bucket"]
    assert chop["n"] == 2  # two SELLs in chop (early us_stock + early crypto)
    assert chop["by_group"]["us_stock"]["n"] == 1
    assert chop["by_group"]["us_stock"]["total_pnl_usdt"] == 10.0
    # late bar: 80 / close(7d earlier). 7d earlier is still in the 80 regime
    # (day 8 is 100, day 15-7=day 8 → 80/100-1 = -20% → risk_off)
    assert "risk_off_bucket" in got
    assert got["risk_off_bucket"]["by_group"]["us_stock"]["total_pnl_usdt"] == -30.0


def test_shuffled_plan_is_seed_deterministic():
    """Same seed + same inputs → identical entry timestamps. Different seed ≠."""
    bars = _bars(80, start_px=10.0, vol=1_000_000.0)
    data = {"AAA/USDT": bars, "BBB/USDT": bars}
    targets = {"AAA/USDT": 3, "BBB/USDT": 2}
    holds = {"AAA/USDT": [4 * HOUR], "BBB/USDT": [2 * HOUR]}
    a = BT.shuffle_entry_plan(data, targets, holds, seed=42, warmup_ts=T0)
    b = BT.shuffle_entry_plan(data, targets, holds, seed=42, warmup_ts=T0)
    c = BT.shuffle_entry_plan(data, targets, holds, seed=99, warmup_ts=T0)
    assert a == b
    assert a != c
    # total planned entries matches the target counts
    assert len(a["AAA/USDT"]) == 3
    assert len(b["BBB/USDT"]) == 2


def test_shuffled_plan_stays_inside_symbol_history():
    """Entries are picked from that symbol's own bars, after warmup, with room for the hold."""
    short = _bars(40, start_px=5.0)
    long = _bars(90, start_px=8.0)
    data = {"SHORT/USDT": short, "LONG/USDT": long}
    plan = BT.shuffle_entry_plan(
        data,
        {"SHORT/USDT": 2, "LONG/USDT": 4},
        {"SHORT/USDT": [3 * HOUR], "LONG/USDT": [5 * HOUR]},
        seed=7,
        warmup_ts=T0 + 20 * HOUR,
    )
    short_ts = {int(b[0]) for b in short}
    long_ts = {int(b[0]) for b in long}
    for entry_ts, exit_ts in plan["SHORT/USDT"]:
        assert entry_ts in short_ts
        assert entry_ts >= T0 + 20 * HOUR
        assert exit_ts > entry_ts
        assert exit_ts in short_ts
    for entry_ts, exit_ts in plan["LONG/USDT"]:
        assert entry_ts in long_ts
        assert entry_ts >= T0 + 20 * HOUR


def test_buy_count_targets_scales_to_n_buys():
    """Shuffled pass aims at filled-buy count, not the raw (mostly skipped) signal count."""
    sigs = (
        [{"symbol": "AAA/USDT", "action": "BUY"}] * 8
        + [{"symbol": "BBB/USDT", "action": "BUY"}] * 2
        + [{"symbol": "AAA/USDT", "action": "SELL_FULL"}] * 3
    )
    got = BT.buy_count_targets(sigs, n_buys=5)
    assert sum(got.values()) == 5
    assert got["AAA/USDT"] >= got["BBB/USDT"]
    assert set(got) <= {"AAA/USDT", "BBB/USDT"}


def test_rolling_fold_bounds_follow_hermes_half_open():
    """90d window, 30d fold, 30d step → 3 non-overlapping [start, start+fold) windows."""
    start = T0
    end = T0 + 90 * 86400
    folds = BT.rolling_fold_bounds(start, end, fold_days=30, step_days=30)
    assert len(folds) == 3
    assert folds[0] == (0, start, start + 30 * 86400)
    assert folds[1] == (1, start + 30 * 86400, start + 60 * 86400)
    assert folds[2] == (2, start + 60 * 86400, start + 90 * 86400)
    # half-open: fold i end == fold i+1 start, no interior overlap
    assert folds[0][2] == folds[1][1]
    assert folds[1][2] == folds[2][1]
    # a 20-day window cannot host a 30-day fold
    assert BT.rolling_fold_bounds(start, start + 20 * 86400, 30, 30) == []


def test_rolling_fold_bounds_step_smaller_than_fold():
    """Hermes default pattern: fold_days=30, step_days=20 on 90d → 4 folds."""
    start = T0
    end = T0 + 90 * 86400
    folds = BT.rolling_fold_bounds(start, end, fold_days=30, step_days=20)
    assert len(folds) == 4
    assert folds[0][1] == start
    assert folds[-1][2] == start + 90 * 86400
    # last fold still fits exactly: 60+30=90
    assert folds[-1][1] == start + 60 * 86400


# ---------------------------------------------------------- Phase 3 ---------

def test_capacity_reject_matches_risk_manager_max_open_gate():
    """Only a flat new BUY at a full book is a capacity reject.

    Mirrors risk/risk_manager.py: when `not has_position` and
    `open_slots >= cap.max_open_eff` the decision is
    RiskDecision(code='max_open_positions'). DCA, already-open symbols,
    sells, and a book with a free slot are not this code.
    """
    assert BT.CAPACITY_REJECT_CODE == "max_open_positions"
    assert BT.is_capacity_rejection(
        action="BUY", has_position=False, is_dca=False, open_slots=36, max_open_eff=36,
    ) is True
    assert BT.is_capacity_rejection(
        action="BUY", has_position=False, is_dca=False, open_slots=19, max_open_eff=18,
    ) is True
    assert BT.is_capacity_rejection(
        action="BUY", has_position=False, is_dca=False, open_slots=35, max_open_eff=36,
    ) is False
    assert BT.is_capacity_rejection(
        action="BUY", has_position=True, is_dca=False, open_slots=36, max_open_eff=36,
    ) is False
    assert BT.is_capacity_rejection(
        action="BUY_DCA", has_position=False, is_dca=True, open_slots=36, max_open_eff=36,
    ) is False
    assert BT.is_capacity_rejection(
        action="SELL_FULL", has_position=True, is_dca=False, open_slots=36, max_open_eff=36,
    ) is False
    # max_open_eff<=0 is not a meaningful ceiling (disabled / unset)
    assert BT.is_capacity_rejection(
        action="BUY", has_position=False, is_dca=False, open_slots=0, max_open_eff=0,
    ) is False


def test_fixed_horizon_return_is_a_price_lookup_not_a_shadow_trade():
    """24h / 72h / 7d = first close at or after entry_ts + horizon. No exit logic."""
    bars: list[list[float]] = []
    for i in range(10 * 24):
        day = i // 24
        px = 100.0 + day
        ts = T0 + i * HOUR
        bars.append([ts, px, px, px, px, 1.0])

    r24 = BT.fixed_horizon_return(bars, T0, 100.0, 24 * HOUR)
    assert r24 is not None
    assert r24["exit_ts"] == T0 + 24 * HOUR
    assert r24["exit_price"] == 101.0
    assert abs(r24["ret"] - 0.01) < 1e-12

    r72 = BT.fixed_horizon_return(bars, T0, 100.0, 72 * HOUR)
    assert r72 is not None
    assert abs(r72["ret"] - 0.03) < 1e-12

    r7d = BT.fixed_horizon_return(bars, T0, 100.0, 7 * 86400)
    assert r7d is not None
    assert abs(r7d["ret"] - 0.07) < 1e-12

    # past the tape → None; do not silently mark out at last close
    assert BT.fixed_horizon_return(bars, T0, 100.0, 30 * 86400) is None
    assert BT.fixed_horizon_return(bars, T0, 0.0, 24 * HOUR) is None
    assert BT.fixed_horizon_return([], T0, 100.0, 24 * HOUR) is None


def test_summarize_horizon_returns_mean_median_pct_positive():
    """Distribution helper used by the Phase-3 reject-vs-taken comparison."""
    empty = BT.summarize_horizon_returns([])
    assert empty["n"] == 0
    assert empty["mean"] is None
    assert empty["median"] is None
    assert empty["pct_positive"] is None

    got = BT.summarize_horizon_returns([0.10, -0.05, 0.00, 0.20])
    assert got["n"] == 4
    assert abs(got["mean"] - 0.0625) < 1e-12
    assert abs(got["median"] - 0.05) < 1e-12
    assert abs(got["pct_positive"] - 0.5) < 1e-12


def test_match_rotation_redeploy_detects_waiting_candidate():
    """A BUY that fills shortly after a stagnant fire, and was just rejected, is a redeploy."""
    fire_ts = T0 + 10 * HOUR
    trades = [
        {
            "type": "SELL", "symbol": "OLD/USDT", "exit": "stagnant_rotation",
            "fill_ts": fire_ts, "fill_price": 10.0, "signal_ts": fire_ts - HOUR,
            "net_pct": 6.0, "pnl": 30.0, "group": "crypto_market",
        },
        {
            "type": "BUY", "symbol": "NEW/USDT",
            "fill_ts": fire_ts + HOUR, "fill_price": 20.0, "signal_ts": fire_ts,
            "group": "crypto_market",
        },
        {
            "type": "SELL", "symbol": "NEW/USDT", "exit": "bb_upper",
            "fill_ts": fire_ts + 20 * HOUR, "fill_price": 22.0,
            "signal_ts": fire_ts + 19 * HOUR, "net_pct": 8.0, "pnl": 40.0,
            "group": "crypto_market",
        },
    ]
    rejects = [
        {
            "symbol": "NEW/USDT", "fill_ts": fire_ts, "signal_ts": fire_ts - HOUR,
            "would_be_entry_price": 19.9, "code": "max_open_positions",
        },
    ]
    got = BT.match_rotation_redeploys(trades, rejects, window_sec=4 * HOUR)
    assert len(got) == 1
    row = got[0]
    assert row["rotated_symbol"] == "OLD/USDT"
    assert row["admitted"] is not None
    assert row["admitted"]["symbol"] == "NEW/USDT"
    assert row["had_waiting_reject"] is True
    assert row["waiting_reject"]["symbol"] == "NEW/USDT"
    assert row["admitted_realized"] is not None
    assert row["admitted_realized"]["net_pct"] == 8.0


def test_match_rotation_redeploy_no_candidate_waiting():
    """A fire with no nearby reject and no subsequent BUY is a no-redeploy result."""
    fire_ts = T0 + 10 * HOUR
    trades = [
        {
            "type": "SELL", "symbol": "OLD/USDT", "exit": "stagnant_rotation",
            "fill_ts": fire_ts, "fill_price": 10.0, "signal_ts": fire_ts - HOUR,
        },
    ]
    got = BT.match_rotation_redeploys(trades, [], window_sec=4 * HOUR)
    assert len(got) == 1
    assert got[0]["admitted"] is None
    assert got[0]["had_waiting_reject"] is False
    assert got[0]["waiting_reject"] is None


def test_simulate_logs_capacity_reject_not_cash_or_illiquid():
    """skipped_no_slot events are logged with code=max_open_positions; cash/illiquid are not."""
    a = _bars(28, start_px=10.0, vol=1_000_000.0)
    b = _bars(28, start_px=20.0, vol=1_000_000.0)
    data = {"AAA/USDT": a, "BBB/USDT": b}
    r = BT.simulate_portfolio(
        data,
        _cfg(),
        _knobs(max_open=1, ticket=100.0, min_ticket=10.0, participation=1.0),
        decision_fn=_always_buy_if_flat,
        warmup_ts=0,
    )
    assert r["skipped_no_slot"] >= 1
    recs = r.get("capacity_rejections") or []
    assert recs, "capacity-rejected BUYs must be logged individually"
    assert all(c.get("code") == "max_open_positions" for c in recs)
    assert all(c.get("would_be_entry_price", 0) > 0 for c in recs)
    assert {c["symbol"] for c in recs} <= {"AAA/USDT", "BBB/USDT"}

    # cash-floor skips must not be mis-tagged as capacity
    cash_blocked = BT.simulate_portfolio(
        {"AAA/USDT": _bars(28, start_px=10.0, vol=1_000_000.0)},
        _cfg(),
        _knobs(max_open=6, ticket=100.0, min_ticket=10.0, participation=1.0, cash_floor=99_999.0),
        decision_fn=_always_buy_if_flat,
        warmup_ts=0,
    )
    assert cash_blocked["skipped_cash_floor"] >= 1
    assert cash_blocked["skipped_no_slot"] == 0
    assert (cash_blocked.get("capacity_rejections") or []) == []


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name}: {e}")
    print("\nALLE TESTS BESTANDEN" if not fails else f"\n{fails} FEHLER")
    raise SystemExit(1 if fails else 0)
