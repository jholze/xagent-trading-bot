"""H2 #735 B1: a lot is symbol + timeframe, not a hardcoded 1h lot.

Same symbol is held on 1h and 4h with different amounts and entries. Escalation
and the daily-cap below-entry check must read the order's timeframe.

Kills Lena's om10 (escalation calls get_position(symbol, "1h")) and om9 (the
daily-cap below-entry check reads the 1h lot). Also pins om5: a marked FULL
whose lot has no positive average_entry stays capped (fail closed).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from test_h2_rev1m_viktor import _bypass
from test_h2_ws_partial_min_size import (
    ENTRY,
    STRK_PX,
    TENANTS,
    _amount,
    _analysis,
    _daily_cap_reached,
    _err,
    _filled_sells,
    _hub,
    _logs,
    _lot,
    _orders,
    _paper,
    _repo_floors_and_clean_books,  # noqa: F401  autouse fixture
    _rm,
    _tenant,
    _upgrades,
    _vol_params,
)

# Distinct sizes so a 1h read cannot match a 4h order (and the reverse).
AMT = {"1h": 100.0, "4h": 250.0}
TFS = ("1h", "4h")


def _seed_tf(symbol, amount, tf, **extra):
    from strategies import positions as pm

    lot = _lot(amount, **extra)
    pm._active_store()[pm.get_key(symbol, tf)] = lot
    pm._positions_state[pm._current_active_key()] = "known"
    return lot


def _held_tf(symbol, tf):
    from strategies.positions import get_position

    return float((get_position(symbol, tf) or {}).get("amount") or 0)


def _seed_both(symbol, *, entry_1h=ENTRY, entry_4h=ENTRY):
    return {
        "1h": _seed_tf(symbol, AMT["1h"], "1h", entry=entry_1h),
        "4h": _seed_tf(symbol, AMT["4h"], "4h", entry=entry_4h),
    }


def _other(tf):
    return "4h" if tf == "1h" else "1h"


def _row(symbol, position, params, tid, tf):
    row = {
        "symbol": symbol,
        "timeframe": tf,
        "position": dict(position),
        "average_entry": float(position.get("average_entry") or ENTRY),
        "recent_high": float(position.get("recent_high") or ENTRY),
        "strategy_params": params,
        "atr_pct": 3.0,
    }
    if tid != "default":
        row["tenant_id"] = tid
    return row


def _ws_tf(sym, lot, params, tid, tf):
    hub = _hub(0.0)
    hub.update_book([_row(sym, lot, params, tid, tf)])
    hub.handle_ws_message(json.dumps({
        "time": 1, "channel": "spot.tickers", "event": "update",
        "result": {
            "currency_pair": sym.replace("/", "_"), "last": str(STRK_PX),
            "highest_bid": str(STRK_PX), "change_percentage": "-12.5",
            "quote_volume": "1000",
        },
    }))
    return hub


def _cycle_tf(symbol, tf):
    from services.signal_orchestrator import SignalOrchestrator

    analysis = _analysis(symbol)
    analysis.timeframe = tf
    orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
    with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
        return orch.execute_if_needed(analysis, {"symbol": symbol, "timeframe": tf}, STRK_PX)


def _assert_full_on(sym, tf):
    """One FULL for this symbol, sized to ``tf``. Do not use ``_assert_one_full``:
    that helper treats the lot as 1h."""
    filled = _filled_sells(sym)
    assert len(filled) == 1, [(o.get("signal"), o.get("status"), _err(o)) for o in _orders(sym)]
    row = filled[0]
    assert row["signal"] == "SELL_STOP_FULL", row.get("signal")
    assert row.get("exit_source") == "stop_loss"
    assert _amount(row) == pytest.approx(AMT[tf], rel=1e-6)
    assert _held_tf(sym, tf) == pytest.approx(0.0, abs=1e-6)
    assert _held_tf(sym, _other(tf)) == pytest.approx(AMT[_other(tf)])


def _marked_full(sym, tf, *, price=STRK_PX):
    return TradeOrder(
        type="SELL", symbol=sym, price=price, amount=AMT[tf],
        signal="SELL_STOP_FULL", source="exit_ws", exit_source="stop_loss",
        hard_stop_origin="exit_ws",
    )


class TestLotIsSymbolPlusTimeframe:
    """om10: escalation must sell the order's lot, not the 1h lot of the same symbol."""

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("tf", TFS)
    def test_risk_direct_full_amount_is_that_lot(self, tid, tf):
        sym = f"MTFR{tf[0]}{tid[:1].upper()}/USDT"
        with _tenant(tid), _logs() as lines:
            _seed_both(sym)
            order = TradeOrder(
                type="SELL", symbol=sym, price=STRK_PX, amount=1.0,
                signal="SELL_STOP_PARTIAL", source="exit_ws", exit_source="stop_loss",
                hard_stop_origin="exit_ws",
            )
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(order, tf, source="exit_ws", locked_sell_checks=True)
            assert dec.approved and dec.order.signal == "SELL_STOP_FULL", (dec.code, dec.message)
            assert dec.order.amount == pytest.approx(AMT[tf])
            assert _held_tf(sym, "1h") == pytest.approx(AMT["1h"])
            assert _held_tf(sym, "4h") == pytest.approx(AMT["4h"])
        up = _upgrades(lines)
        assert len(up) == 1 and f"timeframe={tf}" in up[0][1] and "origin=exit_ws" in up[0][1], up

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("tf", TFS)
    def test_ws_chain_closes_only_that_lot(self, tid, tf):
        sym = f"MTFW{tf[0]}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            lots = _seed_both(sym)
            with _paper(), _logs() as lines:
                _ws_tf(sym, lots[tf], _vol_params(), tid, tf)
            _assert_full_on(sym, tf)
        assert len(_upgrades(lines)) == 1 and f"timeframe={tf}" in _upgrades(lines)[0][1]

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("tf", TFS)
    def test_cycle_closes_only_that_lot(self, tid, tf):
        sym = f"MTFC{tf[0]}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed_both(sym)
            with _paper(), _logs() as lines:
                res = _cycle_tf(sym, tf)
            assert res is not None and res.executed, getattr(res, "message", None)
            _assert_full_on(sym, tf)
        up = _upgrades(lines)
        assert len(up) == 1 and f"timeframe={tf}" in up[0][1] and "origin=long_hard_stop" in up[0][1], up


