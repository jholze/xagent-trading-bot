"""#507 Phase 0: SELL_FULL dust must not reject auto-short with one_way."""

from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from core.models import TradeOrder
from strategies.positions import (
    apply_hard_clear_if_closed,
    clear_positions_memory,
    get_position,
    hard_clear_closed_lot,
    is_open_position,
    set_position_field,
    update_position,
)


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_SHORTS_PAPER = {
    "shorts": {
        "enabled": True,
        "allow_live": False,
        "leverage_default": 2,
        "leverage_cap": 5,
        "max_open": 6,
        "max_margin_pct": 80,
        "auto_notional_pct": 0.35,
        "auto_sources": [
            "rsi_sell",
            "exit_1h_rsi_rollover",
            "oracle_climax_harvest",
            "exit_volume_climax",
        ],
        "volatile": {"market_cap_min_usd": 0},
    }
}


def _eval_short(symbol: str, *, source: str = "auto"):
    from risk.risk_manager import RiskManager

    rm = RiskManager()
    with patch.object(rm.config, "_raw", _SHORTS_PAPER), patch(
        "core.simulated_trading.is_real_live_trading", return_value=False
    ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
        rm, "_portfolio_equity", return_value=10_000
    ):
        return rm.evaluate(
            TradeOrder(
                type="SHORT",
                symbol=symbol,
                price=1.0,
                amount=0,
                usdt_amount=100,
                signal="SHORT",
                source=source,
                exit_source="rsi_sell",
            ),
            "4h",
            source=source,
        )


class TestShortsDustOneWay507(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def test_dust_remainder_does_not_reject_short_one_way(self):
        update_position("B2/USDT", "4h", "BUY", 1.0, 10)
        set_position_field("B2/USDT", "4h", "amount", Decimal("1e-8"))
        pos = get_position("B2/USDT", "4h")
        self.assertGreater(float(pos["amount"]), 1e-12)
        self.assertFalse(is_open_position(pos))

        dec = _eval_short("B2/USDT")
        self.assertNotEqual(dec.code, "one_way")
        self.assertTrue(dec.approved, dec.message)

    def test_material_long_still_rejects_short_one_way(self):
        update_position("MAT/USDT", "4h", "BUY", 1.0, 1.0)
        pos = get_position("MAT/USDT", "4h")
        self.assertTrue(is_open_position(pos))
        self.assertGreaterEqual(float(pos["amount"]) * float(pos["average_entry"]), 1.0)

        dec = _eval_short("MAT/USDT")
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "one_way")

    def test_sell_full_hard_clears_dust_remainder(self):
        update_position("B2/USDT", "4h", "BUY", 1.0, 10)
        update_position("B2/USDT", "4h", "SELL_FULL", 1.0, 9.999999)
        pos = get_position("B2/USDT", "4h")
        self.assertEqual(float(pos["amount"]), 0.0)
        self.assertEqual(float(pos["sold_percent"]), 1.0)
        self.assertFalse(is_open_position(pos))

    def test_hard_clear_leaves_material_long(self):
        update_position("KEEP/USDT", "4h", "BUY", 1.0, 5)
        self.assertFalse(hard_clear_closed_lot("KEEP/USDT", "4h"))
        pos = get_position("KEEP/USDT", "4h")
        self.assertAlmostEqual(float(pos["amount"]), 5.0)
        self.assertTrue(is_open_position(pos))

    def test_apply_hard_clear_if_closed_on_dust_dict(self):
        pos = {"amount": Decimal("1e-8"), "average_entry": 1.0, "sold_percent": 0.99}
        self.assertTrue(apply_hard_clear_if_closed(pos))
        self.assertEqual(float(pos["amount"]), 0.0)
        self.assertEqual(float(pos["sold_percent"]), 1.0)

    def test_b2_rsi_sell_full_then_auto_short_not_one_way(self):
        from services.trading_service import TradingService

        update_position("B2/USDT", "4h", "BUY", 1.0, 10)
        update_position("B2/USDT", "4h", "SELL_FULL", 1.0, 9.999999)
        pos = get_position("B2/USDT", "4h")
        self.assertEqual(float(pos["amount"]), 0.0)
        self.assertEqual(float(pos["sold_percent"]), 1.0)

        svc = TradingService()
        order = TradeOrder(
            type="SELL",
            symbol="B2/USDT",
            price=1.0,
            amount=10,
            usdt_amount=10,
            signal="SELL_FULL",
            exit_source="rsi_sell",
        )
        result = type("R", (), {"price": 1.0, "usdt_amount": 10.0, "executed": True})()
        with patch.object(svc, "execute_order") as nested, patch.object(
            svc, "_execute_order_locked"
        ) as locked, patch.object(svc.config, "_raw", _SHORTS_PAPER), patch.object(
            svc, "max_usdt_for_order", return_value=400
        ):
            svc._maybe_auto_short_after_sell(order, "4h", result)
        nested.assert_not_called()
        locked.assert_called_once()
        self.assertTrue(locked.call_args.kwargs.get("_lock_held"))
        short_order = locked.call_args[0][0]
        self.assertEqual(short_order.type, "SHORT")
        self.assertEqual(short_order.symbol, "B2/USDT")
        self.assertEqual(short_order.exit_source, "rsi_sell")

        dec = _eval_short("B2/USDT")
        self.assertNotEqual(dec.code, "one_way")
        self.assertTrue(dec.approved, dec.message)

    def test_shorts_allow_live_stays_false(self):
        with open(os.path.join(_REPO_ROOT, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertFalse(cfg["shorts"]["allow_live"])
        self.assertTrue(cfg["shorts"]["enabled"])


if __name__ == "__main__":
    unittest.main()
