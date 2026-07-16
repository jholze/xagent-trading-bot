"""Cache-only position keys must not inflate demo NAV when orders replay cash."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.portfolio_baseline import initial_capital, reconcile_display_nav
from notifications.telegram_commands.position_display import (
    aggregate_open_coins_totals,
    position_symbol,
)
from strategies.positions import derive_positions_from_orders_and_cache, is_open_position


class TestPortfolioCacheGhosts(unittest.TestCase):
    def test_ghost_cache_lots_do_not_inflate_nav_under_order_ledger(self):
        order_snap = {
            "ADA_USDT_1h": {
                "amount": 100.0,
                "peak_amount": 100.0,
                "average_entry": 1.0,
            },
        }
        cache_doc = {
            "positions": {
                "ADA_USDT_1h": {"recent_high": 1.1},
                "SPCX_USDT_1h": {
                    "amount": 26.0,
                    "peak_amount": 26.0,
                    "average_entry": 116.8,
                },
            },
        }
        with patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), patch(
            "data_manager.get_config", return_value={"live": {"dry_run": True}}
        ):
            merged = derive_positions_from_orders_and_cache(order_snap, cache_doc)

        self.assertNotIn("SPCX_USDT_1h", merged)
        active = []
        for key, raw in merged.items():
            if is_open_position(raw):
                base, _, tf = key.rpartition("_")
                active.append(
                    {
                        "symbol": base.replace("_", "/"),
                        "timeframe": tf,
                        "amount": float(raw.get("amount", 0)),
                        "average_entry": float(raw.get("average_entry", 0) or 0),
                    }
                )
        prices = {position_symbol(active[0]): 1.05}
        totals = aggregate_open_coins_totals(active, prices)
        initial = 100_000.0
        cash = 100_000.0 - 100.0
        nav = reconcile_display_nav(
            cash, initial, totals["marktwert"], 0.0, totals["unreal"]
        )
        self.assertAlmostEqual(nav["total_value"], cash + totals["marktwert"], places=0)
        self.assertLess(nav["total_value"] - initial, 500.0)


if __name__ == "__main__":
    unittest.main()