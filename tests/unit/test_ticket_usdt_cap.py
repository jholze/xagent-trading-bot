"""max_usdt_per_trade is a hard ceiling after size multipliers."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager


class TestTicketUsdtCap(unittest.TestCase):
    def test_dynamic_size_boost_clamped_to_max_usdt_per_trade(self):
        raw = {
            "max_usdt_per_trade": 4500,
            "max_position_percent": 80,
            "max_open_positions": 50,
            "trading_mode": "paper",
            "paper": {"initial_capital_usdt": 100_000},
            "aggression": {"max_position_multiplier": 2.5},
            "risk": {
                "min_trade_usdt": 100,
                "min_size_multiplier": 0.25,
                "moderate_deploy": {
                    "enabled": True,
                    "size_boost_neutral": 2.0,
                    "size_boost_risk_on": 2.1,
                    "size_boost_risk_off": 1.25,
                    "max_total_multiplier": 2.6,
                    "max_boost": 2.5,
                    "cash_rich_pct": 50,
                    "cash_rich_extra_mult": 1.3,
                    "apply_to_dca": True,
                    "dca_boost_scale": 0.9,
                },
            },
            "architecture": {},
        }
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol="TAG/USDT",
            price=0.0013,
            amount=0,
            usdt_amount=0,
            signal="BUY",
        )
        bias = {
            "active": True,
            "apply_size_mult": True,
            "size_mult": 1.0,
            "regime": "NEUTRAL",
            "source": "test",
        }
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias,
        ), patch(
            "intelligence.memory.cache.get_size_bias", return_value=1.0
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch.object(
            risk, "_portfolio_equity", return_value=100_000.0
        ), patch.object(
            risk, "_available_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_spendable_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_daily_buys_count", return_value=0
        ), patch.object(
            risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}
        ), patch(
            "risk.risk_manager.count_open_full_slots", return_value=0
        ), patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 0, "sold_percent": 0},
        ), patch(
            "risk.risk_manager.load_trade_history",
            return_value={"virtual_balance": 80_000.0},
        ):
            decision = risk.evaluate(order, "1h", source="cmc")

        self.assertTrue(decision.approved, decision.message)
        self.assertLessEqual(decision.order.usdt_amount, 4500.0 + 1e-6)
        # Multipliers would otherwise push base 4500 above the cap
        self.assertGreaterEqual(float(decision.size_multiplier or 1.0), 1.0)

    def test_dca_boost_also_clamped(self):
        raw = {
            "max_usdt_per_trade": 4500,
            "max_position_percent": 80,
            "max_open_positions": 50,
            "trading_mode": "paper",
            "paper": {"initial_capital_usdt": 100_000},
            "risk": {
                "min_trade_usdt": 100,
                "moderate_deploy": {
                    "enabled": True,
                    "size_boost_risk_off": 1.25,
                    "max_boost": 2.5,
                    "max_total_multiplier": 2.6,
                    "apply_to_dca": True,
                    "dca_boost_scale": 0.9,
                    "cash_rich_pct": 10,
                    "cash_rich_extra_mult": 1.3,
                },
            },
        }
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol="TAG/USDT",
            price=0.0013,
            amount=0,
            usdt_amount=4000.0,
            signal="BUY_DCA",
        )
        bias = {
            "active": True,
            "apply_size_mult": True,
            "size_mult": 0.35,
            "regime": "RISK_OFF",
            "source": "test",
        }
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias,
        ), patch.object(
            risk, "_portfolio_equity", return_value=100_000.0
        ), patch.object(
            risk, "_available_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_spendable_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_daily_buys_count", return_value=0
        ), patch.object(
            risk, "_daily_dca_usdt_sum", return_value=0.0
        ), patch(
            "risk.risk_manager.count_open_full_slots", return_value=1
        ), patch(
            "risk.risk_manager.get_position",
            return_value={
                "amount": 1_000_000,
                "sold_percent": 0,
                "average_entry": 0.0013,
                "strategy_tier": "volatile",
            },
        ), patch(
            "risk.risk_manager.load_trade_history",
            return_value={"virtual_balance": 80_000.0},
        ):
            decision = risk.evaluate(order, "1h", source="dca")

        self.assertTrue(decision.approved, decision.message)
        self.assertLessEqual(decision.order.usdt_amount, 4500.0 + 1e-6)


if __name__ == "__main__":
    unittest.main()
