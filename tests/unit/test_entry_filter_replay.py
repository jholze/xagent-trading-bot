"""Offline pins for the entry-filter replay. No network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.entry_filter_replay import (
    BAR_1D,
    BAR_1H,
    DISTANCE_X_PCT,
    HOLDOUT_ENTRIES,
    SMA_PRIMARY,
    TRIAL_COUNT_N,
    CandleStore,
    RawEntry,
    TrialSpec,
    accepts,
    build_feature_row,
    deflated_sharpe,
    ensure_candles,
    entry_universe,
    evaluate_population,
    is_small_cap,
    last_closed_index,
    listing_allows,
    load_decisions,
    load_fills,
    net_return,
    parse_binance_kline,
    parse_gate_candle,
    rank_key,
    round_trip_cost_fraction,
    run_report,
    trial_specs,
    volume_medians,
)
from tools.hypothesis_protocol import validate_payload

UTC = timezone.utc
T0 = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp())


def _daily(closes: list[float], *, low_scale: float = 1.0) -> list[tuple]:
    candles = []
    for index, close in enumerate(closes):
        low = close * low_scale
        candles.append((T0 + index * BAR_1D, close, close, low, close, 1000.0))
    return candles


def _hourly_from(start_hour: int, n: int, *, price: float = 100.0, low: float | None = None) -> list[tuple]:
    candles = []
    for step in range(n):
        bar_low = price if low is None else low
        candles.append((start_hour * BAR_1H + step * BAR_1H, price, price, bar_low, price, 10.0))
    return candles


def _raw(symbol: str = "AAA/USDT", *, hour_offset: int = 0) -> RawEntry:
    signal = datetime.fromtimestamp(T0 + 60 * BAR_1D + 12 * BAR_1H + hour_offset, UTC)
    return RawEntry(
        entry_id="e1",
        symbol=symbol,
        signal_ts=signal,
        day_berlin="2026-05-31",
        source="entry_sensor_15m",
        status="filled",
        code="FILLED",
        tenant="henry",
        dataset="fills",
        signal_price=100.0,
        usdt=100.0,
        csv_fwd24=None,
        csv_fwd72=None,
        is_add=False,
    )


def test_trial_registry_is_frozen_at_31():
    specs = trial_specs()
    assert len(specs) == TRIAL_COUNT_N == 31
    assert len({spec.trial_id for spec in specs}) == 31
    ranked = [spec.trial_id for spec in specs if spec.rank]
    assert ranked == [
        "trend-sma50",
        "distance-x20-y20",
        "btc-sma50",
        "trend50-and-btc50",
        "trend50-and-distance20",
        "smallcap-top100",
        "listing-age-30",
        "listing-age-90",
    ]
    assert SMA_PRIMARY == 50
    assert DISTANCE_X_PCT == 20.0
    assert HOLDOUT_ENTRIES == 0


def test_gate_and_binance_parsers():
    gate = parse_gate_candle(
        ["1793318400", "1400.5", "77311.7", "78801.7", "76282.7", "77981.8", "18.1", "true"]
    )
    assert gate == (1793318400, 77981.8, 78801.7, 76282.7, 77311.7, 1400.5)
    assert parse_gate_candle(["bad"]) is None
    binance = parse_binance_kline(
        [1793318400000, "100", "110", "90", "105", "1", 0, "999.5"]
    )
    assert binance == (1793318400, 100.0, 110.0, 90.0, 105.0, 999.5)


def test_last_closed_index_skips_the_open_bar():
    candles = _daily([100.0] * 3)
    # Noon on day index 2: day 2 is still open, day 1 is the last close.
    signal = T0 + 2 * BAR_1D + 12 * BAR_1H
    assert last_closed_index(candles, signal, BAR_1D) == 1
    # Exactly at day 2's open, that bar is not strictly closed.
    assert last_closed_index(candles, T0 + 2 * BAR_1D, BAR_1D) == 1


def test_sma_uses_last_closed_daily_bar_not_the_open_one():
    closes = [100.0] * 60 + [1000.0]
    daily = _daily(closes)
    signal = T0 + 60 * BAR_1D + 12 * BAR_1H
    fill_hour = signal // BAR_1H + 1
    hourly = _hourly_from(fill_hour - 30, 220, price=100.0, low=95.0)
    row = build_feature_row(_raw(), hourly, daily, daily, volume_median=1.0)
    # Day 60's close of 1000 is still open at noon and must not enter the SMA.
    assert row["sma_above_50"] is False
    assert row["extension_pct"] == pytest.approx(0.0)
    assert row["gross_72_d1"] == pytest.approx(0.0)
    assert row["hit_m20_72_d1"] is False


def test_trend_keep_and_one_bar_delay_and_drawdown():
    closes = [100.0] * 59 + [130.0]
    daily = _daily(closes)
    signal = T0 + 60 * BAR_1D + 100  # 100 seconds into day 60, hour 0 of that day
    raw = _raw()
    raw = RawEntry(**{**raw.__dict__, "signal_ts": datetime.fromtimestamp(signal, UTC)})
    fill_hour = signal // BAR_1H + 1
    hourly = []
    for step in range(0, 220):
        open_ts = (fill_hour - 5 + step) * BAR_1H
        # Entry bar (step == 5) low is 70, so the hold trades through -20%.
        low = 70.0 if open_ts == fill_hour * BAR_1H else 120.0
        opened = 100.0 if open_ts != (fill_hour + 72) * BAR_1H else 110.0
        if open_ts == fill_hour * BAR_1H:
            opened = 100.0
        hourly.append((open_ts, opened, opened, low, opened, 10.0))
    hourly.sort()
    row = build_feature_row(raw, hourly, daily, daily, volume_median=5.0)
    assert row["sma_above_50"] is True
    assert row["gross_72_d1"] == pytest.approx(0.10)
    assert row["hit_m20_72_d1"] is True
    # Delay 2 fills one hour later, whose open is 100 and whose +72h open is 100
    # in this fixture (only the delay-1 exit bar was set to 110).
    assert row["gross_72_d2"] == pytest.approx(0.0)


def test_exact_hour_boundary_does_not_fill_the_same_bar():
    signal = T0 + 10 * BAR_1H
    hourly = []
    for delta in range(0, 80):
        opened = 50.0
        if delta in (1, 73):
            opened = 80.0
        if delta == 72:
            opened = 90.0
        stamp = signal + delta * BAR_1H
        hourly.append((stamp, opened, opened, opened, opened, 1.0))
    daily = _daily([100.0] * 80)
    raw = RawEntry(**{**_raw().__dict__, "signal_ts": datetime.fromtimestamp(signal, UTC)})
    row = build_feature_row(raw, hourly, daily, daily, None)
    # Delay-1 entry and its 72h exit are both 80 (gross 0). The same bar is 50
    # and the bar 72h after that same bar is 90 (gross +80%). Gross 0 means the
    # fill waited for the next hour.
    assert row["gross_72_d1"] == pytest.approx(0.0)


def test_costs_match_the_protocol_schedule():
    assert round_trip_cost_fraction("other", "base", small_cap=False) == pytest.approx(0.008)
    assert round_trip_cost_fraction("stable", "base", small_cap=False) == pytest.approx(0.006)
    assert round_trip_cost_fraction("other", "stress", small_cap=False) == pytest.approx(0.010)
    assert round_trip_cost_fraction("volatile", "hard_stress", small_cap=True) == pytest.approx(0.014)
    assert net_return(0.0, "other", "base", small_cap=False) == pytest.approx(-0.008)


def test_distance_filter_blocks_extension_and_pump():
    spec = next(spec for spec in trial_specs() if spec.trial_id == "distance-x20-y20")
    base = {"extension_pct": 10.0, "pump_24h_pct": 10.0, "snapshot_day": "2026-08-01", "symbol": "AAA/USDT"}
    assert accepts(spec, base, {}) is True
    assert accepts(spec, {**base, "extension_pct": 20.0}, {}) is True
    assert accepts(spec, {**base, "extension_pct": 20.01}, {}) is False
    assert accepts(spec, {**base, "pump_24h_pct": 25.0}, {}) is False
    assert accepts(spec, {**base, "pump_24h_pct": None}, {}) is None


def test_listing_censor_does_not_drop_an_unknown_old_coin():
    assert listing_allows(119.0, True, 112.5) is True
    assert listing_allows(10.0, True, 30.0) is None
    assert listing_allows(10.0, False, 30.0) is False
    assert listing_allows(None, False, 30.0) is None


def test_missing_volume_is_small_cap_and_rank_is_point_in_time():
    table = {
        "2026-08-01": {"AAA/USDT": 50.0, "BBB/USDT": 40.0, "CCC/USDT": 10.0},
        "2026-08-02": {"AAA/USDT": 1.0, "BBB/USDT": 100.0},
    }
    assert is_small_cap("DDD/USDT", "2026-08-01", table, 2) is True
    assert is_small_cap("AAA/USDT", "2026-08-01", table, 2) is False
    assert is_small_cap("BBB/USDT", "2026-08-01", table, 2) is False
    assert is_small_cap("CCC/USDT", "2026-08-01", table, 2) is True
    # The next day's volumes do not change the prior snapshot.
    assert is_small_cap("AAA/USDT", "2026-08-01", table, 1) is False


def test_volume_median_uses_only_prior_days():
    start = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())
    candles = []
    for day in range(40):
        # One hour per day, quote volume = day index. Day 0 volume 0 is still a bar.
        candles.append((start + day * BAR_1D, 1.0, 1.0, 1.0, 1.0, float(day + 1)))
    table = volume_medians({"AAA/USDT": candles})
    snap = datetime.fromtimestamp(start + 30 * BAR_1D, UTC).date().isoformat()
    # Snapshot on day 30 uses days 0..29 (volumes 1..30). Median of 1..30 = 15.5.
    assert table[snap]["AAA/USDT"] == pytest.approx(15.5)


def test_dedupe_prefers_fill_and_drops_adds_and_same_hour():
    def _decision(status: str, code: str, source: str = "entry_sensor_15m") -> RawEntry:
        return RawEntry(
            entry_id=code,
            symbol="AAA/USDT",
            signal_ts=datetime(2026, 10, 3, tzinfo=UTC),
            day_berlin="2026-10-03",
            source=source,
            status=status,
            code=code,
            tenant="henry",
            dataset="decisions",
            signal_price=1.0,
            usdt=None,
            csv_fwd24=None,
            csv_fwd72=None,
            is_add=source == "dca",
        )

    later = datetime(2026, 8, 1, 0, 40, tzinfo=UTC)
    earlier = datetime(2026, 8, 1, 0, 10, tzinfo=UTC)
    other_hour = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    fills = [
        RawEntry(
            "b",
            "AAA/USDT",
            later,
            "2026-08-01",
            "grid",
            "filled",
            "FILLED",
            "default",
            "fills",
            1.0,
            10.0,
            None,
            None,
            False,
        ),
        RawEntry(
            "a",
            "AAA/USDT",
            earlier,
            "2026-08-01",
            "grid",
            "filled",
            "FILLED",
            "henry",
            "fills",
            1.0,
            10.0,
            None,
            None,
            False,
        ),
        RawEntry(
            "c",
            "AAA/USDT",
            other_hour,
            "2026-08-01",
            "grid",
            "filled",
            "FILLED",
            "henry",
            "fills",
            1.0,
            10.0,
            None,
            None,
            False,
        ),
        RawEntry(
            "add",
            "AAA/USDT",
            earlier,
            "2026-08-01",
            "dca",
            "filled",
            "FILLED",
            "henry",
            "fills",
            1.0,
            10.0,
            None,
            None,
            True,
        ),
    ]
    universe = entry_universe(
        [
            _decision("rejected", "cash_floor"),
            _decision("filled", "FILLED"),
            _decision("rejected", "dca_guard_below_avg", source="dca"),
            *fills,
        ]
    )
    decisions = [row for row in universe if row.dataset == "decisions"]
    assert len(decisions) == 1
    assert decisions[0].status == "filled"
    kept_ids = sorted(row.entry_id for row in universe if row.dataset == "fills")
    assert kept_ids == ["a", "c"]


def test_loaders_read_fixture_rows(tmp_path: Path):
    decisions = tmp_path / "decisions.csv"
    decisions.write_text(
        "tenant,day,symbol,source,status,code,price,fwd24h_pct,fwd72h_pct\n"
        "henry,2026-10-03,crv/USDT,entry_sensor_15m,rejected,cash_floor,0.3,1.5,-2.0\n"
        "henry,2026-10-03,CRV/USDT,dca,filled,FILLED,0.2,,\n",
        encoding="utf-8",
    )
    fills = tmp_path / "fills.jsonl"
    fills.write_text(
        json.dumps(
            {
                "id": "1",
                "side": "buy",
                "status": "filled",
                "symbol": "CRV/USDT",
                "source": "grid",
                "signal": "BUY",
                "tenant_id": "henry",
                "timestamps": {"created": "2026-08-01T10:15:00"},
                "execution": {"price": 0.5, "usdt": 25.0},
            }
        )
        + "\n"
        + json.dumps({"id": "2", "side": "sell", "status": "filled", "symbol": "CRV/USDT"})
        + "\n",
        encoding="utf-8",
    )
    loaded = load_decisions(decisions)
    assert loaded[0].symbol == "CRV/USDT"
    assert loaded[0].csv_fwd72 == pytest.approx(-2.0)
    assert loaded[1].is_add is True
    buy = load_fills(fills)
    assert len(buy) == 1
    assert buy[0].usdt == pytest.approx(25.0)
    assert buy[0].signal_ts.tzinfo is not None
    # 10:15 Berlin in August is 08:15 UTC.
    assert buy[0].signal_ts.hour == 8


def test_cache_skips_completed_ranges_without_a_client(tmp_path: Path):
    store = CandleStore(tmp_path / "candles.sqlite")
    start = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 11, 8, tzinfo=UTC).timestamp())
    store.save("AAA_USDT", "1d", start, end, _daily([1.0, 2.0]))
    hourly_start = int(datetime(2026, 6, 20, tzinfo=UTC).timestamp())
    store.save("AAA_USDT", "1h", hourly_start, end, [])

    def _boom(*_args, **_kwargs):
        raise AssertionError("fetcher should not run when the range is cached")

    ensure_candles(store, ["AAA_USDT"], network=True, client=_boom)  # type: ignore[arg-type]
    assert len(store.load("AAA_USDT", "1d")) == 2
    with pytest.raises(RuntimeError, match="not cached"):
        ensure_candles(store, ["BBB_USDT"], network=False)
    store.close()


def _feature(
    *,
    day: str,
    gross72: float,
    above50: bool,
    above38: bool | None = None,
    above63: bool | None = None,
    btc50: bool = True,
    extension: float = 0.0,
    pump: float = 0.0,
    symbol: str = "AAA/USDT",
    source: str = "grid",
    hit: bool = False,
    snapshot: str | None = None,
) -> dict:
    return {
        "entry_id": f"{symbol}-{day}-{gross72}",
        "symbol": symbol,
        "day": day,
        "snapshot_day": snapshot or day,
        "source": source,
        "status": "filled",
        "code": "FILLED",
        "tenant": "henry",
        "dataset": "fills",
        "usdt": 100.0,
        "asset_class": "other",
        "window": "W1" if day <= "2026-08-26" else "W2" if day <= "2026-09-23" else "W3",
        "half": "A" if day <= "2026-09-08" else "B",
        "in_protocol": True,
        "in_owner": day >= "2026-10-03",
        "sma_above_38": above50 if above38 is None else above38,
        "sma_above_50": above50,
        "sma_above_63": above50 if above63 is None else above63,
        "extension_pct": extension,
        "pump_24h_pct": pump,
        "btc_above_38": btc50,
        "btc_above_50": btc50,
        "btc_above_63": btc50,
        "volume_median": 10.0,
        "listing_age_days": 200.0,
        "listing_censored": True,
        "rv_daily": 0.02,
        "gross_24_d1": gross72 / 2,
        "gross_72_d1": gross72,
        "hit_m20_72_d1": hit,
        "hit_m20_7d_d1": hit,
        "gross_24_d2": gross72 / 2,
        "gross_72_d2": gross72,
        "hit_m20_72_d2": hit,
    }


def test_walk_forward_and_protocol_json_cannot_pass_without_holdout():
    rows = []
    days = {
        "W1": "2026-08-01",
        "W2": "2026-09-01",
        "W3": "2026-10-01",
    }
    for _window, day in days.items():
        for index in range(40):
            good = index % 2 == 0
            rows.append(
                _feature(
                    day=day,
                    gross72=0.03 if good else -0.08,
                    above50=good,
                    above38=True,
                    symbol=f"S{index}/USDT",
                    hit=not good,
                )
            )
    volume = {day: {f"S{index}/USDT": float(100 - index) for index in range(40)} for day in days.values()}
    # Give the kept names the highest volume so the small-cap filter keeps the winners.
    results = evaluate_population(trial_specs(), rows, volume, with_protocol=True)
    trend = next(row for row in results if row["trial_id"] == "trend-sma50")
    assert trend["protocol_inputs"]["walk_forward_wins"] == 3
    assert trend["metrics"]["kept"]["n"] == 60
    assert trend["metrics"]["kept"]["hit_m20_share"] == pytest.approx(0.0)
    assert trend["metrics"]["b0"]["hit_m20_share"] == pytest.approx(50.0)
    assert trend["metrics"]["kept"]["median_72"] > trend["metrics"]["b0"]["median_72"]
    # SMA 38 keeps every row, so its edge is 0 while SMA 50's edge is positive: a cliff.
    assert trend["protocol_inputs"]["parameter_cliff"] is True
    verdict = validate_payload(trend["payload"])
    assert verdict.status == trend["protocol_status"]
    assert verdict.status != "PASS"
    assert any(reason.startswith("holdout_entries") for reason in verdict.reasons)
    assert trend["payload"]["funding"] == "not_applicable"
    assert trend["payload"]["one_bar_delay"] is True
    assert trend["payload"]["same_bar_fill"] is False
    assert trend["payload"]["trial_count_n"] == 31


def test_rank_puts_sub_100_behind_and_breaks_ties_by_registry_order():
    def _result(trial_id: str, n_kept: int, wins: int, reduction: float) -> dict:
        return {
            "trial_id": trial_id,
            "metrics": {
                "kept": {"n": n_kept, "hit_m20_share": 10.0, "median_72": reduction, "mean_72": reduction},
                "b0": {"hit_m20_share": 10.0 + reduction, "median_72": 0.0, "mean_72": 0.0},
            },
            "protocol_inputs": {"walk_forward_wins": wins},
        }

    low_n = _result("listing-age-30", 10, 3, 5.0)
    high_n = _result("trend-sma50", 100, 2, 0.1)
    assert rank_key(high_n) > rank_key(low_n)


def test_deflated_sharpe_falls_as_trial_count_rises():
    returns = [0.02, -0.01, 0.015, 0.01, -0.005, 0.012, 0.008, -0.002] * 8
    low_n = deflated_sharpe(returns, 1)
    high_n = deflated_sharpe(returns, 31)
    assert 0.0 < high_n < low_n
    assert deflated_sharpe([-0.02, -0.01, -0.03, 0.005], 1) < 0.5
    assert deflated_sharpe([0.01], 5) == 0.0


def test_flat_cost_does_not_change_mean_edge():
    rows = [
        _feature(day="2026-08-01", gross72=0.02, above50=True, symbol="A/USDT"),
        _feature(day="2026-08-01", gross72=-0.04, above50=False, symbol="B/USDT"),
    ]
    volume = {"2026-08-01": {"A/USDT": 10.0, "B/USDT": 1.0}}
    spec = TrialSpec("trend-sma50", "trend", "trend", True, sma=50, cost_tier="base")
    stressed = TrialSpec("stress-trend-sma50", "trend", "stress-trend", False, sma=50, cost_tier="stress")
    base = evaluate_population([spec], rows, volume, with_protocol=True)[0]
    stress = evaluate_population([stressed], rows, volume, with_protocol=True)[0]
    assert base["protocol_inputs"]["edge_mean_72"] == pytest.approx(
        stress["protocol_inputs"]["edge_mean_72"]
    )
    # Absolute mean still pays the higher stress cost.
    assert stress["metrics"]["kept"]["mean_72"] < base["metrics"]["kept"]["mean_72"]


def test_committed_hypothesis_payload_is_a_kill_and_not_in_evidence():
    """The replay payload stays out of docs/evidence because a non-PASS JSON fails CI."""
    payload = json.loads(
        Path("docs/investigations/2026-11-01-entry-quality-replay.json").read_text(encoding="utf-8")
    )
    result = validate_payload(payload)
    assert result.status == "KILL"
    assert result.status != "PASS"
    assert payload["trial_id"] == "trend50-and-distance20"
    assert payload["holdout_entries"] == 0
    assert payload["trial_count_n"] == TRIAL_COUNT_N
    assert list(Path("docs/evidence").glob("*.json")) == []


def test_run_report_is_json_and_names_a_non_pass(tmp_path: Path):
    rows = []
    for index, day in enumerate(("2026-08-10", "2026-09-10", "2026-10-10")):
        rows.append(
            _feature(
                day=day,
                gross72=-0.05,
                above50=index == 0,
                symbol="ZZ/USDT",
                hit=True,
            )
        )
    volume = {day: {"ZZ/USDT": 1.0} for day in ("2026-08-10", "2026-09-10", "2026-10-10")}
    report = run_report(rows, volume)
    json.dumps(report)
    assert report["registered"]["trial_count_n"] == 31
    assert "Do not change" in report["recommendation"]
    top = report["ranking"][0]
    match = next(row for row in report["protocol_fills"] if row["trial_id"] == top)
    assert match["protocol_status"] != "PASS"
    assert "protocol_fills" in report["tables"]