class TestDailyCapBelowEntryUsesOrderTimeframe:
    """om9: the bypass reads the order timeframe's average_entry, not the 1h lot."""

    @pytest.mark.parametrize("tid", TENANTS)
    def test_4h_below_entry_bypasses_when_1h_is_above_price(self, tid):
        sym = f"MTFD{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached(), _logs() as lines:
            # 4h is a loss (must bypass). 1h entry is below the price (must not).
            _seed_both(sym, entry_1h=0.50, entry_4h=ENTRY)
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(_marked_full(sym, "4h"), "4h", source="exit_ws", locked_sell_checks=True)
            assert dec.approved, (dec.code, dec.message)
            assert _held_tf(sym, "1h") == pytest.approx(AMT["1h"])
            assert _held_tf(sym, "4h") == pytest.approx(AMT["4h"])
        b = _bypass(lines)
        assert len(b) == 1 and "origin=exit_ws" in b[0], b

    @pytest.mark.parametrize("tid", TENANTS)
    def test_4h_at_or_above_entry_capped_when_1h_is_below_price(self, tid):
        sym = f"MTFE{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached(), _logs() as lines:
            # 4h entry equals the price (strict < fails). 1h is a loss, so a 1h read would bypass.
            _seed_both(sym, entry_1h=ENTRY, entry_4h=STRK_PX)
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(_marked_full(sym, "4h"), "4h", source="exit_ws", locked_sell_checks=True)
            assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)
            assert _held_tf(sym, "1h") == pytest.approx(AMT["1h"])
            assert _held_tf(sym, "4h") == pytest.approx(AMT["4h"])
        assert not _bypass(lines), lines


class TestBypassFailsClosedWithoutEntry:
    """om5: missing or zero average_entry does not bypass the daily cap."""

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("entry", [0, None])
    def test_marked_full_without_positive_entry_is_capped(self, tid, entry):
        sym = f"MTFN{tid[:1].upper()}{0 if entry == 0 else 1}/USDT"
        with _tenant(tid), _daily_cap_reached(), _logs() as lines:
            lot = _seed_tf(sym, AMT["1h"], "1h", entry=ENTRY)
            if entry is None:
                lot.pop("average_entry", None)
            else:
                lot["average_entry"] = entry
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(
                    _marked_full(sym, "1h"), "1h", source="exit_ws", locked_sell_checks=True
                )
        assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)
        assert not _bypass(lines), lines
        assert _filled_sells(sym) == []
