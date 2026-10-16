"""Offline checks for the #616 A/B/C walk-forward harness. No network."""

from __future__ import annotations

from datetime import timedelta

from scripts.wf_watchlist_abc import (
    FREEZE_ID,
    Bar,
    Exit,
    _utc,
    build_folds,
    fee_funding,
    fold_metrics,
    max_drawdown_usdt,
    profit_factor,
    run_measurement,
    setup_gates,
)

STOPS_A = frozenset({"trailing_stop", "liquidation"})


def _bars(n: int, price: float = 100.0, start=None) -> list[Bar]:
    start = start or _utc(2026, 9, 1)
    out = []
    for i in range(n):
        ts = int((start + timedelta(hours=i)).timestamp())
        out.append(Bar(ts=ts, open=price, high=price, low=price, close=price))
    return out


def _exit(**kwargs) -> Exit:
    base = dict(
        id="e1",
        symbol="AAA/USDT",
        ts=_utc(2026, 9, 1, 2, 1),
        exit_source="rsi_sell",
        signal="SELL_FULL",
        source="auto",
        pnl=10.0,
        usdt=1000.0,
        timeframe="1h",
        ledger="test",
    )
    base.update(kwargs)
    return Exit(**base)


def _folds():
    return {
        "folds": [
            {
                "id": "F1",
                "train_start": _utc(2026, 9, 1),
                "train_end": _utc(2026, 9, 1),
                "oos_start": _utc(2026, 9, 1),
                "oos_end": _utc(2026, 9, 20),
            }
        ],
        "window_start": _utc(2026, 9, 1),
        "window_end": _utc(2026, 9, 20),
        "wf_incomplete": True,
        "adapted": True,
        "reason": "test",
    }


def _cfg():
    return {
        "max_usdt_per_trade": 4500.0,
        "shorts": {
            "allow_live": False,
            "leverage_default": 2,
            "leverage_cap": 2,
            "liquidation_buffer": 0.05,
            "auto_notional_pct": 0.35,
            "funding_rate_8h": 0.0001,
            "volatile": {
                "time_cap_hours": 4,
                "stop_margin_pct": 0.12,
                "trail_arm_pct": 4,
                "trail_retrace_pct": 1.5,
                "rsi_cover_below": 32,
            },
        },
        "costs": {
            "fee_source": "freeze_7_5bp",
            "gate": {
                "swap": {
                    "fee_taker_pct": 0.075,
                    "fee_maker_pct": 0.075,
                    "slippage_pct": 0.0,
                }
            },
        },
    }


def test_profit_factor_and_drawdown():
    assert profit_factor([]) is None
    assert profit_factor([1.0, -0.5]) == 2.0
    assert profit_factor([1.0]) == float("inf")
    assert max_drawdown_usdt([5, -3, -4, 1]) == 7


def test_fee_funding_flat_short():
    gross, fees, funding, net = fee_funding(350.0, 100.0, 100.0, 3.5, 4.0)
    assert gross == 0
    assert abs(fees - 0.525) < 1e-9
    assert abs(funding - 0.0175) < 1e-9
    assert abs(net - (-0.5425)) < 1e-9


def test_arm_a_flat_time_cap_matches_fee_model():
    bars = _bars(30)
    measured = run_measurement(
        [_exit()],
        {"AAA/USDT": bars},
        bars,
        folds_meta=_folds(),
        regime_at=lambda _ts: None,
        mcap_of=lambda _sym: None,
        clock_end=_utc(2026, 9, 5),
        config_raw=_cfg(),
    )
    trades = [t for t in measured["trades"] if t["arm"] == "A"]
    assert len(trades) == 1
    assert trades[0]["cover_source"] == "time_cap"
    assert abs(trades[0]["pnl"] - (-0.5425)) < 1e-4
    row_c = [r for r in measured["rows"] if r["arm"] == "C"]
    assert row_c
    assert all(r["n"] == 0 and r["sum_pnl"] == 0 and r["pf"] is None for r in row_c)


def test_missing_regime_fail_closed_and_b0_skips_loss(monkeypatch):
    bars = _bars(80)
    exits = [
        _exit(id="win", pnl=5.0, ts=_utc(2026, 9, 1, 2, 1)),
        _exit(id="loss", pnl=-3.0, ts=_utc(2026, 9, 1, 3, 1), exit_source="exit_volume_climax"),
    ]
    measured = run_measurement(
        exits,
        {"AAA/USDT": bars},
        bars,
        folds_meta=_folds(),
        regime_at=lambda _ts: None,
        mcap_of=lambda _sym: None,
        clock_end=_utc(2026, 9, 6),
        config_raw=_cfg(),
    )
    assert sum(1 for w in measured["watch_b0"] if w["status"] == "expired") == 1
    assert sum(1 for w in measured["watch_b0"] if w["reason"] == "skip_loss") == 1
    assert sum(1 for w in measured["watch_b1"] if w["status"] == "expired") == 2
    assert measured["agg"]["B0"]["n"] == 0
    assert measured["agg"]["B1"]["n"] == 0
    assert measured["gate_fail_first_b0"].get("G1_regime_missing", 0) > 0
    monkeypatch  # regime stays missing; no open is the assertion


