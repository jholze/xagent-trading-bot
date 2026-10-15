"""#671: the existing liquidity lock caps buy size. It does not reject on depth.

Fees are 0 in these cases. Symbols are fixture labels. min_trade_usdt is set
in the fixture (15, unless a case says why it cannot be). Thresholds come
from the config the case builds. No coin name is introduced here; T10 reads
the existing loss-case fixture, which already names symbols.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from execution.gate_adapter import set_venue_limits_override
from risk.risk_manager import RiskManager
from tests.support.venue_block import as_venue_config, venue_block
from services.venue_quality import (
    VenueMetrics,
    check_venue_for_buy,
    is_thin_venue_stamp,
    reset_venue_cache_for_tests,
    stamp_venue_for_fill,
)
from strategies.positions import (
    VENUE_MIN_MERGE_LOSS_EXCEEDED,
    _mark_positions_unknown,
    _resolve_store_key,
    clear_positions_memory,
    find_open_positions_for_symbol,
    get_position,
    refresh_venue_min_mark,
    update_position,
)
from tests.unit.test_issue_662_small_ticket import _first_firing_stop
from tests.unit.test_long_mcap_venue_563 import _eval_env

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "loss_cases_641.csv"

@pytest.fixture(autouse=True)
def _clean():
    clear_positions_memory()
    reset_venue_cache_for_tests()
    yield
    clear_positions_memory()
    reset_venue_cache_for_tests()


def _book(
    *,
    symbol="LAB/USDT",
    bid=325.0,
    ask=1694.0,
    qv=600_000.0,
) -> VenueMetrics:
    return VenueMetrics(
        symbol=symbol,
        quote_volume_24h_usdt=qv,
        last=1.0,
        bid=0.999,
        ask=1.001,
        bid_size=10_000.0,
        ask_size=10_000.0,
        spread_pct=0.2,
        top_book_bid_usdt=bid,
        top_book_ask_usdt=ask,
        capture="ok",
        depth_bid_usdt=bid,
        depth_ask_usdt=ask,
        depth_parsed=True,
        quote_volume_present=True,
    )


def _cfg(
    *,
    min_trade=15.0,
    max_usdt=4500.0,
    depth_multiple=1,
    volume_multiple=20,
    volume_floor=500_000.0,
    venue_floor=1e-9,
    min_book=1e-9,
    extra_risk=None,
    shorts=False,
) -> BotConfig:
    risk = {
        "min_trade_usdt": min_trade,
        "cash_floor_pct": 0,
        "max_daily_loss_pct": 0,
        "max_daily_sells": 0,
        "cash_policy": {"enabled": False},
        "position_capacity": {"enabled": False},
        "moderate_deploy": {"enabled": False},
        "slot_eviction": {"enabled": False},
        "stop_gap_buffer_pct": 6.21,
        "stop_loss_pct": 20,
        "partial_stop_ratio": 0.67,
        "venue_min_max_pct_of_ticket": 0.20,
        "venue_step_max_pct_of_ticket": 0.02,
        "longs": {
            "volatile": {"market_cap_min_usd": 0},
            "stable": {"market_cap_min_usd": 0},
        },
        "venue_quality": venue_block(
            enabled=True,
            min_quote_volume_24h_usdt=venue_floor,
            min_top_book_usdt_per_side=min_book,
            max_spread_pct=50,
            min_volume_to_order_multiple=volume_multiple,
        ),
        "liquidity_guard": {
            "min_quote_volume_24h_usdt": volume_floor,
            "depth_window_pct": 0.5,
            "order_book_cache_ttl_sec": 15,
            "depth_to_order_multiple": depth_multiple,
        },
    }
    if extra_risk:
        risk.update(extra_risk)
    raw = {
        "trading_mode": "paper",
        "max_usdt_per_trade": max_usdt,
        "max_position_percent": 100,
        "max_open_positions": 50,
        "stop_loss_pct": 20,
        "trade_cooldown_hours": 0,
        "risk": risk,
        "paper": {"initial_capital_usdt": 100_000},
    }
    if shorts:
        raw["shorts"] = {
            "enabled": True,
            "allow_live": False,
            "leverage_default": 2,
            "leverage_cap": 2,
            "max_open": 6,
            "max_margin_pct": 80,
            "volatile": {"market_cap_min_usd": 0},
            "stable": {"market_cap_min_usd": 0},
        }
    cfg = BotConfig()
    cfg._raw = raw
    return cfg


def _buy(symbol="LAB/USDT", usdt=25.0, *, source="technical", signal="BUY", price=1.0):
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=usdt,
        signal=signal,
        source=source,
    )


def _codes(dec) -> list:
    details = dec.details or {}
    return list(details.get("codes") or ([dec.code] if dec.code else []))


def _capped_lines(log) -> list[str]:
    return [
        str(call.args[0])
        for call in log.call_args_list
        if call.args and "liq_guard_size_capped" in str(call.args[0])
    ]


def _eval(rm, order, source, metrics, *, planned=None, position=None):
    with _eval_env(rm, position=position, metrics=metrics, mcap=50_000_000):
        if planned is None:
            return rm.evaluate(order, "1h", source=source, indicators={})
        with patch.object(
            rm, "_dynamic_size", return_value=(float(planned), {"total_multiplier": 1.0})
        ):
            return rm.evaluate(order, "1h", source=source, indicators={})


def _seed_book_age(symbol="LAB/USDT", age=4.0):
    import services.venue_quality as vq

    pair = symbol.replace("/", "_").upper()
    vq._book_cache[pair] = (time.time() - age, (1.0, 1.0, True, ()))


def test_t1_shipped_config_approves_an_order_inside_the_band(monkeypatch):
    raw = json.loads((ROOT / "config.json").read_text())
    raw["risk"]["min_trade_usdt"] = 15
    assert raw["risk"]["liquidity_guard"]["depth_to_order_multiple"] == 1
    assert raw["risk"]["venue_quality"]["min_volume_to_order_multiple"] == 20
    assert "liquidity_guard" not in raw.get("dry_run_defaults", {})
    assert "venue_quality" not in raw.get("dry_run_defaults", {})
    cfg = BotConfig()
    cfg._raw = raw
    rm = RiskManager(cfg)
    book = _book()
    with patch("logger.log") as log:
        dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(25)
    assert _capped_lines(log) == []


def test_t2_depth_multiple_cuts_to_the_thinner_band():
    rm = RiskManager(_cfg(depth_multiple=20))
    with patch("logger.log") as log:
        dec = _eval(rm, _buy(usdt=25), "technical", _book(), planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(16.25)
    assert dec.details["cap_depth"] == pytest.approx(16.25)
    assert dec.details["cap_volume"] == pytest.approx(30_000)
    lines = _capped_lines(log)
    assert len(lines) == 1
    assert "planned=25" in lines[0]
    assert "final=16.25" in lines[0]


def test_t3_cut_below_min_trade_is_depth_not_size_too_small():
    rm = RiskManager(_cfg(depth_multiple=25))
    dec = _eval(rm, _buy(usdt=25), "technical", _book(), planned=25)
    assert dec.approved is False
    assert dec.code == "liq_guard_depth_lt_order"
    assert dec.code != "size_too_small"
    assert dec.order is None


def test_t4_volume_multiple_binds_when_the_band_does_not():
    rm = RiskManager(_cfg(volume_floor=100))
    book = _book(bid=100_000, ask=100_000, qv=400)
    dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(20)
    assert dec.details["cap_volume"] == pytest.approx(20)


def test_t4b_order_above_volume_boundary_is_cut_not_rejected():
    rm = RiskManager(_cfg(volume_floor=100))
    book = _book(bid=100_000, ask=100_000, qv=400)
    dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(20)
    assert dec.code != "venue_liquidity_block"


def test_t4c_order_above_the_band_is_cut_to_the_band():
    rm = RiskManager(_cfg())
    dec = _eval(rm, _buy(usdt=400), "technical", _book(), planned=400)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(325)
    assert "liq_guard_depth_lt_order" not in _codes(dec)


def test_t4d_exact_boundary_is_not_cut_and_cents_floor_down():
    rm = RiskManager(_cfg(volume_floor=100))
    exact = _book(bid=100_000, ask=100_000, qv=400)
    with patch("logger.log") as log:
        kept = _eval(rm, _buy(usdt=20), "technical", exact, planned=20)
    assert kept.approved is True, kept.message
    assert kept.order.usdt_amount == pytest.approx(20)
    assert _capped_lines(log) == []

    # 16.256 rounds to 16.26. The floor must stay at 16.25, under the cap.
    odd = RiskManager(_cfg(volume_floor=1))
    book = _book(bid=16.256, ask=16.256, qv=1_000_000)
    cut = _eval(odd, _buy(usdt=25), "technical", book, planned=25)
    assert cut.approved is True, cut.message
    assert cut.order.usdt_amount == pytest.approx(16.25)
    assert cut.order.usdt_amount <= 16.256


def test_subcent_planned_size_is_not_logged_as_a_liquidity_cut():
    """A cap that does not bind is not a cut, even when the size has sub-cent digits."""
    rm = RiskManager(_cfg(volume_floor=100))
    book = _book(bid=100_000, ask=100_000, qv=10_000_000)
    with patch("logger.log") as log:
        dec = _eval(rm, _buy(usdt=25.004), "technical", book, planned=25.004)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(25.0)
    assert _capped_lines(log) == []


def test_relvol_subcent_balance_stays_approved_when_the_cap_does_not_bind():
    """Ticket then balance can leave 24.997. Staging approves that at 25.0.

    The liquidity cap is far above the order, so RelVol still uses the
    pre-ticket size and does not reject size_too_small.
    """
    rm = RiskManager(
        _cfg(
            min_trade=15,
            max_usdt=25,
            volume_floor=100,
            extra_risk={"relvol_min_trade_usdt": 1000},
        )
    )
    book = _book(bid=100_000, ask=100_000, qv=10_000_000)
    order = _buy(usdt=1500, source="gainer_relvol", signal="GAINER_RELVOL")
    with _eval_env(rm, metrics=book, mcap=50_000_000):
        with patch.object(
            rm, "_dynamic_size", return_value=(1500.0, {"total_multiplier": 1.0})
        ):
            with patch.object(rm, "_spendable_usdt", return_value=24.997):
                with patch("logger.log") as log:
                    dec = rm.evaluate(order, "1h", source="gainer_relvol", indicators={})
    assert dec.approved is True, dec.message
    assert dec.code != "size_too_small"
    assert dec.order.usdt_amount == pytest.approx(25.0)
    assert _capped_lines(log) == []


def test_t5_size_is_never_raised_to_the_cap():
    # min_trade 15 would reject an order of 10 before the cap is relevant.
    # The case uses the configured minimum, set under the order.
    rm = RiskManager(_cfg(min_trade=9))
    book = _book(bid=50, ask=50, qv=1_000_000)
    dec = _eval(rm, _buy(usdt=10), "technical", book, planned=10)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(10)


def test_t6_missing_multiples_fail_closed_and_the_stamp_does_not_invent_20():
    missing_depth = _cfg()
    del missing_depth.raw["risk"]["liquidity_guard"]["depth_to_order_multiple"]
    dec = _eval(
        RiskManager(missing_depth), _buy(), "technical", _book(), planned=25
    )
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"

    missing_k = _cfg()
    del missing_k.raw["risk"]["venue_quality"]["min_volume_to_order_multiple"]
    buy = _eval(RiskManager(missing_k), _buy(), "technical", _book(), planned=25)
    assert buy.approved is False
    assert buy.code == "liq_guard_missing_input"

    block = venue_block(
        enabled=True,
        min_quote_volume_24h_usdt=1e-9,
        min_top_book_usdt_per_side=1e-9,
        max_spread_pct=100,
    )
    del block["min_volume_to_order_multiple"]
    raw = {"risk": {"venue_quality": block}}
    metrics = _book(bid=10_000, ask=10_000, qv=100)
    reset_venue_cache_for_tests()
    with patch("services.venue_quality.log") as log:
        stamp = stamp_venue_for_fill(
            "LAB/USDT", planned_usdt=25, config_raw=raw, metrics=metrics
        )
    warnings = [
        str(call.args[0])
        for call in log.call_args_list
        if call.args and "min_volume_to_order_multiple" in str(call.args[0])
    ]
    assert len(warnings) == 1
    assert "config_problem=missing" in warnings[0]
    assert stamp.get("capture") == "missing"
    assert stamp.get("volume_to_order_mult") != 20
    assert is_thin_venue_stamp(stamp, raw["risk"]["venue_quality"]) is None


def test_t7_dca_limit_sees_the_cut_size():
    book = _book()
    approve_cfg = _cfg(depth_multiple=20, extra_risk={"max_daily_dca_usdt": 20})
    rm = RiskManager(approve_cfg)
    real = RiskManager._daily_dca_usdt_limit_blocked.__get__(rm, RiskManager)
    order = _buy(usdt=25, source="dca", signal="BUY_DCA")
    with _eval_env(rm, metrics=book, mcap=50_000_000):
        with patch.object(rm, "_daily_dca_usdt_sum", return_value=0.0):
            with patch.object(rm, "_daily_dca_usdt_limit_blocked", real):
                approved = rm.evaluate(order, "1h", source="dca", indicators={})
    assert approved.approved is True, approved.message
    assert approved.order.usdt_amount == pytest.approx(16.25)

    reject_cfg = _cfg(depth_multiple=20, extra_risk={"max_daily_dca_usdt": 16})
    rm2 = RiskManager(reject_cfg)
    real2 = RiskManager._daily_dca_usdt_limit_blocked.__get__(rm2, RiskManager)
    with _eval_env(rm2, metrics=book, mcap=50_000_000):
        with patch.object(rm2, "_daily_dca_usdt_sum", return_value=0.0):
            with patch.object(rm2, "_daily_dca_usdt_limit_blocked", real2):
                rejected = rm2.evaluate(order, "1h", source="dca", indicators={})
    assert rejected.approved is False
    assert rejected.code == "max_daily_dca_usdt"


def test_t7b_open_lot_cost_reduces_room_across_timeframes():
    update_position("LAB/USDT", "4h", "BUY", 10.0, 1.0)
    update_position("LAB/USDT", "1h", "BUY", 6.0, 1.0)
    book = _book(bid=20, ask=20, qv=1_000_000)
    rm = RiskManager(_cfg(volume_floor=100))
    blocked = _eval(rm, _buy(usdt=25, source="dca", signal="BUY_DCA"), "dca", book)
    assert blocked.approved is False
    assert blocked.code == "liq_guard_depth_lt_order"
    assert blocked.details["open_cost_usdt"] == pytest.approx(16)

    clear_positions_memory()
    update_position("LAB/USDT", "4h", "BUY", 10.0, 1.0)
    update_position("LAB/USDT", "1h", "BUY", 6.0, 1.0)
    small = RiskManager(_cfg(min_trade=3, volume_floor=100))
    kept = _eval(small, _buy(usdt=25, source="dca", signal="BUY_DCA"), "dca", book)
    assert kept.approved is True, kept.message
    assert kept.order.usdt_amount == pytest.approx(4)


def test_t7c_full_room_and_unreadable_cost_fail_closed():
    update_position("LAB/USDT", "4h", "BUY", 20.0, 1.0)
    book = _book(bid=20, ask=20, qv=1_000_000)
    rm = RiskManager(_cfg(volume_floor=100))
    full = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert full.approved is False
    assert full.code == "liq_guard_depth_lt_order"
    assert full.order is None
    assert (full.details or {}).get("final_usdt") in (None, 0)

    clear_positions_memory()
    _mark_positions_unknown(_resolve_store_key(), RuntimeError("unread"))
    unknown = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert unknown.approved is False
    assert unknown.code == "liq_guard_missing_input"

    clear_positions_memory()
    import strategies.positions as positions

    positions._activate(positions._resolve_store_key())
    positions._active_store()[positions.get_key("LAB/USDT", "4h")] = {
        "amount": 2.0,
        "average_entry": 0,
        "entry_price": 0,
        "last_buy_price": 0,
        "side": "long",
    }
    zero = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert zero.approved is False
    assert zero.code == "liq_guard_missing_input"


def test_short_lot_does_not_reduce_long_room():
    update_position("LAB/USDT", "1h", "SHORT", 16.0, 1.0)
    book = _book(bid=20, ask=20, qv=1_000_000)
    rm = RiskManager(_cfg(volume_floor=100))
    dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(20)
    assert dec.details["open_cost_usdt"] == pytest.approx(0)

    update_position("LAB/USDT", "4h", "BUY", 16.0, 1.0)
    cost, err = rm._open_cost_usdt("LAB/USDT", side="short")
    assert err is None
    assert cost == pytest.approx(16)
    long_cost, long_err = rm._open_cost_usdt("LAB/USDT", side="long")
    assert long_err is None
    assert long_cost == pytest.approx(16)


def test_below_venue_minimum_lot_counts_toward_open_cost():
    """A parked below-min lot still uses room. The one-way scan does not see it."""
    set_venue_limits_override(
        {"known": True, "min_cost": 18.0, "amount_step": 1e-8, "price_places": 8}
    )
    update_position("LAB/USDT", "4h", "BUY", 8.0, 1.0)
    assert refresh_venue_min_mark("LAB/USDT", "4h", mark=1.0, mark_fresh=True) == "set"
    assert find_open_positions_for_symbol("LAB/USDT") == []
    counted = find_open_positions_for_symbol(
        "LAB/USDT", include_below_venue_minimum=True
    )
    assert len(counted) == 1
    rm = RiskManager(_cfg(volume_floor=100))
    book = _book(bid=20, ask=20, qv=1_000_000)
    dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is False, dec.message
    assert dec.code == "liq_guard_depth_lt_order"
    assert dec.details["open_cost_usdt"] == pytest.approx(8)
    assert dec.order is None


def test_dust_under_one_usdt_is_not_in_the_open_scan():
    update_position("LAB/USDT", "4h", "BUY", 0.4, 1.0)
    assert find_open_positions_for_symbol("LAB/USDT") == []
    rm = RiskManager(_cfg(volume_floor=100))
    book = _book(bid=20, ask=20, qv=1_000_000)
    dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is True, dec.message
    assert dec.details["open_cost_usdt"] == pytest.approx(0)
    assert dec.order.usdt_amount == pytest.approx(20)


def test_t8_cut_size_fails_relvol_and_the_pair_minimum():
    rel = RiskManager(
        _cfg(volume_floor=100, extra_risk={"relvol_min_trade_usdt": 30})
    )
    book = _book(bid=20, ask=20, qv=1_000_000)
    relvol = _eval(
        rel, _buy(usdt=100, source="gainer_relvol"), "gainer_relvol", book, planned=100
    )
    assert relvol.approved is False
    assert relvol.code == "size_too_small"

    set_venue_limits_override(
        {"known": True, "min_cost": 18.0, "amount_step": 1e-8, "price_places": 8}
    )
    pair = RiskManager(_cfg(max_usdt=100, volume_floor=100))
    thin = _book(bid=16, ask=16, qv=1_000_000)
    blocked = _eval(pair, _buy(usdt=25), "technical", thin, planned=25)
    assert blocked.approved is False
    assert blocked.code == "venue_pair_blocked"


def test_t8b_merge_loss_bound_is_recomputed_on_the_cut_size():
    symbol = "SYM/USDT"
    set_venue_limits_override(
        {"known": True, "min_cost": 18.0, "amount_step": 1e-8, "price_places": 8}
    )
    rm = RiskManager(_cfg(max_usdt=100, min_trade=15, volume_floor=100))
    update_position(symbol, "1h", "BUY", 10.0, 1.0)
    stop = _first_firing_stop(20.0)
    buffer = 6.21
    factor = (stop - buffer) / 100.0
    cut_bound = factor * (10.0 + 20.0)
    uncut_bound = factor * (10.0 + 90.0)
    loss = (cut_bound + uncut_bound) / 2.0
    price = 10.0 - loss
    assert cut_bound < loss <= uncut_bound
    assert 0 < price < 18
    assert refresh_venue_min_mark(symbol, "1h", mark=price, mark_fresh=True) == "set"
    # Band 30 leaves room of 20 after the below-min cost of 10.
    book = _book(symbol=symbol, bid=30, ask=30, qv=1_000_000)
    order = _buy(symbol, usdt=100, source="auto", price=price)
    with _eval_env(rm, metrics=book, mcap=50_000_000, position=get_position(symbol, "1h")):
        with patch.object(
            rm,
            "_dynamic_size",
            side_effect=lambda base, *a, **k: (float(base), {"total_multiplier": 1.0}),
        ):
            dec = rm.evaluate(order, "1h", source="auto", indicators={})
    assert dec.approved is False
    assert dec.code == VENUE_MIN_MERGE_LOSS_EXCEEDED
    assert dec.details["reason"] == "loss_bound"
    assert dec.details["final_buy"] == pytest.approx(20)


def test_t8c_short_above_room_is_rejected_without_a_shrink():
    book = _book(bid=100, ask=100, qv=10_000_000)
    rm = RiskManager(_cfg(volume_floor=100, shorts=True))

    def _short(usdt, tf="1h"):
        return TradeOrder(
            type="SHORT",
            symbol="LAB/USDT",
            price=1.0,
            amount=0,
            usdt_amount=usdt,
            signal="SHORT",
            source="auto",
            leverage=2,
        ), tf

    above, tf = _short(150)
    with _eval_env(rm, metrics=book, mcap=50_000_000):
        dec = rm.evaluate(above, tf, source="auto")
    assert dec.approved is False
    assert dec.code == "liq_guard_depth_lt_order"
    assert above.usdt_amount == 150
    assert dec.order is None

    edge, _tf = _short(100)
    with _eval_env(rm, metrics=book, mcap=50_000_000):
        ok = rm.evaluate(edge, "1h", source="auto")
    assert ok.approved is True, ok.message
    assert ok.order.usdt_amount == pytest.approx(100)

    update_position("LAB/USDT", "4h", "BUY", 50.0, 1.0)
    held, _tf = _short(100)
    with _eval_env(rm, metrics=book, mcap=50_000_000):
        other = rm.evaluate(held, "1h", source="auto")
    assert other.approved is True, other.message
    assert other.order.usdt_amount == pytest.approx(100)

    clear_positions_memory()
    update_position("LAB/USDT", "1h", "SHORT", 16.0, 1.0)
    tight = _book(bid=20, ask=20, qv=10_000_000)
    adding, _tf = _short(10)
    with _eval_env(rm, metrics=tight, mcap=50_000_000, position=get_position("LAB/USDT", "1h")):
        refused = rm.evaluate(adding, "1h", source="auto")
    assert refused.approved is False
    assert refused.code == "liq_guard_depth_lt_order"
    assert adding.usdt_amount == 10


def test_t9_stop_does_not_consult_the_buy_lock():
    rm = RiskManager(_cfg())
    thin = _book(bid=1, ask=1, qv=1_000)
    order = TradeOrder(
        "SELL", "LAB/USDT", 1.0, 10, signal="SELL_STOP_FULL", source="exit_ws"
    )
    dec = _eval(
        rm,
        order,
        "exit_ws",
        thin,
        position={"amount": 10.0, "average_entry": 1.0, "dca_rounds": 0},
    )
    assert dec.approved is True, dec.message
    assert not str(dec.code).startswith("liq_guard_")


def test_t10_fixture_entry_stays_volume_low_for_every_depth_multiple():
    row = None
    with FIXTURE.open(encoding="utf-8") as handle:
        for item in csv.DictReader(handle):
            if item["order_id"] == "c1278fdd8690":
                row = item
                break
    assert row is not None
    symbol = row["symbol"]
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    book = _book(
        symbol=symbol,
        bid=325,
        ask=1694,
        qv=float(row["qv24_logged"]),
    )
    for multiple in (1, 25):
        rm = RiskManager(_cfg(depth_multiple=multiple))
        dec = _eval(
            rm,
            _buy(symbol, usdt=1000, source="entry_sensor_15m", price=float(row["price"])),
            "entry_sensor_15m",
            book,
            planned=1000,
        )
        assert dec.approved is False, multiple
        assert "liq_guard_volume_low" in _codes(dec), multiple


def test_t11_tape_records_the_guard_on_approve_cut_and_reject(monkeypatch, tmp_path):
    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    monkeypatch.setenv("BUY_DECISION_TAPE_UNDER_TEST", "1")
    _seed_book_age()
    # Band 325/1694, qv 600_000, k 20 → cap_volume 30_000. Depth multiple
    # 1 leaves the 25 order inside the cap. 20 cuts to 16.25. 25 cuts to
    # 13, which is under min_trade 15.
    cases = (
        (1, "accepted", 25.0, 325.0, 325.0, 325.0),
        (20, "accepted", 16.25, 16.25, 16.25, 16.25),
        (25, "rejected", 13.0, 13.0, 13.0, 13.0),
    )
    for multiple, _outcome, _final, _cap, _depth, _allowed in cases:
        rm = RiskManager(_cfg(depth_multiple=multiple))
        _eval(rm, _buy(usdt=25), "technical", _book(), planned=25)
    path = tmp_path / "logs" / "buy_decision_tape.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) == 3
    for row, (_multiple, outcome, final, cap, depth, allowed) in zip(rows, cases):
        assert row["outcome"] == outcome
        guard = row["book"]["guard"]
        assert guard["band_bid_usdt"] == pytest.approx(325)
        assert guard["band_ask_usdt"] == pytest.approx(1694)
        assert guard["qv24_usdt"] == pytest.approx(600_000)
        assert guard["book_age_sec"] == pytest.approx(4, abs=2)
        assert guard["liq_cap_usdt"] == pytest.approx(cap)
        assert guard["cap_depth"] == pytest.approx(depth)
        assert guard["cap_volume"] == pytest.approx(30_000)
        assert guard["open_cost_usdt"] == pytest.approx(0)
        assert guard["planned_usdt"] == pytest.approx(25)
        assert guard["final_usdt"] == pytest.approx(final)
        assert guard["allowed_usdt"] == pytest.approx(allowed)


def test_t12_fill_stamp_still_applies_the_volume_multiple():
    raw = {
        "risk": {
            "venue_quality": venue_block(
                enabled=True,
                min_quote_volume_24h_usdt=1e-9,
                min_top_book_usdt_per_side=1e-9,
                max_spread_pct=100,
                min_volume_to_order_multiple=20,
            )
        }
    }
    metrics = _book(bid=10_000, ask=10_000, qv=100)
    stamp = stamp_venue_for_fill(
        "LAB/USDT", planned_usdt=25, config_raw=raw, metrics=metrics
    )
    assert stamp["venue_ok"] is False
    assert any("× order" in str(reason) for reason in stamp["venue_reasons"])
    assert is_thin_venue_stamp(stamp, as_venue_config(raw["risk"]["venue_quality"])) is True


@pytest.mark.parametrize("bad", (float("nan"), float("inf")))
def test_non_finite_depth_multiple_is_missing_input(bad):
    cfg = _cfg()
    cfg.raw["risk"]["liquidity_guard"]["depth_to_order_multiple"] = bad
    dec = _eval(RiskManager(cfg), _buy(), "technical", _book(), planned=25)
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"
    assert dec.order is None


@pytest.mark.parametrize("bad", (float("nan"), float("inf")))
def test_non_finite_volume_multiple_is_missing_input(bad):
    cfg = _cfg()
    cfg.raw["risk"]["venue_quality"]["min_volume_to_order_multiple"] = bad
    dec = _eval(RiskManager(cfg), _buy(), "technical", _book(), planned=25)
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"
    assert dec.order is None


@pytest.mark.parametrize(
    ("bid", "ask"),
    ((float("nan"), 1_000.0), (float("inf"), float("inf"))),
)
def test_non_finite_depth_is_missing_input(bid, ask):
    rm = RiskManager(_cfg(volume_floor=100, min_book=1e-9))
    book = _book(bid=bid, ask=ask, qv=1_000_000)
    dec = _eval(rm, _buy(usdt=25), "technical", book, planned=25)
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"
    assert dec.order is None


def test_manual_buy_is_not_capped():
    rm = RiskManager(_cfg(depth_multiple=20))
    order = _buy(usdt=25, source="manual")
    dec = _eval(rm, order, "manual", _book(), planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(25)
