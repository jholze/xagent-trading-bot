import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import get_bot_config
from core.models import MarketContext, TradeOrder
from risk.risk_manager import RiskManager
from strategies.positions import get_key, get_position, positions, update_position
from strategies.technical_rsi_bb import TechnicalRSIStrategy


class TestTradeCooldown(unittest.TestCase):
    def setUp(self):
        self.symbol = "TEST/USDT"
        self.tf = "4h"
        self.key = get_key(self.symbol, self.tf)
        self._positions_backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        if self.key in positions:
            del positions[self.key]

    def tearDown(self):
        positions.clear()
        positions.update(self._positions_backup)

    def test_buy_blocked_within_cooldown(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["last_trade_at"] = datetime.now().isoformat()
        pos["last_trade_type"] = "BUY"

        risk = RiskManager()
        order = TradeOrder(type="BUY", symbol=self.symbol, price=1.0, amount=0, usdt_amount=25)
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}):
            with patch.object(risk, "_portfolio_equity", return_value=5000.0):
                with patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
                    decision = risk.evaluate(order, self.tf)

        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "trade_cooldown")

    def test_stop_loss_sell_bypasses_cooldown(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["last_trade_at"] = datetime.now().isoformat()
        pos["last_trade_type"] = "SELL"

        risk = RiskManager()
        order = TradeOrder(
            type="SELL",
            symbol=self.symbol,
            price=0.8,
            amount=50,
            signal="SELL_STOP_FULL",
        )
        decision = risk.evaluate(order, self.tf)
        self.assertTrue(decision.approved)

    def test_manual_buy_bypasses_cooldown(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["last_trade_at"] = datetime.now().isoformat()
        pos["last_trade_type"] = "BUY"

        risk = RiskManager()
        order = TradeOrder(type="BUY", symbol=self.symbol, price=1.0, amount=0, usdt_amount=200)
        with patch.object(risk, "_portfolio_equity", return_value=5000.0):
            with patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
                decision = risk.evaluate(order, self.tf, source="manual")

        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.order.usdt_amount, 200.0, places=2)

    def test_buy_allowed_after_cooldown_expires(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["last_trade_at"] = (datetime.now() - timedelta(hours=5)).isoformat()
        pos["last_trade_type"] = "BUY"

        risk = RiskManager()
        order = TradeOrder(type="BUY", symbol=self.symbol, price=1.0, amount=0, usdt_amount=25)
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}):
            with patch.object(risk, "_portfolio_equity", return_value=5000.0):
                with patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
                    decision = risk.evaluate(order, self.tf)

        self.assertTrue(decision.approved)


class TestRSIChurnPrevention(unittest.TestCase):
    def setUp(self):
        self.symbol = "TEST/USDT"
        self.tf = "4h"
        self.key = get_key(self.symbol, self.tf)
        self._positions_backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        if self.key in positions:
            del positions[self.key]
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0

    def tearDown(self):
        positions.clear()
        positions.update(self._positions_backup)

    def _analyze(self, rsi: float, last_rsi: float):
        pos = get_position(self.symbol, self.tf)
        pos["last_rsi"] = last_rsi
        strategy = TechnicalRSIStrategy()
        market = MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=1.0,
            rsi=rsi,
            lower_bb=1.05,
            vol_multiplier=1.5,
            has_position=True,
            average_entry=1.0,
            open_positions=1,
            strategy_params={
                "rsi_sell_30": 72,
                "rsi_sell_20": 84,
                "take_profit_pct": 12,
            },
        )
        return strategy.analyze({"symbol": self.symbol, "timeframe": self.tf}, market)

    def test_rsi_high_without_cross_stays_hold(self):
        analysis = self._analyze(rsi=75.0, last_rsi=74.0)
        self.assertEqual(analysis.action, "HOLD")

    def test_rsi_cross_triggers_sell_once(self):
        first = self._analyze(rsi=73.0, last_rsi=71.0)
        self.assertEqual(first.normalized_action, "SELL_PARTIAL_30")

        update_position(self.symbol, self.tf, "SELL_30", 1.0, 30)

        second = self._analyze(rsi=75.0, last_rsi=73.0)
        self.assertEqual(second.action, "HOLD")

    def test_take_profit_triggers_once(self):
        strategy = TechnicalRSIStrategy()
        market = MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=1.13,
            rsi=55.0,
            lower_bb=1.05,
            vol_multiplier=1.5,
            has_position=True,
            average_entry=1.0,
            open_positions=1,
            strategy_params={"take_profit_pct": 12, "rsi_sell_30": 72, "rsi_sell_20": 84},
        )
        pos = get_position(self.symbol, self.tf)
        pos["last_rsi"] = 54.0
        analysis = strategy.analyze({"symbol": self.symbol, "timeframe": self.tf}, market)
        self.assertEqual(analysis.normalized_action, "SELL_PARTIAL_30")
        self.assertIn("take_profit", analysis.sources)


if __name__ == "__main__":
    unittest.main()