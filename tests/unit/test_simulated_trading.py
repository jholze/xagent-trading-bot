"""Simulated live: one ledger/cash path for staging and dry-run."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.simulated_trading import (
    is_simulated_trading,
    uses_order_ledger_cash,
    uses_simulated_portfolio,
)
from data_manager import is_dry_run_enhanced
from core.sim_ledger_replay import replay_simulated_ledger
from data_manager import (
    compute_sim_cash_from_orders,
    resolve_ledger_scope,
    resolve_sim_cash_balance,
    uses_simulated_live_portfolio,
)
from services.ledger_sync import _build_positions_snapshot_from_orders


class TestSimulatedTrading(unittest.TestCase):
    def test_demo_mode_is_simulated(self):
        cfg = {"trading_mode": "live", "live": {"dry_run": True}}
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False):
            self.assertTrue(is_simulated_trading(cfg))
            self.assertTrue(uses_simulated_portfolio(cfg))
            self.assertTrue(uses_order_ledger_cash(cfg))
            self.assertEqual(resolve_ledger_scope(), "demo")

    def test_dry_run_enhanced_uses_trade_history_not_orders(self):
        cfg = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": True},
        }
        with patch.dict(os.environ, {"DEMO_MODE": "0"}, clear=False):
            self.assertTrue(is_simulated_trading(cfg))
            self.assertTrue(is_dry_run_enhanced(cfg))
            self.assertFalse(uses_order_ledger_cash(cfg))

    def test_live_dry_run_is_simulated(self):
        cfg = {"trading_mode": "live", "live": {"dry_run": True}}
        with patch.dict(os.environ, {"DEMO_MODE": "0"}, clear=False):
            self.assertTrue(is_simulated_trading(cfg))
            self.assertTrue(uses_simulated_live_portfolio(cfg))

    def test_real_live_is_not_simulated(self):
        cfg = {"trading_mode": "live", "live": {"dry_run": False}}
        with patch.dict(os.environ, {"DEMO_MODE": "0"}, clear=False):
            self.assertFalse(is_simulated_trading(cfg))
            self.assertFalse(uses_order_ledger_cash(cfg))

    def test_resolve_sim_cash_from_orders_for_demo_scope(self):
        cfg = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": True, "simulated_balance_usdt": 100_000},
        }
        orders = {
            "orders": [
                {
                    "id": "1",
                    "status": "filled",
                    "side": "buy",
                    "execution": {"price": 1.0, "amount": 100.0},
                    "timestamps": {"filled": "2026-07-15T10:00:00"},
                }
            ]
        }
        with patch("data_manager.load_orders", return_value=orders), \
             patch("data_manager.get_config", return_value=cfg):
            cash = resolve_sim_cash_balance(scope="demo", config=cfg)
        self.assertAlmostEqual(cash, 99_900.0, places=2)

    def test_compute_sim_cash_skips_overdraw_buys(self):
        orders = [
            {
                "id": "buy-ok",
                "status": "filled",
                "side": "buy",
                "symbol": "AAA/USDT",
                "timeframe": "4h",
                "execution": {"price": 1.0, "amount": 100.0},
                "timestamps": {"filled": "2026-07-15T10:00:00"},
            },
            {
                "id": "buy-phantom",
                "status": "filled",
                "side": "buy",
                "symbol": "BBB/USDT",
                "timeframe": "4h",
                "execution": {"price": 1.0, "amount": 500.0},
                "timestamps": {"filled": "2026-07-15T10:01:00"},
            },
            {
                "id": "sell-1",
                "status": "filled",
                "side": "sell",
                "symbol": "AAA/USDT",
                "timeframe": "4h",
                "execution": {"price": 2.0, "amount": 50.0},
                "timestamps": {"filled": "2026-07-15T10:02:00"},
            },
        ]
        cash = compute_sim_cash_from_orders(orders, initial=200.0)
        # 200 - 100 = 100; phantom 500 buy skipped; sell +100 → 200
        self.assertAlmostEqual(cash, 200.0, places=2)

    def test_overdraw_conserves_cash_positions_and_nav(self):
        """Phantom fills must not open lots or mint cash on orphan sells."""
        orders = [
            {
                "id": "buy-ok",
                "status": "filled",
                "side": "buy",
                "symbol": "AAA/USDT",
                "timeframe": "4h",
                "execution": {"price": 1.0, "amount": 100.0},
                "timestamps": {"filled": "2026-07-15T10:00:00"},
            },
            {
                "id": "buy-phantom",
                "status": "filled",
                "side": "buy",
                "symbol": "BBB/USDT",
                "timeframe": "4h",
                "execution": {"price": 1.0, "amount": 500.0},
                "timestamps": {"filled": "2026-07-15T10:01:00"},
            },
            {
                "id": "sell-phantom",
                "status": "filled",
                "side": "sell",
                "symbol": "BBB/USDT",
                "timeframe": "4h",
                "execution": {"price": 2.0, "amount": 500.0},
                "timestamps": {"filled": "2026-07-15T10:02:00"},
            },
        ]
        initial = 200.0
        replay = replay_simulated_ledger(orders, initial)
        self.assertAlmostEqual(replay["cash"], 100.0, places=2)
        self.assertAlmostEqual(replay["positions"]["AAA_USDT_4h"]["amount"], 100.0, places=4)
        bbb = replay["positions"].get("BBB_USDT_4h", {}).get("amount", 0)
        self.assertAlmostEqual(bbb, 0.0, places=4)

        cost_basis = sum(
            float(p.get("amount") or 0) * float(p.get("average_entry") or 0)
            for p in replay["positions"].values()
            if float(p.get("amount") or 0) > 0
        )
        self.assertAlmostEqual(replay["cash"] + cost_basis, initial, places=2)

        with patch("data_manager.load_orders", return_value={"orders": orders}), \
             patch("data_manager.get_config", return_value={
                 "trading_mode": "live",
                 "live": {"dry_run": True, "simulated_balance_usdt": initial},
             }), \
             patch("core.portfolio_baseline.initial_capital", return_value=initial):
            snap = _build_positions_snapshot_from_orders("demo")
        self.assertAlmostEqual(snap["AAA_USDT_4h"]["amount"], 100.0, places=4)
        self.assertNotIn("BBB_USDT_4h", snap)


if __name__ == "__main__":
    unittest.main()