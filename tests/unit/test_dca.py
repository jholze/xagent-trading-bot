import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import BUY_DCA
from core.models import MarketContext, TradeOrder
from risk.risk_manager import RiskManager
from strategies.dca import dca_enabled, evaluate_dca_addon
from strategies.decision_engine import DecisionEngine
from strategies.positions import get_key, get_position, positions, update_position


class TestDCAModule(unittest.TestCase):
    def setUp(self):
        self.symbol = "DCA/USDT"
        self.tf = "1h"
        self.key = get_key(self.symbol, self.tf)
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        self.params = {
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "stop_loss_pct": 18,
            "dca": {
                "enabled": True,
                "mode": "live",
                "interval_hours": 12,
                "fixed_usdt": 20,
                "loss_pct_min": -20,
                "loss_pct_max": -3,
                "sl_proximity_pct": 15,
                "max_rounds": 3,
            },
        }

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def _market(self, entry: float, price: float) -> MarketContext:
        return MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=price,
            has_position=True,
            average_entry=entry,
            open_positions=1,
            strategy_params=self.params,
        )

    def test_dca_enabled(self):
        self.assertTrue(dca_enabled(self.params))
        self.assertFalse(dca_enabled({"dca": {"enabled": False}}))

    def test_dca_triggers_in_dip_window(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0

        cand = evaluate_dca_addon(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, BUY_DCA)
        self.assertAlmostEqual(cand.usdt_amount, 20.0)

    def test_dca_blocked_after_ladder_started(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["exit_ladder_step"] = 1
        pos["sold_percent"] = 0.3

        cand = evaluate_dca_addon(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNone(cand)

    def test_dca_blocked_when_gain(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        cand = evaluate_dca_addon(self._market(1.0, 1.05), pos, self.params)
        self.assertIsNone(cand)

    def test_dca_blocked_within_interval(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["last_dca_at"] = datetime.now().isoformat()
        pos["dca_rounds"] = 1

        cand = evaluate_dca_addon(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNone(cand)

    def test_dca_blocked_at_max_rounds(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["dca_rounds"] = 3

        cand = evaluate_dca_addon(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNone(cand)

    def test_buy_dca_preserves_ladder_state(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = get_position(self.symbol, self.tf)
        pos["peak_amount"] = 1000.0
        pos["exit_ladder_step"] = 0

        update_position(self.symbol, self.tf, "BUY_DCA", 0.9, 22.22)
        pos = get_position(self.symbol, self.tf)

        self.assertAlmostEqual(float(pos["peak_amount"]), 1000.0)
        self.assertEqual(pos["exit_ladder_step"], 0)
        self.assertEqual(pos["sold_percent"], 0.0)
        self.assertEqual(pos["dca_rounds"], 1)
        self.assertIsNotNone(pos["last_dca_at"])
        self.assertGreater(float(pos["amount"]), 1000)


class TestDCADecisionEngine(unittest.TestCase):
    def setUp(self):
        self.symbol = "DCAE/USDT"
        self.tf = "1h"
        self.key = get_key(self.symbol, self.tf)
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_shadow_dca_emits_hold_with_shadow(self):
        market = MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=0.92,
            rsi=45.0,
            lower_bb=0.9,
            vol_multiplier=1.0,
            has_position=True,
            average_entry=1.0,
            open_positions=1,
            strategy_params={
                "strategy_profile": "volatile_altcoin",
                "volatility_tier": "volatile",
                "stop_loss_pct": 18,
                "dca": {
                    "enabled": True,
                    "mode": "shadow",
                    "interval_hours": 12,
                    "fixed_usdt": 20,
                    "loss_pct_min": -20,
                    "loss_pct_max": -3,
                    "sl_proximity_pct": 15,
                    "max_rounds": 3,
                },
            },
        )
        engine = DecisionEngine()
        with patch.object(engine, "_merge_sell", return_value=("HOLD", ["technical"], 50.0, [])):
            analysis = engine.evaluate_with_market(
                {"symbol": self.symbol, "timeframe": self.tf},
                market,
            )
        self.assertEqual(analysis.action, "HOLD")
        self.assertEqual(analysis.shadow_action, "BUY_DCA")
        self.assertIn("dca", analysis.sources)


class TestDCARisk(unittest.TestCase):
    def setUp(self):
        self.symbol = "DCAR/USDT"
        self.tf = "1h"
        self.key = get_key(self.symbol, self.tf)
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_dca_bypasses_rebuy_cooldown_with_open_position(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        update_position(self.symbol, self.tf, "SELL_FULL", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["amount"] = Decimal("50")
        pos["last_trade_at"] = datetime.now().isoformat()
        pos["last_trade_type"] = "SELL"

        from core.config import BotConfig
        from data_manager import get_config

        raw = dict(get_config())
        raw.setdefault("architecture", {})["min_hours_after_sell_before_rebuy"] = 4.0
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol=self.symbol,
            price=1.0,
            amount=0,
            usdt_amount=20,
            signal="BUY_DCA",
            source="dca",
        )
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), \
             patch.object(risk, "_portfolio_equity", return_value=5000.0), \
             patch.object(risk, "_daily_buys_count", return_value=0), \
             patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
            decision = risk.evaluate(order, self.tf, source="dca")

        self.assertTrue(decision.approved)

    def test_dca_interval_blocks_rapid_addon(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["last_dca_at"] = datetime.now().isoformat()
        pos["dca_rounds"] = 1

        from core.config import BotConfig
        from data_manager import get_config

        raw = dict(get_config())
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol=self.symbol,
            price=1.0,
            amount=0,
            usdt_amount=20,
            signal="BUY_DCA",
            source="dca",
        )
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), \
             patch.object(risk, "_portfolio_equity", return_value=5000.0), \
             patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
            decision = risk.evaluate(order, self.tf, source="dca")

        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "trade_cooldown")
        self.assertIn("DCA interval", decision.message)


if __name__ == "__main__":
    unittest.main()