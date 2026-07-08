"""DecisionEngine integration — entry_guard blocks whipsaw structure sells."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import HOLD, SELL_PARTIAL_30
from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.sell_rotation_policy import SellPolicyAudit


def _technical_hold() -> SignalAnalysis:
    return SignalAnalysis(
        action="HOLD",
        symbol="DOGE/USDT",
        timeframe="4h",
        rsi=70.0,
        lower_bb=0.95,
        vol_multiplier=2.5,
        ampel_emoji="🟡",
        ampel_text="neutral",
        sources=["technical"],
        rationale="neutral",
    )


def _fresh_15m_position(*, minutes_ago: float = 3.0) -> dict:
    entry_at = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "amount": 1000.0,
        "peak_amount": 1000.0,
        "average_entry": 1.0,
        "sold_percent": 0.0,
        "entry_source": "entry_sensor_15m",
        "entry_at": entry_at,
        "first_buy_at": entry_at,
        "strategy_tier": "volatile",
        "rsi_sell_tiers_done": {},
        "recent_high": 1.02,
    }


def _bb_upper_market() -> MarketContext:
    return MarketContext(
        symbol="DOGE/USDT",
        timeframe="4h",
        current_price=1.02,
        rsi=70.0,
        upper_bb=1.01,
        lower_bb=0.95,
        middle_bb=0.98,
        vol_multiplier=2.5,
        has_position=True,
        average_entry=1.0,
        strategy_params={
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "bb_sell_enabled": True,
            "bb_sell_upper_ratio": 0.99,
            "bb_sell_rsi_min": 62,
        },
    )


class TestEntryGuardDecisionEngine(unittest.TestCase):
    def test_blocks_bb_upper_whipsaw_for_fresh_15m_entry(self):
        engine = DecisionEngine()
        market = _bb_upper_market()
        position = _fresh_15m_position()
        technical = _technical_hold()
        continuation_metrics = {
            "volume_spike_ratio": 2.8,
            "price_momentum": True,
            "body_atr_ratio": 0.5,
        }

        rotation_passthrough = lambda cands, *args, **kwargs: (cands, SellPolicyAudit())
        with patch("strategies.decision_engine.get_position", return_value=position), patch(
            "strategies.decision_engine.apply_rotation_sell_filters",
            side_effect=rotation_passthrough,
        ), patch.object(
            engine.market,
            "fetch_15m_sensor_metrics",
            return_value=continuation_metrics,
        ):
            action, _, _, rationales, sell_source, _ = engine._merge_sell(
                technical,
                None,
                None,
                [],
                market,
                position,
                None,
            )

        self.assertEqual(action, HOLD)
        self.assertTrue(any("EntryGuard" in r for r in rationales))

    def test_allows_bb_upper_after_mega_pump(self):
        engine = DecisionEngine()
        market = _bb_upper_market()
        market.current_price = 1.14
        position = _fresh_15m_position(minutes_ago=5)
        technical = _technical_hold()
        continuation_metrics = {
            "volume_spike_ratio": 2.8,
            "price_momentum": True,
        }

        rotation_passthrough = lambda cands, *args, **kwargs: (cands, SellPolicyAudit())
        with patch("strategies.decision_engine.get_position", return_value=position), patch(
            "strategies.decision_engine.apply_rotation_sell_filters",
            side_effect=rotation_passthrough,
        ), patch.object(
            engine.market,
            "fetch_15m_sensor_metrics",
            return_value=continuation_metrics,
        ):
            action, _, _, rationales, sell_source, _ = engine._merge_sell(
                technical,
                None,
                None,
                [],
                market,
                position,
                None,
            )

        self.assertEqual(action, SELL_PARTIAL_30)
        self.assertEqual(sell_source, "bb_upper")
        self.assertFalse(any("EntryGuard" in r for r in rationales))


if __name__ == "__main__":
    unittest.main()