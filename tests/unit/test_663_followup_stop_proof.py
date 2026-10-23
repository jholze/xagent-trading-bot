"""#663 follow-up: proof the full-stop exception stays narrow.

Tests only. A relabelled stop, a manual sell, a venue dust reject, and a
second full stop inside the live cooldown must not widen the exception.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tests.unit.test_ws_long_hard_stop_657 import (
    _clear_guards,
    _fire,
    _held,
    _hub,
    _logs,
    _orders,
    _paper,
    _params,
    _row,
    _seed,
)


@pytest.fixture(autouse=True)
def _reset_books():
    from strategies.positions import reset_all_position_stores_for_tests

    reset_all_position_stores_for_tests()
    _clear_guards()
    yield
    _clear_guards()
    reset_all_position_stores_for_tests()


def _markets(symbol, min_cost):
    from unittest.mock import MagicMock

    ex = MagicMock()
    ex.markets = {
        symbol: {"limits": {"amount": {"min": 0.0}, "cost": {"min": min_cost}}}
    }
    ex.load_markets.return_value = ex.markets
    ex.amount_to_precision.side_effect = lambda _sym, amt: float(amt)
    return ex


@contextmanager
def _gate(symbol, min_cost):
    from execution.gate_adapter import GateExecutionAdapter

    ex = _markets(symbol, min_cost)
    prev_cache = GateExecutionAdapter._shadow_markets_cache
    prev_failed = GateExecutionAdapter._shadow_markets_failed
    try:
        GateExecutionAdapter._shadow_markets_failed = False
        GateExecutionAdapter._shadow_markets_cache = None
        with patch.object(GateExecutionAdapter, "_get_exchange", lambda self: ex):
            yield
    finally:
        GateExecutionAdapter._shadow_markets_cache = prev_cache
        GateExecutionAdapter._shadow_markets_failed = prev_failed


def _filled(symbol):
    return [o for o in _orders(symbol) if o.get("status") == "filled"]


def _below(symbol, timeframe="1h"):
    from strategies.positions import BELOW_VENUE_MINIMUM

    return _seed(
        symbol,
        timeframe=timeframe,
        amount=80,
        entry=1.0,
        venue_min_state=BELOW_VENUE_MINIMUM,
    )


class TestRelabeledFullStop:
    def test_relabeled_sell_stop_full_above_threshold_is_rejected(self):
        """A SELL_STOP_FULL label is not the exception.

        Two orders, each rejected by one gate:
        the exit_source check, and the evaluate_long_hard_stop recompute.
        Removing either gate alone lets that order fill.
        """
        from services.exit_realtime.execute import try_execute_trail_exit
        from strategies.positions import get_position, is_below_venue_minimum

        params = _params(full=10, partial=5)
        relabeled = "K663R/USDT"
        above = "K663A/USDT"
        _below(relabeled)
        _below(above)
        assert is_below_venue_minimum(get_position(relabeled, "1h"))
        assert is_below_venue_minimum(get_position(above, "1h"))

        with _paper():
            # Real full stop (11% vs 10%), but the source is not stop_loss.
            with _gate(relabeled, 1.0):
                relabeled_out = try_execute_trail_exit(
                    symbol=relabeled,
                    timeframe="1h",
                    price=0.89,
                    action="SELL_STOP_FULL",
                    exit_source="take_profit",
                    rationale="relabelled stop",
                    force_local=True,
                    strategy_params=params,
                )
            # Label and source say stop, price is above the 10% stop (4% loss).
            with _gate(above, 1.0):
                above_out = try_execute_trail_exit(
                    symbol=above,
                    timeframe="1h",
                    price=0.96,
                    action="SELL_STOP_FULL",
                    exit_source="stop_loss",
                    rationale="above the stop",
                    force_local=True,
                    strategy_params=params,
                )

        assert relabeled_out.get("executed") is False
        assert above_out.get("executed") is False
        assert _held(relabeled) == pytest.approx(80.0, abs=0.05)
        assert _held(above) == pytest.approx(80.0, abs=0.05)
        assert _filled(relabeled) == []
        assert _filled(above) == []
        assert is_below_venue_minimum(get_position(relabeled, "1h"))
        assert is_below_venue_minimum(get_position(above, "1h"))


class TestManualFullStop:
    def test_manual_sell_does_not_get_the_full_stop_exception(self):
        """A manual SELL_STOP_FULL at a real full stop stays rejected.

        exit_source is stop_loss and the loss is past the configured stop,
        so those two gates are not what reject this order. The manual check is.
        """
        from core.models import TradeOrder
        from services.trading_service import TradingService
        from strategies.positions import BELOW_VENUE_MINIMUM, get_position, is_below_venue_minimum

        symbol = "K663M/USDT"
        _seed(
            symbol,
            amount=100,
            entry=1.0,
            venue_min_state=BELOW_VENUE_MINIMUM,
        )
        assert is_below_venue_minimum(get_position(symbol, "1h"))
        order = TradeOrder(
            type="SELL",
            symbol=symbol,
            price=0.49,
            amount=100,
            signal="SELL_STOP_FULL",
            source="manual",
            exit_source="stop_loss",
        )
        with _paper(), _gate(symbol, 1.0):
            result = TradingService().execute_order(
                order,
                "1h",
                source="manual",
                idempotency_key=uuid.uuid4().hex,
            )

        assert result.executed is False
        assert "below the venue minimum" in str(result.message or "")
        assert _held(symbol) == pytest.approx(100.0, abs=0.05)
        assert _filled(symbol) == []
        assert is_below_venue_minimum(get_position(symbol, "1h"))


class TestVenueRejectDustPath:
    def test_venue_reject_of_full_stop_keeps_the_below_min_lot(self):
        """The venue dust path runs. The lot is not closed."""
        from execution.gate_adapter import GateExecutionAdapter
        from strategies.positions import (
            BELOW_VENUE_MINIMUM,
            get_position,
            is_below_venue_minimum,
            mark_exchange_min_reject,
        )

        symbol = "K663D/USDT"
        params = _params(full=10, partial=5)
        lot = _seed(
            symbol,
            amount=40,
            entry=1.0,
            venue_min_state=BELOW_VENUE_MINIMUM,
        )
        hub = _hub(cooldown=0)
        hub.update_book([_row(symbol, params=params, position=lot)])
        calls = {"n": 0}
        real = mark_exchange_min_reject

        def _spy(sym, tf):
            calls["n"] += 1
            return real(sym, tf)

        with _paper(), patch(
            "strategies.positions.mark_exchange_min_reject", side_effect=_spy
        ):
            with _gate(symbol, 100.0):
                _fire(hub, symbol, "0.89", "0.89")

        pos = get_position(symbol, "1h")
        assert calls["n"] >= 1
        assert _held(symbol) == pytest.approx(40.0, abs=0.05)
        assert float(pos.get("sold_percent") or 0) == pytest.approx(0.0)
        assert is_below_venue_minimum(pos)
        assert pos.get("venue_min_state") == BELOW_VENUE_MINIMUM
        assert _filled(symbol) == []
        rejected = [o for o in _orders(symbol) if o.get("status") != "filled"]
        assert rejected
        assert rejected[-1]["signal"] == "SELL_STOP_FULL"
        blob = " ".join(
            str(rejected[-1].get(key) or "")
            for key in ("error", "message", "status")
        )
        risk = rejected[-1].get("risk") or {}
        blob = f"{blob} {risk.get('message') or ''} {risk.get('code') or ''}"
        assert "minimum" in blob.lower()
        assert GateExecutionAdapter is not None


class TestFullStopCooldown:
    def test_second_full_stop_in_cooldown_is_not_sent(self):
        """Partial reject, one full stop, no second send, then one more at expiry.

        The in-window repeat is stopped by ``_debounce_ok`` for
        ``ws_full_stop_during_below_min``. Removing that gate lets the
        repeat out. When ``live_cooldown_sec`` has elapsed exactly, the
        next full stop is sent again.
        """
        symbol = "K663T/USDT"
        params = _params(full=10, partial=5)
        cooldown = 60.0
        lot = _seed(symbol, amount=40, entry=1.0)
        hub = _hub(cooldown=cooldown)
        hub.update_book([_row(symbol, params=params, position=lot)])
        clock = {"now": 1_000_000.0}

        def _mono():
            return clock["now"]

        def _stop_orders():
            return [
                o
                for o in _orders(symbol)
                if str(o.get("signal") or "") in ("SELL_STOP_PARTIAL", "SELL_STOP_FULL")
            ]

        def _fulls():
            return [o for o in _stop_orders() if o.get("signal") == "SELL_STOP_FULL"]

        with _paper(), _logs() as lines, _gate(symbol, 100.0), patch(
            "services.exit_realtime.hub.time.monotonic", _mono
        ):
            _fire(hub, symbol, "0.94", "0.94")
            partials = [o for o in _stop_orders() if o.get("signal") == "SELL_STOP_PARTIAL"]
            assert len(partials) == 1
            assert partials[0].get("status") != "filled"
            assert _held(symbol) == pytest.approx(40.0, abs=0.05)

            _fire(hub, symbol, "0.89", "0.89")
            assert len(_fulls()) == 1
            assert _fulls()[0].get("status") != "filled"
            assert _held(symbol) == pytest.approx(40.0, abs=0.05)

            _fire(hub, symbol, "0.89", "0.89")
            assert len(_fulls()) == 1

            full_key = f"|{symbol}|ws_full_stop_during_below_min"
            stamped = hub._last_fire[full_key]
            clock["now"] = stamped + cooldown - 1e-6
            _fire(hub, symbol, "0.89", "0.89")
            assert len(_fulls()) == 1

            clock["now"] = stamped + cooldown
            _fire(hub, symbol, "0.89", "0.89")

        assert len(_fulls()) == 2
        assert _fulls()[-1].get("status") != "filled"
        full_fires = [
            line
            for line in lines
            if "exit_ws stop fire" in line and "SELL_STOP_FULL" in line
        ]
        assert len(full_fires) == 2
        assert _held(symbol) == pytest.approx(40.0, abs=0.05)