def test_setup_gates_missing_inputs_fail_and_full_pass():
    ok, reasons, _info = setup_gates(
        regime=None,
        atr=None,
        close=100,
        rsi=None,
        mcap=None,
        bounce=None,
        coin_ret=None,
        btc_ret=None,
        post_long_blocked=False,
        reentry_blocked=False,
        symbol_open=False,
        open_count=0,
        max_open=6,
        daily_pnl=0,
        has_next_bar=True,
    )
    assert ok is False
    assert "G1_regime_missing" in reasons
    ok2, reasons2, info = setup_gates(
        regime="RISK_OFF",
        atr=2.0,
        close=100,
        rsi=57,
        mcap=2_000_000_000,
        bounce=True,
        coin_ret=None,
        btc_ret=None,
        post_long_blocked=False,
        reentry_blocked=False,
        symbol_open=False,
        open_count=0,
        max_open=6,
        daily_pnl=0,
        has_next_bar=True,
    )
    assert reasons2 == []
    assert ok2 is True
    assert info["cap_class"] == "large"
    assert info["confluence"] >= 2


def test_arm_b_opens_only_when_gates_are_supplied(monkeypatch):
    import scripts.wf_watchlist_abc as wf

    monkeypatch.setattr(wf, "wilder_atr", lambda bars, period=14: [1.0] * len(bars))
    monkeypatch.setattr(wf, "wilder_rsi", lambda closes, period=14: [57.0] * len(closes))
    monkeypatch.setattr(wf, "bounce_failed", lambda bars, idx, atr: True)
    bars = _bars(80)
    measured = run_measurement(
        [_exit(usdt=1000.0, pnl=20.0)],
        {"AAA/USDT": bars},
        bars,
        folds_meta=_folds(),
        regime_at=lambda _ts: "RISK_OFF",
        mcap_of=lambda _sym: 2_000_000_000.0,
        clock_end=_utc(2026, 9, 6),
        config_raw=_cfg(),
    )
    trades = [t for t in measured["trades"] if t["arm"] == "B0"]
    assert len(trades) == 1
    assert trades[0]["cover_source"] == "watchlist_time"
    # large-cap time cap 24h, size min(500, 1575, 1500) = 500, flat price
    _g, fees, funding, net = fee_funding(500.0, 100.0, 100.0, 5.0, 24.0)
    assert abs(trades[0]["pnl"] - net) < 1e-3
    assert abs(fees - 0.75) < 1e-9
    assert abs(funding - 0.15) < 1e-9


def test_non_allowlist_does_not_open_arm_a():
    bars = _bars(20)
    measured = run_measurement(
        [_exit(exit_source="trailing_take_profit")],
        {"AAA/USDT": bars},
        bars,
        folds_meta=_folds(),
        config_raw=_cfg(),
        clock_end=_utc(2026, 9, 4),
    )
    assert [t for t in measured["trades"] if t["arm"] == "A"] == []


def test_short_window_adapts_to_three_oos_folds():
    exits = [
        _exit(id=f"e{i}", ts=_utc(2026, 9, 20 + (i % 9), 12))
        for i in range(9)
    ]
    meta = build_folds(exits, _utc(2026, 7, 1), _utc(2026, 9, 29))
    assert meta["adapted"] is True
    assert len(meta["folds"]) >= 3
    assert meta["wf_incomplete"] is False
    assert meta["canonical_oos_with_events"] == ["F4"]


def test_watchlist_cap_frees_after_ttl():
    clustered = [
        _exit(id=f"c{i}", ts=_utc(2026, 9, 1, 0, 0) + timedelta(seconds=i), pnl=1.0)
        for i in range(41)
    ]
    measured = run_measurement(
        clustered,
        {"AAA/USDT": []},
        [],
        folds_meta=_folds(),
        clock_end=_utc(2026, 9, 5),
        config_raw=_cfg(),
    )
    assert sum(1 for w in measured["watch_b1"] if w["reason"] == "max_entries") == 1
    assert sum(1 for w in measured["watch_b1"] if w["status"] == "expired") == 40

    spaced = [
        _exit(id=f"s{i}", ts=_utc(2026, 9, 1) + timedelta(hours=49 * i), pnl=1.0)
        for i in range(41)
    ]
    # Fold in the test only covers through Sep 20, so stretch the fold.
    folds = _folds()
    folds["folds"][0]["oos_end"] = _utc(2026, 12, 1)
    measured2 = run_measurement(
        spaced,
        {"AAA/USDT": []},
        [],
        folds_meta=folds,
        clock_end=_utc(2026, 12, 1),
        config_raw=_cfg(),
    )
    assert sum(1 for w in measured2["watch_b1"] if w["reason"] == "max_entries") == 0
    assert sum(1 for w in measured2["watch_b1"] if w["status"] == "expired") == 41


def test_freeze_id():
    assert FREEZE_ID == "FBR-v1.1-watchlist-616"
    empty = fold_metrics([], stop_sources=STOPS_A, expired=None)
    assert empty["n"] == 0 and empty["pf"] is None and empty["sum_pnl"] == 0
