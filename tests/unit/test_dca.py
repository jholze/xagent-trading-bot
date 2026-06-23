import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import BUY_DCA
from core.models import MarketContext, TradeOrder
from risk.risk_manager import RiskManager
from services.market_service import MarketService
from strategies.dca import dca_enabled, evaluate_dca_addon, should_dca
from strategies.decision_engine import DecisionEngine
from strategies.positions import get_key, get_position, positions, update_position


def _scoring_dca_cfg(mode: str = "live") -> dict:
    return {
        "enabled": True,
        "mode": mode,
        "interval_hours": 12,
        "fixed_usdt": 20,
        "loss_pct_min": -20,
        "loss_pct_max": -3,
        "sl_proximity_pct": 15,
        "max_rounds": 3,
        "scoring": {
            "enabled": True,
            "min_score": 6,
            "min_core_criteria_met": 3,
            "max_score": 10,
            "btc_lookback_hours": 8,
            "volatile": {
                "atr_mult_high": 1.8,
                "atr_mult_low": 1.2,
                "funding_max_pct": -0.035,
                "rsi_soft": 35,
                "rsi_hard": 30,
                "btc_underperf_high": 1.5,
                "btc_underperf_low": 1.2,
                "bb_support_enabled": True,
                "bb_support_ratio": 1.02,
            },
            "stable": {
                "atr_mult_high": 2.5,
                "atr_mult_low": 1.8,
                "funding_max_pct": -0.06,
                "rsi_soft": 35,
                "rsi_hard": 30,
                "btc_underperf_high": 2.0,
                "btc_underperf_low": 1.5,
                "bb_support_enabled": True,
                "bb_support_ratio": 1.02,
            },
        },
    }


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
            "dca": _scoring_dca_cfg(),
        }

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def _market(
        self,
        entry: float,
        price: float,
        *,
        rsi: float = 28.0,
        atr_pct: float = 3.0,
        funding_rate_pct: float | None = -0.04,
        btc_underperf_ratio: float | None = 2.0,
        lower_bb: float = 0.91,
    ) -> MarketContext:
        return MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=price,
            rsi=rsi,
            lower_bb=lower_bb,
            atr_pct=atr_pct,
            funding_rate_pct=funding_rate_pct,
            btc_underperf_ratio=btc_underperf_ratio,
            has_position=True,
            average_entry=entry,
            open_positions=1,
            strategy_params=self.params,
        )

    def test_dca_enabled(self):
        self.assertTrue(dca_enabled(self.params))
        self.assertFalse(dca_enabled({"dca": {"enabled": False}}))

    def test_dca_triggers_with_scoring(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0

        cand = evaluate_dca_addon(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, BUY_DCA)
        self.assertAlmostEqual(cand.usdt_amount, 20.0)
        self.assertGreaterEqual(cand.score, 6)

    def test_scoring_blocks_weak_signal(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0

        weak = self._market(
            1.0,
            0.97,
            rsi=50.0,
            atr_pct=3.0,
            funding_rate_pct=0.01,
            btc_underperf_ratio=None,
            lower_bb=0.8,
        )
        decision = should_dca(weak, pos, self.params)
        self.assertFalse(decision.should_dca)
        self.assertIn("score", decision.blocked_reason or "")

    def test_scoring_disabled_uses_legacy_dip(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 100)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0
        params = dict(self.params)
        params["dca"] = _scoring_dca_cfg()
        params["dca"]["scoring"]["enabled"] = False

        market = self._market(
            1.0,
            0.92,
            rsi=60.0,
            funding_rate_pct=None,
            btc_underperf_ratio=None,
        )
        cand = evaluate_dca_addon(market, pos, params)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.score, 0)

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


class TestDCAMarketService(unittest.TestCase):
    def test_btc_underperformance_ratio(self):
        svc = MarketService()
        coin_df = pd.DataFrame(
            {
                "close": [100, 100, 100, 100, 95, 90],
                "high": [100] * 6,
                "low": [90] * 6,
                "volume": [1] * 6,
            }
        )
        btc_df = pd.DataFrame(
            {
                "close": [100, 100, 100, 100, 99, 98],
                "high": [100] * 6,
                "low": [98] * 6,
                "volume": [1] * 6,
            }
        )
        with patch.object(svc, "_fetch_ohlcv", side_effect=[coin_df, btc_df]):
            ratio = svc.btc_underperformance_ratio("DOGE/USDT", "1h", lookback_hours=4)
        self.assertAlmostEqual(ratio, 5.0)

    def test_btc_underperformance_none_when_coin_outperforms(self):
        svc = MarketService()
        coin_df = pd.DataFrame({"close": [100, 101, 102], "high": [100] * 3, "low": [99] * 3, "volume": [1] * 3})
        btc_df = pd.DataFrame({"close": [100, 99, 98], "high": [100] * 3, "low": [97] * 3, "volume": [1] * 3})
        with patch.object(svc, "_fetch_ohlcv", side_effect=[coin_df, btc_df]):
            ratio = svc.btc_underperformance_ratio("DOGE/USDT", "1h", lookback_hours=2)
        self.assertIsNone(ratio)


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
            rsi=28.0,
            lower_bb=0.9,
            atr_pct=3.0,
            funding_rate_pct=-0.04,
            btc_underperf_ratio=2.0,
            vol_multiplier=1.0,
            has_position=True,
            average_entry=1.0,
            open_positions=1,
            strategy_params={
                "strategy_profile": "volatile_altcoin",
                "volatility_tier": "volatile",
                "stop_loss_pct": 18,
                "dca": _scoring_dca_cfg(mode="shadow"),
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

    def test_build_market_context_fetches_dca_inputs_for_open_position(self):
        engine = DecisionEngine()
        coin = {
            "symbol": self.symbol,
            "timeframe": self.tf,
            "strategy_params": {
                "strategy_profile": "volatile_altcoin",
                "dca": _scoring_dca_cfg(),
            },
        }
        with patch.object(
            engine.market,
            "fetch_indicators",
            return_value={
                "rsi": 28.0,
                "lower_bb": 0.9,
                "middle_bb": 0.95,
                "upper_bb": 1.0,
                "vol_multiplier": 1.0,
                "atr_pct": 3.0,
            },
        ), patch.object(engine.market, "fetch_funding_rate", return_value=-0.04) as fund_mock, patch.object(
            engine.market, "btc_underperformance_ratio", return_value=2.0
        ) as btc_mock:
            ctx = engine.build_market_context(coin, 0.92)

        fund_mock.assert_called_once()
        btc_mock.assert_called_once()
        self.assertEqual(ctx.funding_rate_pct, -0.04)
        self.assertEqual(ctx.btc_underperf_ratio, 2.0)


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