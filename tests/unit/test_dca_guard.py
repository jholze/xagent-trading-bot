"""Hard DCA lock (#640) at order submit.

Replay keys are the decision identities from #639. The orders store has no
2Z rows (``xagent_orders`` returned 0), so the tests do not read it.
Times in the fixture names are Europe/Berlin; the ids use the true UTC.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from strategies.dca_policy import evaluate_dca_guard
from risk.risk_manager import RiskManager
from services.venue_quality import VenueMetrics
from strategies.positions import (
    get_position,
    positions,
    update_position,
)
from tests.unit.test_long_mcap_venue_563 import _eval_env

# Gate 24h volume recorded for 2Z on 06.10. Band depth is above every
# order in this file so the volume rule is what blocks, not the book.
_ZZ_VOLUME = 301_891.0
_THICK = VenueMetrics(
    symbol="2Z/USDT",
    quote_volume_24h_usdt=5_000_000.0,
    last=1.0,
    bid=0.999,
    ask=1.001,
    bid_size=100_000.0,
    ask_size=100_000.0,
    spread_pct=0.2,
    top_book_bid_usdt=100_000.0,
    top_book_ask_usdt=100_000.0,
    capture="ok",
    depth_bid_usdt=100_000.0,
    depth_ask_usdt=100_000.0,
    depth_parsed=True,
)
_ZZ_BOOK = VenueMetrics(
    symbol="2Z/USDT",
    quote_volume_24h_usdt=_ZZ_VOLUME,
    last=0.04283,
    bid=0.0428,
    ask=0.0429,
    bid_size=1_000.0,
    ask_size=1_000.0,
    spread_pct=0.23,
    top_book_bid_usdt=50_000.0,
    top_book_ask_usdt=50_000.0,
    capture="ok",
    depth_bid_usdt=50_000.0,
    depth_ask_usdt=50_000.0,
    depth_parsed=True,
)


def _cfg():
    return BotConfig(
        {
            "max_usdt_per_trade": 5000,
            "max_open_positions": 100,
            "trade_cooldown_hours": 0,
            "max_position_percent": 100,
            "max_daily_trades": 0,
            "risk": {
                "daily_loss_limit_pct": 100,
                "min_trade_usdt": 5,
                "cash_floor_pct": 0,
                "max_daily_sells": 0,
                "liquidity_guard": {
                    "min_quote_volume_24h_usdt": 500000,
                    "depth_window_pct": 0.5,
                    "order_book_cache_ttl_sec": 15,
                },
            },
            "trading": {"mode": "paper", "initial_capital": 10000},
            "live": {"execution": "shadow", "dry_run": True},
        }
    )


def _buy(**kw) -> TradeOrder:
    base = dict(
        type="BUY",
        symbol="2Z/USDT",
        price=0.07,
        amount=0,
        usdt_amount=400,
        signal="BUY_DCA",
        source="dca",
    )
    base.update(kw)
    return TradeOrder(**base)


def _lot(**kw) -> dict:
    lot = {
        "amount": 14_000.0,
        "average_entry": 0.068056,
        "dca_rounds": 0,
        "symbol": "2Z/USDT",
    }
    lot.update(kw)
    return lot


def _codes(dec) -> list[str]:
    details = dec.details or {}
    return list(details.get("codes") or ([dec.code] if dec.code else []))


@contextmanager
def _open(rm, lot, metrics=_THICK, timeframe_positions=None):
    def _gp(symbol, tf, tenant_id=None):
        if timeframe_positions is not None:
            return timeframe_positions.get(tf) or {"amount": 0, "dca_rounds": 0}
        return lot

    found = None
    if timeframe_positions:
        for tf, pos in timeframe_positions.items():
            if float((pos or {}).get("amount") or 0) > 0:
                found = (tf, pos)
                break

    with _eval_env(
        rm,
        position=lot,
        metrics=metrics,
        mcap=50_000_000,
        get_position_side_effect=_gp if timeframe_positions is not None else None,
        open_found=found,
    ):
        yield


def test_r1_blocks_below_average_even_when_rsi_is_oversold():
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=0, average_entry=0.068056)
    with _open(rm, lot):
        dec = rm.evaluate(
            _buy(price=0.062835, usdt_amount=400),
            "15m",
            source="dca",
            indicators={"rsi": 35.5},
        )
    assert dec.approved is False
    assert dec.code == "dca_guard_below_avg"
    assert "dca_guard_below_avg" in _codes(dec)
    assert dec.details["price"] == pytest.approx(0.062835)
    assert dec.details["avg"] == pytest.approx(0.068056)
    assert "buy_dca" not in dec.code


def test_r1_live_equal_to_average_passes():
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=0, average_entry=1.0)
    with _open(rm, lot):
        dec = rm.evaluate(
            _buy(price=1.0, usdt_amount=400, symbol="LAB/USDT"),
            "1h",
            source="dca",
            indicators={"rsi": 20},
        )
    assert dec.approved is True, f"{dec.code}: {dec.message}"


def test_r2_second_round_blocked_and_first_round_allowed_above_avg():
    rm = RiskManager(_cfg())
    with _open(rm, _lot(dca_rounds=0, average_entry=1.0)):
        first = rm.evaluate(_buy(price=1.05, usdt_amount=400), "1h", source="dca")
    assert first.approved is True, f"{first.code}: {first.message}"
    with _open(rm, _lot(dca_rounds=1, average_entry=1.0)):
        second = rm.evaluate(_buy(price=1.05, usdt_amount=400), "1h", source="dca")
    assert second.approved is False
    assert second.code == "dca_guard_max_rounds"


def test_r2b_any_lock_blocks_and_there_is_no_allowance():
    rm = RiskManager(_cfg())
    lot = _lot(
        dca_rounds=0,
        average_entry=1.0,
        lock={"enabled": True, "modes": ["no_auto_sell", "no_evict"], "reason": "telegram"},
    )
    with _open(rm, lot):
        dec = rm.evaluate(_buy(price=1.2, usdt_amount=400), "1h", source="dca")
    assert dec.approved is False
    assert "dca_guard_locked" in _codes(dec)
    assert dec.details["locked"] is True


def test_r2c_second_timeframe_is_an_add():
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=0, average_entry=1.0)
    with _open(rm, lot, timeframe_positions={"15m": {"amount": 0}, "1h": lot}):
        dec = rm.evaluate(
            _buy(price=0.5, usdt_amount=400, signal="BUY", source="entry_sensor_15m"),
            "15m",
            source="entry_sensor_15m",
        )
    assert dec.approved is False
    assert dec.code == "dca_guard_below_avg"


def test_r3_missing_average_blocks():
    rm = RiskManager(_cfg())
    lot = _lot(average_entry=0, dca_rounds=0)
    with _open(rm, lot):
        dec = rm.evaluate(_buy(price=1.0), "1h", source="dca")
    assert dec.code == "dca_guard_missing_input"


def test_r3_stale_price_uses_existing_freshness_rule():
    rm = RiskManager(_cfg())
    with _open(rm, _lot(dca_rounds=0, average_entry=1.0)):
        dec = rm.evaluate(
            _buy(price=1.1),
            "1h",
            source="dca",
            indicators={"price_age_sec": 10_000},
        )
    assert dec.code == "dca_guard_missing_input"


def test_new_entry_without_a_lot_is_not_a_dca_add():
    rm = RiskManager(_cfg())
    with _open(rm, {"amount": 0, "dca_rounds": 0, "average_entry": 0}):
        dec = rm.evaluate(
            _buy(price=1.0, signal="BUY", source="entry_sensor_15m", usdt_amount=200),
            "15m",
            source="entry_sensor_15m",
        )
    assert dec.approved is True, f"{dec.code}: {dec.message}"
    assert not any(c.startswith("dca_guard_") for c in _codes(dec))


def test_human_operator_is_exempt_mcp_bot_is_not():
    """A human buy is not auto-blocked. The same checks still run and are logged."""
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=2, average_entry=1.0)
    with _open(rm, lot):
        with patch("logger.log") as log:
            human = rm.evaluate(
                _buy(price=0.4, source="manual", signal="BUY"),
                "1h",
                source="manual",
            )
        bot = rm.evaluate(
            _buy(price=0.4, source="mcp:agent-7", signal="BUY"),
            "1h",
            source="mcp:agent-7",
        )
    assert human.approved is True, f"{human.code}: {human.message}"
    texts = [str(call.args[0]) for call in log.call_args_list if call.args]
    assert any(
        line.startswith("manual_buy_guard")
        and "blocked=True" in line
        and "dca_guard_below_avg" in line
        for line in texts
    ), texts
    assert bot.approved is False
    assert bot.code.startswith("dca_guard_")


@pytest.mark.parametrize(
    "source,signal",
    [
        ("dca", "BUY_DCA"),
        ("dca_sniper", "BUY_DCA"),
        ("dca_recovery", "BUY_DCA"),
        ("entry_sensor_15m", "BUY"),
        ("mcp:henry-bot", "BUY"),
    ],
)
def test_g12_every_add_caller_hits_the_submit_guard(source, signal):
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=0, average_entry=1.0)
    frames = None
    if source == "entry_sensor_15m":
        frames = {"15m": {"amount": 0}, "1h": lot}
    with _open(rm, lot, timeframe_positions=frames):
        dec = rm.evaluate(
            _buy(price=0.5, source=source, signal=signal, usdt_amount=400),
            "15m" if source == "entry_sensor_15m" else "1h",
            source=source,
        )
    assert dec.approved is False
    assert dec.code == "dca_guard_below_avg"
    assert dec.details["price"] == pytest.approx(0.5)


def test_g5_deploy_boost_request_from_2026_10_06_hits_the_lock():
    """Replay one deploy_boost 2Z request from 2026-10-06 (Berlin morning).

    The sniper logged action=buy_dca at 759 USDT (policy mult 1.35) and the
    round cap was the only thing that stopped the fill. The lock blocks it.
    decision id: default:2Z/USDT:dca_sniper_deep:deploy_boost:2026-10-06T02:25:00Z
    Berlin 2026-10-06 04:25.
    """
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=2, average_entry=0.06351)
    with _open(rm, lot, metrics=_ZZ_BOOK):
        dec = rm.evaluate(
            _buy(
                price=0.04283,
                usdt_amount=759,
                source="dca_sniper",
                signal="BUY_DCA",
            ),
            "15m",
            source="dca_sniper",
            indicators={"rsi": 30, "deploy_boost": True},
        )
    assert dec.approved is False
    assert "dca_guard_below_avg" in _codes(dec)
    assert "dca_guard_max_rounds" in _codes(dec)
    assert "liq_guard_volume_low" in _codes(dec)
    assert "liq_guard_depth_lt_order" not in _codes(dec)
    assert dec.details["price"] == pytest.approx(0.04283)
    assert dec.details["planned_usdt"] == pytest.approx(759)


def test_replay_2z_addon_buys_by_decision_id():
    """Both real 2Z adds, keyed on decision ids, not the orders store.

    Berlin 2026-09-30 19:09:43 → UTC 2026-09-30 17:09:43
    Berlin 2026-10-01 13:15:13 → UTC 2026-10-01 11:15:13
    """
    rows = [
        {
            "decision_id": "default:2Z/USDT:BUY_DCA:2026-09-30T17:09:43Z",
            "berlin": "2026-09-30 19:09:43",
            "price": 0.062835,
            "usdt": 400,
            "rounds": 0,
            "avg": 0.068056,
        },
        {
            "decision_id": "default:2Z/USDT:BUY_DCA:2026-10-01T11:15:13Z",
            "berlin": "2026-10-01 13:15:13",
            "price": 0.05796,
            "usdt": 450,
            "rounds": 1,
            "avg": 0.0665,
        },
    ]
    rm = RiskManager(_cfg())
    for row in rows:
        lot = _lot(dca_rounds=row["rounds"], average_entry=row["avg"])
        with _open(rm, lot, metrics=_ZZ_BOOK):
            dec = rm.evaluate(
                _buy(price=row["price"], usdt_amount=row["usdt"]),
                "15m",
                source="dca",
            )
        assert dec.approved is False, row["decision_id"]
        assert "dca_guard_below_avg" in _codes(dec), row["berlin"]
        if row["rounds"] >= 1:
            assert "dca_guard_max_rounds" in _codes(dec)
        assert "liq_guard_volume_low" in _codes(dec)
        assert "liq_guard_depth_lt_order" not in _codes(dec)
        assert dec.details["price"] == pytest.approx(row["price"])


def test_g7_henry_2z_lot_is_guarded_per_tenant(tmp_path, monkeypatch):
    """henry 2Z, dca_rounds 2. Known add: Berlin 2026-10-02 22:15:49
    (UTC 2026-10-02 20:15:49). The fill price was not in the log; the open
    lot's round count is what the lock uses.
    """
    monkeypatch.setenv("BUY_DECISION_TAPE_UNDER_TEST", "1")
    monkeypatch.setenv("BUY_DECISION_TAPE", "1")
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=2, average_entry=0.059067)
    from core.tenant_context import tenant_context

    # Tenant config for henry lives in Mongo. The test only needs the
    # tenant id on the tape, not a live tenant document.
    with tenant_context("henry", scope="demo"), patch(
        "strategies.registry.get_bot_config", return_value=_cfg()
    ), patch("price_fetcher._stale_price_max_age_sec", return_value=300.0), patch(
        "data_manager.get_config",
        return_value={"observability": {"json_logs": False}},
    ):
        with _open(rm, lot, metrics=_ZZ_BOOK):
            order = _buy(price=0.059067, usdt_amount=400, source="dca")
            order.idempotency_key = "henry:2Z/USDT:BUY_DCA:2026-10-02T20:15:49Z"
            dec = rm.evaluate(order, "15m", source="dca")
    assert dec.approved is False
    assert "dca_guard_max_rounds" in _codes(dec)
    from strategies.buy_decision_tape import tape_path

    rows = [json.loads(line) for line in open(tape_path(), encoding="utf-8") if line.strip()]
    assert rows
    row = rows[-1]
    assert row["tenant"] == "henry"
    assert "dca_guard_max_rounds" in row["filter_codes"]
    assert row["filter_codes"] != ["buy_dca"]
    assert row["book"]["guard"]["price"] == pytest.approx(0.059067)
    assert row["book"]["guard"]["dca_rounds"] == 2
    assert row["correlation_id"] == "henry:2Z/USDT:BUY_DCA:2026-10-02T20:15:49Z"


def test_sells_and_stops_are_not_blocked_by_the_dca_guard():
    rm = RiskManager(_cfg())
    lot = _lot(dca_rounds=2, average_entry=1.0, amount=100)
    for signal in ("SELL_FULL", "SELL_STOP_FULL", "SELL_STOP_PARTIAL"):
        order = TradeOrder(
            type="SELL",
            symbol="2Z/USDT",
            price=0.01,
            amount=10,
            signal=signal,
            source="exit_ws",
        )
        with _open(rm, lot, metrics=_ZZ_BOOK):
            dec = rm.evaluate(order, "15m", source="exit_ws")
        assert dec.approved is True, f"{signal} {dec.code}: {dec.message}"
        assert not str(dec.code).startswith("dca_guard_")
        assert not str(dec.code).startswith("liq_guard_")


def test_counter_partial_sell_keeps_lock_keeps_full_close_resets():
    positions.clear()
    symbol = "CNT/USDT"
    tf = "1h"
    update_position(symbol, tf, "BUY", 10.0, amount_traded=10.0, source="manual")
    update_position(symbol, tf, "BUY_DCA", 8.0, amount_traded=5.0, source="dca")
    assert int(get_position(symbol, tf)["dca_rounds"]) == 1

    update_position(symbol, tf, "SELL", 9.0, amount_traded=3.0, source="manual")
    after_partial = get_position(symbol, tf)
    assert float(after_partial["amount"]) > 0
    assert int(after_partial["dca_rounds"]) == 1

    after_partial["lock"] = {
        "enabled": True,
        "modes": ["no_auto_sell"],
        "reason": "test",
    }
    assert int(get_position(symbol, tf)["dca_rounds"]) == 1

    update_position(symbol, tf, "SELL_FULL", 9.0, source="manual")
    closed = get_position(symbol, tf)
    assert float(closed["amount"]) == 0
    assert int(closed["dca_rounds"]) == 0

    update_position(symbol, tf, "BUY", 11.0, amount_traded=4.0, source="manual")
    reopened = get_position(symbol, tf)
    assert float(reopened["amount"]) > 0
    assert int(reopened["dca_rounds"]) == 0
    positions.clear()


def test_merge_does_not_inherit_rounds_onto_a_new_cycle():
    from strategies.positions import derive_positions_from_orders_and_cache

    snap = {
        "ZRO_USDT_1h": {
            "amount": 10.0,
            "average_entry": 2.0,
            "dca_rounds": 0,
            "first_buy_at": "2026-10-01T00:00:00Z",
        }
    }
    cache = {
        "positions": {
            "ZRO_USDT_1h": {
                "amount": 10.0,
                "average_entry": 2.0,
                "dca_rounds": 2,
                "first_buy_at": "2026-08-01T00:00:00Z",
            }
        }
    }
    merged = derive_positions_from_orders_and_cache(snap, cache)
    assert merged["ZRO_USDT_1h"]["dca_rounds"] == 0

    same_cycle_cache = {
        "positions": {
            "ZRO_USDT_1h": {
                "amount": 10.0,
                "dca_rounds": 2,
                "first_buy_at": "2026-10-01T00:00:00Z",
            }
        }
    }
    same = derive_positions_from_orders_and_cache(snap, same_cycle_cache)
    assert same["ZRO_USDT_1h"]["dca_rounds"] == 2


def test_pure_guard_order_of_codes():
    result = evaluate_dca_guard(
        {
            "amount": 1,
            "average_entry": 2.0,
            "dca_rounds": 3,
            "lock": {"enabled": True, "modes": ["no_auto_sell"]},
        },
        price=1.0,
        source="dca",
        has_open_lot=True,
        symbol="2Z/USDT",
    )
    assert result.codes == [
        "dca_guard_locked",
        "dca_guard_below_avg",
        "dca_guard_max_rounds",
    ]
