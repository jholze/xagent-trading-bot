"""Hard liquidity lock (#641). Repairs the #563 venue gate.

2Z is blocked by the Gate 24h volume floor (301,891 < 500,000), not by
the ±0.5% book band. The band recount of the historical book is not this
test. Replay times are Europe/Berlin; decision ids use true UTC. The
orders store is not read (it has no 2Z rows).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ["PYTEST_DB_SUFFIX"] = "sLiqGuard"

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.venue_quality import (
    VenueMetrics,
    check_venue_for_buy,
    depth_within_mid_band,
    liquidity_guard_config,
)
from tests.unit.test_long_mcap_venue_563 import _eval_env

_ZZ_VOLUME = 301_891.0


def _cfg(**risk):
    raw_risk = {
        "daily_loss_limit_pct": 100,
        "min_trade_usdt": 5,
        "cash_floor_pct": 0,
        "max_daily_sells": 0,
        "liquidity_guard": {
            "min_quote_volume_24h_usdt": 500000,
            "depth_window_pct": 0.5,
        },
    }
    raw_risk.update(risk)
    return BotConfig(
        {
            "max_usdt_per_trade": 5000,
            "max_open_positions": 100,
            "trade_cooldown_hours": 0,
            "max_position_percent": 100,
            "max_daily_trades": 0,
            "risk": raw_risk,
            "trading": {"mode": "paper", "initial_capital": 10000},
            "live": {"execution": "shadow", "dry_run": True},
        }
    )


def _book(**kw) -> VenueMetrics:
    base = dict(
        symbol="2Z/USDT",
        quote_volume_24h_usdt=2_000_000.0,
        last=1.0,
        bid=0.999,
        ask=1.001,
        bid_size=10_000.0,
        ask_size=10_000.0,
        spread_pct=0.2,
        top_book_bid_usdt=10_000.0,
        top_book_ask_usdt=10_000.0,
        capture="ok",
        depth_bid_usdt=10_000.0,
        depth_ask_usdt=10_000.0,
        depth_parsed=True,
        quote_volume_present=True,
    )
    base.update(kw)
    return VenueMetrics(**base)


def _zz_book() -> VenueMetrics:
    """Recorded Gate volume. Band depth is above the order on purpose."""
    return _book(
        quote_volume_24h_usdt=_ZZ_VOLUME,
        last=0.068056,
        depth_bid_usdt=50_000.0,
        depth_ask_usdt=50_000.0,
        top_book_bid_usdt=50_000.0,
        top_book_ask_usdt=50_000.0,
    )


def _codes(dec) -> list[str]:
    details = dec.details or {}
    return list(details.get("codes") or ([dec.code] if dec.code else []))


def _eval(rm, order, source, metrics, position=None, indicators=None):
    with _eval_env(rm, position=position, metrics=metrics, mcap=50_000_000):
        return rm.evaluate(order, "15m", source=source, indicators=indicators)


def test_floor_below_500k_is_ignored():
    cfg = liquidity_guard_config(
        {"risk": {"liquidity_guard": {"min_quote_volume_24h_usdt": 50_000, "depth_window_pct": 0.5}}}
    )
    assert cfg["min_quote_volume_24h_usdt"] == 500_000.0
    assert cfg["depth_window_pct"] == pytest.approx(0.5)


def test_window_default_is_half_a_percent():
    cfg = liquidity_guard_config({"risk": {}})
    assert cfg["depth_window_pct"] == pytest.approx(0.5)
    assert cfg["min_quote_volume_24h_usdt"] == 500_000.0


def test_band_not_level_count_aave_style_book_passes_a_1000_usdt_order():
    """Top five asks sum to 826, which is under a 1000 USDT order.
    Levels still inside ±0.5% of mid bring the ask side to ~243000.
    A level count would block. The band does not.
    """
    # mid = (99.95 + 100.05) / 2 = 100. ±0.5% is 99.50 .. 100.50.
    asks = [
        ["100.05", "1.0"],
        ["100.10", "1.2"],
        ["100.15", "1.5"],
        ["100.20", "2.0"],
        ["100.25", "2.5"],
        ["100.40", "2420"],
    ]
    bids = [
        ["99.95", "1.0"],
        ["99.90", "1.2"],
        ["99.85", "1.5"],
        ["99.80", "2.0"],
        ["99.75", "2.5"],
        ["99.60", "2500"],
    ]
    bid_d, ask_d, parsed = depth_within_mid_band(
        {"bids": bids, "asks": asks}, window_pct=0.5
    )
    assert parsed is True
    top5_ask = sum(float(px) * float(sz) for px, sz in asks[:5])
    assert top5_ask < 1000
    assert ask_d > 200_000
    assert bid_d > 200_000
    metrics = _book(
        symbol="AAVE/USDT",
        depth_bid_usdt=bid_d,
        depth_ask_usdt=ask_d,
        quote_volume_24h_usdt=5_000_000,
    )
    result = check_venue_for_buy(
        "AAVE/USDT",
        source="entry_sensor_15m",
        planned_usdt=1000,
        config_raw=_cfg().raw,
        metrics=metrics,
    )
    assert result.ok is True, result.reasons
    assert "liq_guard_depth_lt_order" not in (getattr(result, "guard_codes", None) or [])


def test_either_side_thinner_than_final_order_blocks():
    rm = RiskManager(_cfg())
    metrics = _book(depth_ask_usdt=5_000, depth_bid_usdt=500, symbol="LAB/USDT")
    lot = {"amount": 10.0, "average_entry": 1.0, "dca_rounds": 0}
    dec = _eval(
        rm,
        TradeOrder(
            "BUY", "LAB/USDT", 1.05, 0, usdt_amount=800, signal="BUY_DCA", source="dca"
        ),
        "dca",
        metrics,
        position=lot,
    )
    assert dec.approved is False
    assert dec.code == "liq_guard_depth_lt_order"
    assert dec.details["depth_bid_usdt"] == pytest.approx(500)
    assert dec.details["planned_usdt"] == pytest.approx(800)


def test_final_size_after_boost_is_what_the_book_is_compared_to():
    """Depth covers the pre-boost 800 but not 800 * 1.35."""
    rm = RiskManager(_cfg())
    metrics = _book(depth_ask_usdt=1000, depth_bid_usdt=1000, symbol="LAB/USDT")
    lot = {"amount": 10.0, "average_entry": 1.0, "dca_rounds": 0}
    order = TradeOrder(
        "BUY", "LAB/USDT", 1.05, 0, usdt_amount=800, signal="BUY_DCA", source="dca"
    )
    with patch("risk.moderate_deploy.size_boost_for_regime", return_value=1.35):
        dec = _eval(rm, order, "dca", metrics, position=lot)
    assert dec.approved is False
    assert dec.code == "liq_guard_depth_lt_order"
    assert dec.details["planned_usdt"] == pytest.approx(1080)


def test_replay_2z_rows_blocked_by_volume_not_by_the_book():
    """Entry Berlin 2026-09-27 21:36:37 (UTC 19:36:37) and both adds.

    decision ids:
      default:2Z/USDT:entry_sensor_15m:2026-09-27T19:36:37Z
      default:2Z/USDT:BUY_DCA:2026-09-30T17:09:43Z
      default:2Z/USDT:BUY_DCA:2026-10-01T11:15:13Z
    """
    rm = RiskManager(_cfg())
    book = _zz_book()
    rows = [
        {
            "id": "default:2Z/USDT:entry_sensor_15m:2026-09-27T19:36:37Z",
            "source": "entry_sensor_15m",
            "signal": "BUY",
            "price": 0.068056,
            "usdt": 1000,
            "position": None,
        },
        {
            "id": "default:2Z/USDT:BUY_DCA:2026-09-30T17:09:43Z",
            "source": "dca",
            "signal": "BUY_DCA",
            "price": 0.062835,
            "usdt": 400,
            "position": {"amount": 14000.0, "average_entry": 0.068056, "dca_rounds": 0},
        },
        {
            "id": "default:2Z/USDT:BUY_DCA:2026-10-01T11:15:13Z",
            "source": "dca",
            "signal": "BUY_DCA",
            "price": 0.05796,
            "usdt": 450,
            "position": {"amount": 20000.0, "average_entry": 0.0665, "dca_rounds": 1},
        },
    ]
    for row in rows:
        order = TradeOrder(
            "BUY",
            "2Z/USDT",
            row["price"],
            0,
            usdt_amount=row["usdt"],
            signal=row["signal"],
            source=row["source"],
        )
        order.idempotency_key = row["id"]
        dec = _eval(rm, order, row["source"], book, position=row["position"])
        assert dec.approved is False, row["id"]
        assert "liq_guard_volume_low" in _codes(dec), row["id"]
        assert "liq_guard_depth_lt_order" not in _codes(dec), row["id"]
        if row["position"] is None:
            assert dec.code == "liq_guard_volume_low"
        assert dec.details["quote_volume_24h_usdt"] == pytest.approx(_ZZ_VOLUME)
        assert dec.details["price"] == pytest.approx(row["price"])


def test_missing_gate_data_blocks_the_buy_and_the_loop_continues():
    rm = RiskManager(_cfg())
    missing = _book(capture="missing", quote_volume_present=False, depth_parsed=False)
    buy = TradeOrder("BUY", "L3/USDT", 1.0, 0, usdt_amount=200, signal="BUY", source="cmc")
    dec = _eval(rm, buy, "cmc", missing)
    assert dec.approved is False
    assert "liq_guard_missing_input" in _codes(dec)
    sell = TradeOrder(
        "SELL", "L3/USDT", 1.0, 1.0, signal="SELL_FULL", source="exit_ws"
    )
    sold = _eval(
        rm,
        sell,
        "exit_ws",
        missing,
        position={"amount": 5.0, "average_entry": 1.0, "dca_rounds": 0},
    )
    assert sold.approved is True, f"{sold.code}: {sold.message}"


def test_fetch_error_does_not_raise():
    rm = RiskManager(_cfg())
    order = TradeOrder("BUY", "L3/USDT", 1.0, 0, usdt_amount=200, signal="BUY", source="cmc")
    with _eval_env(
        rm,
        mcap=50_000_000,
        metrics_side_effect=RuntimeError("gate down"),
    ):
        dec = rm.evaluate(order, "15m", source="cmc")
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"


@pytest.mark.parametrize("signal", ["SELL_FULL", "SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL"])
def test_sells_stops_and_exits_are_never_blocked(signal):
    rm = RiskManager(_cfg())
    thin = _book(
        quote_volume_24h_usdt=1_000,
        depth_bid_usdt=1,
        depth_ask_usdt=1,
        depth_parsed=True,
        capture="missing",
        quote_volume_present=False,
    )
    order = TradeOrder(
        "SELL", "2Z/USDT", 1.0, 100, signal=signal, source="exit_ws"
    )
    dec = _eval(
        rm,
        order,
        "exit_ws",
        thin,
        position={"amount": 100.0, "average_entry": 1.0, "dca_rounds": 2},
    )
    assert dec.approved is True, f"{signal} {dec.code}: {dec.message}"
    assert not str(dec.code).startswith("liq_guard_")


def test_manual_buy_is_exempt_from_the_liquidity_lock():
    rm = RiskManager(_cfg())
    thin = _book(quote_volume_24h_usdt=1_000, depth_bid_usdt=1, depth_ask_usdt=1)
    dec = _eval(
        rm,
        TradeOrder("BUY", "L3/USDT", 1.0, 0, usdt_amount=500, signal="BUY", source="manual"),
        "manual",
        thin,
    )
    assert dec.approved is True, f"{dec.code}: {dec.message}"
