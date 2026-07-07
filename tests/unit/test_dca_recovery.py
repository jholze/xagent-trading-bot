"""Unit tests for DCA-Recovery (PR1–PR3)."""

from __future__ import annotations

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
from strategies.dca import effective_stop_loss_thresholds
from strategies.dca_recovery import (
    evaluate_dca_recovery,
    in_recovery_phase,
    recovery_enabled,
    should_dca_recovery,
)
from strategies.decision_engine import DecisionEngine
from strategies.positions import get_key, get_position, positions, update_position
from tests.unit.test_dca import _scoring_dca_cfg


def _recovery_cfg(mode: str = "live") -> dict:
    return {
        "enabled": False,
        "mode": mode,
        "interval_hours": 8,
        "max_rounds": 2,
        "loss_pct_min": -25,
        "loss_pct_max": -2,
        "max_sold_percent": 0.85,
        "min_remainder_usdt": 50,
        "remainder_size_ratio": 0.35,
        "sl_proximity_pct": 12,
        "cascade_min_drop_pct": 4.0,
        "cascade_score_discount": 1,
        "scoring_inherit": True,
    }


class TestDCARecoveryModule(unittest.TestCase):
    def setUp(self):
        self.symbol = "RECOV/USDT"
        self.tf = "4h"
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        self.params = {
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "stop_loss_pct": 50,
            "dca": {**_scoring_dca_cfg(), "recovery": _recovery_cfg()},
        }

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def _market(self, entry: float, price: float, **kwargs) -> MarketContext:
        defaults = dict(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=price,
            rsi=28.0,
            lower_bb=price * 0.95,
            atr_pct=3.0,
            funding_rate_pct=-0.04,
            btc_underperf_ratio=2.0,
            has_position=True,
            average_entry=entry,
            open_positions=1,
            strategy_params=self.params,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def _partial_tail_pos(self, *, sold: float = 0.30, amount: float = 700):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = get_position(self.symbol, self.tf)
        pos["peak_amount"] = 1000.0
        pos["sold_percent"] = sold
        pos["exit_ladder_step"] = 1
        pos["amount"] = Decimal(str(amount))
        pos["average_entry"] = 1.0
        return pos

    def test_recovery_enabled_follows_dca_enabled(self):
        self.assertTrue(recovery_enabled(self.params))
        off = dict(self.params)
        off["dca"] = {**_scoring_dca_cfg(), "enabled": False}
        self.assertFalse(recovery_enabled(off))

    def test_in_recovery_phase(self):
        pos = self._partial_tail_pos()
        self.assertTrue(in_recovery_phase(pos))
        pos["sold_percent"] = 0.0
        pos["exit_ladder_step"] = 0
        self.assertFalse(in_recovery_phase(pos))

    def test_recovery_triggers_on_minus_tail(self):
        pos = self._partial_tail_pos()
        cand = evaluate_dca_recovery(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, BUY_DCA)
        self.assertEqual(cand.source, "dca")
        self.assertGreater(cand.usdt_amount, 0)

    def test_unified_dca_works_in_accumulation(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = get_position(self.symbol, self.tf)
        pos["average_entry"] = 1.0
        cand = evaluate_dca_recovery(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "dca")

    def test_recovery_blocked_when_gain_positive(self):
        pos = self._partial_tail_pos()
        cand = evaluate_dca_recovery(self._market(1.0, 1.05), pos, self.params)
        self.assertIsNone(cand)

    def test_recovery_blocked_when_sold_too_high(self):
        pos = self._partial_tail_pos(sold=0.90, amount=100)
        cand = evaluate_dca_recovery(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNone(cand)

    def test_recovery_blocked_within_interval(self):
        pos = self._partial_tail_pos()
        pos["last_dca_at"] = datetime.now().isoformat()
        pos["dca_rounds"] = 1
        cand = evaluate_dca_recovery(self._market(1.0, 0.92), pos, self.params)
        self.assertIsNone(cand)

    def test_recovery_buy_preserves_ladder_and_increments_recovery_rounds(self):
        pos = self._partial_tail_pos()
        update_position(self.symbol, self.tf, "BUY_DCA", 0.9, 7.78)
        pos = get_position(self.symbol, self.tf)
        self.assertEqual(pos["exit_ladder_step"], 1)
        self.assertAlmostEqual(float(pos["sold_percent"]), 0.30, places=2)
        self.assertEqual(pos["dca_rounds"], 1)
        self.assertIsNotNone(pos["last_dca_at"])
        self.assertAlmostEqual(float(pos["last_recovery_ref_price"]), 0.9)

    def test_effective_stop_counts_recovery_rounds(self):
        pos = {"dca_rounds": 1, "dca_recovery_rounds": 1, "last_dca_recovery_at": None}
        params = {"dca": {"interval_hours": 12, "stop_loss_widen_pct_per_round": 6}}
        full, partial, _ = effective_stop_loss_thresholds(pos, params, 50.0)
        self.assertEqual(full, 62.0)
        self.assertIsNone(partial)


class TestDCARecoveryDecisionEngine(unittest.TestCase):
    def setUp(self):
        self.symbol = "RECOV2/USDT"
        self.tf = "4h"
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = get_position(self.symbol, self.tf)
        pos["peak_amount"] = 1000.0
        pos["sold_percent"] = 0.30
        pos["exit_ladder_step"] = 1
        pos["amount"] = Decimal("700")
        pos["average_entry"] = 1.0

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_decision_engine_unified_dca_on_partial_tail(self):
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
                "volatility_tier": "volatile",
                "stop_loss_pct": 50,
                "dca": {**_scoring_dca_cfg(), "recovery": _recovery_cfg()},
            },
        )
        engine = DecisionEngine()
        with patch.object(engine, "_merge_sell", return_value=("HOLD", ["technical"], 50.0, [], "", {})):
            analysis = engine.evaluate_with_market(
                {"symbol": self.symbol, "timeframe": self.tf},
                market,
            )
        self.assertEqual(analysis.action, "BUY_DCA")
        self.assertIn("dca", analysis.sources)


class TestDCARecoveryRisk(unittest.TestCase):
    def setUp(self):
        self.symbol = "RECOVR/USDT"
        self.tf = "4h"
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_dca_interval_uses_last_dca_at(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = get_position(self.symbol, self.tf)
        pos["sold_percent"] = 0.30
        pos["last_dca_at"] = datetime.now().isoformat()

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
            usdt_amount=140,
            signal="BUY_DCA",
            source="dca_recovery",
        )
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), \
             patch.object(risk, "_portfolio_equity", return_value=5000.0), \
             patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
            decision = risk.evaluate(order, self.tf, source="dca_recovery")

        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "trade_cooldown")
        self.assertIn("DCA interval", decision.message)

    def test_recovery_counts_toward_daily_dca_limit(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)

        from core.config import BotConfig
        from data_manager import get_config

        raw = dict(get_config())
        raw.setdefault("risk", {})["max_daily_dca_buys"] = 2
        raw.setdefault("live", {})["dry_run_enhanced"] = False
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol=self.symbol,
            price=1.0,
            amount=0,
            usdt_amount=140,
            signal="BUY_DCA",
            source="dca_recovery",
        )
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), \
             patch.object(risk, "_portfolio_equity", return_value=5000.0), \
             patch.object(risk, "_daily_dca_buys_count", return_value=2), \
             patch("risk.risk_manager.load_trade_history", return_value={"virtual_balance": 5000.0}):
            decision = risk.evaluate(order, self.tf, source="dca_recovery")

        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "max_daily_dca_buys")


if __name__ == "__main__":
    unittest.main()