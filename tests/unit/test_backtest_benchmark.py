"""Buy & Hold benchmark, monthly returns and entry timing (backtest/harness/benchmark.py).

Pure tests: synthetic candles and runs, no Mongo, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backtest.harness import benchmark as bm

UTC = timezone.utc
H = 3600
DAY = 86400
T0 = datetime(2026, 9, 28, tzinfo=UTC)  # Monday


def bars(start: datetime, hours: int, price_at):
    s = int(start.timestamp())
    out = []
    for k in range(hours):
        o = price_at(k)
        c = price_at(k + 1)
        out.append((s + k * H, o, max(o, c), min(o, c), c, 1.0))
    return out


def lot(sym, entry, pnl, ret, invested=100.0):
    return SimpleNamespace(key=("t", sym, "1h"), entry_ts=entry, pnl=pnl, ret=ret, invested=invested)


def run(lots, pnl_curve, capital_curve):
    return SimpleNamespace(lots=lots, pnl_curve=pnl_curve, capital_curve=capital_curve)


def test_equal_weight_split_without_costs():
    n = 72
    data = {"AAA/USDT": bars(T0, n, lambda k: 100.0 + 100.0 * k / n), "BBB/USDT": bars(T0, n, lambda k: 50.0)}
    pts = bm.sample_points(int(T0.timestamp()), int(T0.timestamp()) + n * H)
    res = bm.buy_and_hold(["AAA/USDT", "BBB/USDT"], data.get, pts, 1000.0)
    assert res["symbols"] == ["AAA/USDT", "BBB/USDT"]
    assert res["equity"][0] == pytest.approx(1000.0)
    assert res["equity"][-1] == pytest.approx(500.0 * 2 + 500.0)  # AAA doubles, BBB flat
    assert res["per_symbol"]["AAA/USDT"]["return_pct"] == pytest.approx(100.0)
    assert res["per_symbol"]["BBB/USDT"]["return_pct"] == pytest.approx(0.0)


def test_costs_always_lower_the_benchmark():
    n = 48
    data = {"AAA/USDT": bars(T0, n, lambda k: 10.0 + k * 0.1)}
    pts = bm.sample_points(int(T0.timestamp()), int(T0.timestamp()) + n * H)
    free = bm.buy_and_hold(["AAA/USDT"], data.get, pts, 1000.0)
    paid = bm.buy_and_hold(["AAA/USDT"], data.get, pts, 1000.0, fee_pct=0.2, slippage_pct=lambda s: 0.5)
    assert all(p < f for p, f in zip(paid["equity"], free["equity"]))
    assert paid["equity"][0] < 1000.0  # entry cost and liquidation cost show at once


def test_symbol_without_price_at_start_is_excluded_and_listed():
    late = T0 + timedelta(days=3)
    data = {"AAA/USDT": bars(T0, 120, lambda k: 1.0), "NEW/USDT": bars(late, 24, lambda k: 1.0)}
    pts = bm.sample_points(int(T0.timestamp()), int(T0.timestamp()) + 120 * H)
    res = bm.buy_and_hold(["AAA/USDT", "NEW/USDT", "GONE/USDT"], lambda s: data.get(s, []), pts, 900.0)
    assert res["symbols"] == ["AAA/USDT"]
    assert set(res["excluded"]) == {"NEW/USDT", "GONE/USDT"}
    assert res["equity"][-1] == pytest.approx(900.0)  # whole capital in the usable leg


def test_no_usable_symbol_gives_no_equity():
    pts = bm.sample_points(int(T0.timestamp()), int(T0.timestamp()) + 5 * DAY)
    res = bm.buy_and_hold(["X/USDT"], lambda s: [], pts, 100.0)
    assert res["equity"] is None and "X/USDT" in res["excluded"]


def test_strategy_equity_and_peak_deployed():
    s = int(T0.timestamp())
    cap = {s: 500.0, s + 5 * H: 300.0, s + DAY + H: -800.0}
    assert bm.peak_deployed(cap) == 800.0
    pnl = {s + 2 * H: 10.0, s + DAY + 2 * H: -30.0, s + 2 * DAY + 5: 5.0}
    pts = bm.sample_points(s, s + 2 * DAY)
    eq = bm.strategy_equity(pnl, pts, 800.0)
    assert pts == [s, s + DAY, s + 2 * DAY]
    assert eq == [800.0, 810.0, 785.0]  # PnL booked at/after the end is in the last point


def test_max_drawdown_and_sharpe():
    assert bm.max_drawdown_pct([100, 120, 90, 130, 117]) == pytest.approx(25.0)
    assert bm.max_drawdown_pct([100, 101, 102]) == 0.0
    assert bm.sharpe([0.01, 0.01], 365) is None  # zero stdev
    assert bm.sharpe([0.01], 365) is None
    r = [0.01, -0.005, 0.02, 0.0]
    sr = bm.sharpe(r, 365)
    import math
    import statistics

    assert sr == pytest.approx(statistics.fmean(r) / statistics.stdev(r) * math.sqrt(365))


def test_monthly_returns_and_counts_across_month_ends():
    start = datetime(2026, 8, 30, tzinfo=UTC)
    end = datetime(2026, 10, 3, tzinfo=UTC)
    pts = bm.sample_points(int(start.timestamp()), int(end.timestamp()))
    # equity: 100 until Sep 1, 110 at Oct 1, 99 at end
    eq = []
    for p in pts:
        d = datetime.fromtimestamp(p, UTC)
        eq.append(100.0 if d <= datetime(2026, 9, 1, tzinfo=UTC) else (110.0 if d <= datetime(2026, 10, 1, tzinfo=UTC) else 99.0))
    months = bm.monthly_returns(pts, eq)
    assert [m["month"] for m in months] == ["2026-08", "2026-09", "2026-10"]
    assert months[0]["return_pct"] == pytest.approx(0.0) and months[0]["partial"]
    assert months[1]["return_pct"] == pytest.approx(10.0) and not months[1]["partial"]
    assert months[2]["return_pct"] == pytest.approx(-10.0) and months[2]["partial"]
    assert bm.month_counts(months) == {"positive": 1, "negative": 1, "flat": 1}


def test_entry_timing_utc_and_berlin_labels_and_dst():
    summer = datetime(2026, 10, 4, 22, 30, tzinfo=UTC)  # Sun 22:30 UTC = Mon 00:30 CEST
    winter = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # Mon 07:00 UTC = 08:00 CET
    before = datetime(2026, 9, 1, tzinfo=UTC)
    lots = [lot("A/USDT", summer, 5.0, 0.05), lot("A/USDT", winter, -2.0, -0.02), lot("B/USDT", before, 1.0, 0.01)]
    s, e = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()), int(datetime(2026, 11, 30, tzinfo=UTC).timestamp())
    et = bm.entry_timing(lots, s, e, "Europe/Berlin")
    assert et["entries"] == 2 and et["zones"] == ["UTC", "Europe/Berlin"]
    u, b = et["by_zone"]["UTC"], et["by_zone"]["Europe/Berlin"]
    assert u["hour"][22]["n"] == 1 and b["hour"][0]["n"] == 1
    assert u["hour"][7]["n"] == 1 and b["hour"][8]["n"] == 1
    assert u["weekday"]["Sun"]["n"] == 1 and b["weekday"]["Sun"]["n"] == 0
    assert b["weekday"]["Mon"]["n"] == 2 and b["weekday"]["Mon"]["wins"] == 1
    assert b["weekday"]["Mon"]["pnl_usdt"] == pytest.approx(3.0)
    assert b["weekday"]["Mon"]["avg_ret_pct"] == pytest.approx(1.5)
    assert sum(x["n"] for x in u["hour"].values()) == 2


def test_entry_timing_rejects_naive_timestamps():
    with pytest.raises(ValueError):
        bm.entry_timing([lot("A/USDT", datetime(2026, 10, 2), 1.0, 0.1)], 0, 2**40, "Europe/Berlin")


def test_baskets_are_configurable_and_traded_basket_follows_the_run():
    cfg = bm.BenchmarkConfig(symbols=("ZZZ/USDT", "YYY/USDT"))
    assert cfg.baskets(["B/USDT", "A/USDT", "A/USDT"]) == {
        "ZZZ/USDT+YYY/USDT": ["ZZZ/USDT", "YYY/USDT"],
        bm.TRADED_BASKET: ["A/USDT", "B/USDT"],
    }
    assert bm.BenchmarkConfig(symbols=(), include_traded_basket=False).baskets(["A/USDT"]) == {}
    assert bm.BenchmarkConfig().symbols == bm.DEFAULT_BENCHMARK_SYMBOLS


def _report(capital=None):
    s = int(T0.timestamp())
    n = 10 * 24
    data = {
        "REF/USDT": bars(T0, n, lambda k: 100.0 * (1 + 0.2 * k / n)),
        "A/USDT": bars(T0, n, lambda k: 10.0 * (1 - 0.3 * k / n)),
    }
    pnl = {s + d * DAY + H: (3.0 if d % 2 else -1.0) for d in range(10)}
    r = run([lot("A/USDT", T0 + timedelta(hours=3), 11.0, 0.011, invested=1000.0)], pnl, {s: 1000.0})
    cfg = bm.BenchmarkConfig(symbols=("REF/USDT",), capital=capital)
    return bm.benchmark_report(r, data.get, T0, T0 + timedelta(days=10), config=cfg)


def test_benchmark_report_compares_strategy_and_both_baskets():
    rep = _report()
    assert rep["capital_base_usdt"] == 1000.0 and rep["capital_base_rule"] == "peak deployed capital"
    st = rep["strategy"]
    assert st["pnl_usdt"] == pytest.approx(sum(3.0 if d % 2 else -1.0 for d in range(10)))  # from the PnL curve
    assert set(rep["benchmarks"]) == {"REF/USDT", bm.TRADED_BASKET}
    ref = rep["benchmarks"]["REF/USDT"]
    assert ref["metrics"]["return_pct"] == pytest.approx(20.0, abs=0.01)
    assert ref["excess_return_pp"] == pytest.approx(st["return_pct"] - ref["metrics"]["return_pct"])
    assert not ref["beats_return"]
    traded = rep["benchmarks"][bm.TRADED_BASKET]
    assert traded["symbols"] == ["A/USDT"] and traded["metrics"]["return_pct"] < 0
    assert traded["beats_return"] and traded["lower_dd"]
    assert rep["entry_timing"]["entries"] == 1
    md = "\n".join(bm.render_markdown("variant", rep))
    assert "B&H REF/USDT" in md and "Beats on Sharpe" in md and "Beats on return" in md and "Europe/Berlin" in md and "UTC" in md


def test_fixed_capital_and_empty_run():
    assert _report(capital=5000.0)["capital_base_usdt"] == 5000.0
    empty = bm.benchmark_report(run([], {}, {}), lambda s: [], T0, T0 + timedelta(days=2))
    assert empty["strategy"] is None and "note" in empty
    assert "no capital" in "\n".join(bm.render_markdown("x", empty))
