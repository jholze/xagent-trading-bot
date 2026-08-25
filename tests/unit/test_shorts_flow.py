"""P0 shorts: lot side + paper execute_order does not treat SHORT as SELL."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.models import TradeOrder
from services.portfolio_service import PortfolioService
from strategies.positions import (
    clear_positions_memory,
    get_position,
    update_position,
)
from strategies.short_math import is_short, unrealized_pnl


class TestShortsFlow(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def test_update_position_short_then_cover(self):
        update_position("AAA/USDT", "4h", "SHORT", 2.0, 10, leverage=2)
        pos = get_position("AAA/USDT", "4h")
        self.assertTrue(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 10)
        self.assertEqual(float(pos["leverage"]), 2)
        update_position("AAA/USDT", "4h", "COVER", 1.5, 10)
        pos = get_position("AAA/USDT", "4h")
        self.assertFalse(is_short(pos) and float(pos["amount"]) > 0)
        self.assertAlmostEqual(unrealized_pnl("short", 10, 2.0, 1.5), 5.0)

    def test_execute_order_unknown_type_not_sell(self):
        svc = PortfolioService()
        with patch.object(svc, "execute_sell") as sell:
            out = svc.execute_order(TradeOrder(type="NOPE", symbol="X/USDT", price=1, amount=1))
        self.assertFalse(out.executed)
        sell.assert_not_called()

    def test_one_way_short_on_long_rejected_by_risk(self):
        from risk.risk_manager import RiskManager

        update_position("BBB/USDT", "4h", "BUY", 1.0, 5)
        rm = RiskManager()
        with patch.object(rm, "_available_usdt", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="BBB/USDT", price=1.0, amount=0, usdt_amount=100),
                "4h",
                source="manual",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "one_way")

    def test_buy_and_sell_on_short_rejected_by_risk_and_portfolio(self):
        from risk.risk_manager import RiskManager

        update_position("CCC/USDT", "4h", "SHORT", 2.0, 10, leverage=2)
        rm = RiskManager()
        buy = rm.evaluate(
            TradeOrder(type="BUY", symbol="CCC/USDT", price=2.0, amount=0, usdt_amount=20),
            "4h",
            source="manual",
        )
        self.assertFalse(buy.approved)
        self.assertEqual(buy.code, "one_way")
        sell = rm.evaluate(
            TradeOrder(type="SELL", symbol="CCC/USDT", price=2.0, amount=10, signal="SELL_FULL"),
            "4h",
            source="manual",
        )
        self.assertFalse(sell.approved)
        self.assertEqual(sell.code, "one_way")
        svc = PortfolioService()
        out_buy = svc.execute_buy("CCC/USDT", "4h", 2.0, usdt_amount=20)
        self.assertFalse(out_buy.executed)
        out_sell = svc.execute_sell("CCC/USDT", "4h", 2.0, "SELL_FULL", 10)
        self.assertFalse(out_sell.executed)
        pos = get_position("CCC/USDT", "4h")
        self.assertTrue(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 10)

    def test_update_position_refuses_buy_sell_on_short(self):
        update_position("DDD/USDT", "4h", "SHORT", 1.0, 5, leverage=2)
        update_position("DDD/USDT", "4h", "BUY", 1.0, 5)
        pos = get_position("DDD/USDT", "4h")
        self.assertTrue(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 5)
        update_position("DDD/USDT", "4h", "SELL_FULL", 1.2, 5)
        pos = get_position("DDD/USDT", "4h")
        self.assertTrue(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 5)

    def test_cover_allowed_when_shorts_disabled(self):
        from risk.risk_manager import RiskManager

        update_position("EEE/USDT", "4h", "SHORT", 1.0, 8, leverage=2)
        rm = RiskManager()
        disabled = {"shorts": {"enabled": False, "allow_live": False}}
        with patch.object(rm.config, "_raw", disabled), patch(
            "core.simulated_trading.is_real_live_trading", return_value=False
        ):
            dec = rm.evaluate(
                TradeOrder(type="COVER", symbol="EEE/USDT", price=0.9, amount=8),
                "4h",
                source="manual",
            )
            short_dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="FFF/USDT", price=1.0, amount=0, usdt_amount=50),
                "4h",
                source="manual",
            )
        self.assertTrue(dec.approved)
        self.assertFalse(short_dec.approved)
        self.assertEqual(short_dec.code, "shorts_disabled")

    def test_auto_short_uses_fraction_not_full_sell(self):
        from strategies.short_policy import auto_short_notional_usdt

        cfg = {"shorts": {"auto_notional_pct": 0.35}}
        self.assertAlmostEqual(auto_short_notional_usdt(1000, cap=400, config_raw=cfg), 350)
        self.assertAlmostEqual(auto_short_notional_usdt(2000, cap=400, config_raw=cfg), 400)

    def test_auto_short_does_not_call_execute_order(self):
        from services.trading_service import TradingService

        svc = TradingService()
        order = TradeOrder(
            type="SELL",
            symbol="GGG/USDT",
            price=1.0,
            amount=10,
            usdt_amount=10,
            exit_source="rsi_sell",
        )
        result = type("R", (), {"price": 1.0, "usdt_amount": 1000.0, "executed": True})()
        with patch.object(svc, "execute_order") as nested, patch.object(
            svc, "_execute_order_locked"
        ) as locked, patch(
            "strategies.short_policy.shorts_enabled", return_value=True
        ), patch(
            "strategies.short_policy.is_auto_short_source", return_value=True
        ), patch(
            "strategies.positions.is_open_position", return_value=False
        ), patch.object(svc, "max_usdt_for_order", return_value=400):
            svc._maybe_auto_short_after_sell(order, "4h", result)
        nested.assert_not_called()
        locked.assert_called_once()
        short_order = locked.call_args[0][0]
        self.assertEqual(short_order.type, "SHORT")
        self.assertLess(short_order.usdt_amount, 1000)
        self.assertAlmostEqual(short_order.usdt_amount, 350)

    def test_risk_rejects_low_mcap_auto_short(self):
        from risk.risk_manager import RiskManager

        rm = RiskManager()
        raw = {
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 5,
                "max_open": 6,
                "max_margin_pct": 80,
                "volatile": {"market_cap_min_usd": 50_000_000},
            }
        }
        with patch.object(rm.config, "_raw", raw), patch(
            "core.simulated_trading.is_real_live_trading", return_value=False
        ), patch(
            "data.cmc_market_cap.resolve_market_cap_usd", return_value=1_000_000
        ), patch.object(rm, "_available_usdt", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="TINY/USDT", price=1.0, amount=0, usdt_amount=100),
                "4h",
                source="auto",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "short_mcap")

    def test_risk_rejects_margin_pct_cap(self):
        from risk.risk_manager import RiskManager

        rm = RiskManager()
        raw = {
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 5,
                "max_open": 6,
                "max_margin_pct": 5,
                "volatile": {"market_cap_min_usd": 0},
            }
        }
        with patch.object(rm.config, "_raw", raw), patch(
            "core.simulated_trading.is_real_live_trading", return_value=False
        ), patch(
            "data.cmc_market_cap.resolve_market_cap_usd", return_value=1e9
        ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
            rm, "_portfolio_equity", return_value=1_000
        ):
            dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="BIG/USDT", price=1.0, amount=0, usdt_amount=400),
                "4h",
                source="manual",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "short_margin_pct")

    def test_sell_repair_from_other_tf_short_is_one_way(self):
        from risk.risk_manager import RiskManager

        update_position("HOP/USDT", "1h", "SHORT", 1.0, 20, leverage=2)
        rm = RiskManager()
        with patch.object(rm, "_available_usdt", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(type="SELL", symbol="HOP/USDT", price=1.0, amount=0, signal="SELL_FULL"),
                "4h",
                source="auto",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "one_way")

    def test_execute_short_on_long_rejected(self):
        update_position("LNG/USDT", "4h", "BUY", 1.0, 5)
        svc = PortfolioService()
        out = svc.execute_short("LNG/USDT", "4h", 1.0, usdt_amount=20, leverage=2)
        self.assertFalse(out.executed)
        pos = get_position("LNG/USDT", "4h")
        self.assertFalse(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 5)

    def test_update_position_refuses_short_on_long(self):
        update_position("L2/USDT", "4h", "BUY", 1.0, 8)
        update_position("L2/USDT", "4h", "SHORT", 1.0, 8, leverage=2)
        pos = get_position("L2/USDT", "4h")
        self.assertFalse(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 8)

    def test_unknown_nav_rejects_short(self):
        from risk.risk_manager import RiskManager

        rm = RiskManager()
        raw = {
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 5,
                "max_open": 6,
                "max_margin_pct": 20,
                "volatile": {"market_cap_min_usd": 0},
            }
        }
        with patch.object(rm.config, "_raw", raw), patch(
            "core.simulated_trading.is_real_live_trading", return_value=False
        ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
            rm, "_portfolio_equity", return_value=0
        ):
            dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="NAV/USDT", price=1.0, amount=0, usdt_amount=100),
                "4h",
                source="manual",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "short_margin_pct")

    def test_auto_mcap_unknown_rejected(self):
        from risk.risk_manager import RiskManager

        rm = RiskManager()
        raw = {
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 5,
                "max_open": 6,
                "max_margin_pct": 80,
                "volatile": {"market_cap_min_usd": 50_000_000},
            }
        }
        with patch.object(rm.config, "_raw", raw), patch(
            "core.simulated_trading.is_real_live_trading", return_value=False
        ), patch(
            "data.cmc_market_cap.resolve_market_cap_usd", return_value=None
        ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
            rm, "_portfolio_equity", return_value=10_000
        ):
            dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="UNK/USDT", price=1.0, amount=0, usdt_amount=100),
                "4h",
                source="auto",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "short_mcap")
