import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core.actions import HOLD, SELL_FULL
from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.profit_max_lifetime import evaluate_profit_max_lifetime


class TestProfitMaxLifetime(unittest.TestCase):
    def _params(self, **overrides):
        base = {
            "strategy_profile": "volatile_altcoin",
            "profit_max_lifetime": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 3.0,
                "max_hours": 96,
                "min_gain_pct": 1.0,
                "skip_if_peak_above_pct": 40.0,
            },
        }
        base["profit_max_lifetime"].update(overrides)
        return base

    def _market(self, **kwargs):
        defaults = dict(
            symbol="H/USDT",
            timeframe="4h",
            current_price=1.05,
            has_position=True,
            average_entry=1.0,
            atr_pct=8.0,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def test_no_trigger_before_armed(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        pos = {"profit_armed_at": None, "recent_high": 1.02}
        self.assertIsNone(
            evaluate_profit_max_lifetime(self._market(), pos, self._params(), now=now)
        )

    def test_triggers_after_max_hours_in_profit(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        armed = (now - timedelta(hours=100)).isoformat()
        pos = {"profit_armed_at": armed, "recent_high": 1.08, "profit_max_lifetime_done": False}
        cand = evaluate_profit_max_lifetime(self._market(), pos, self._params(), now=now)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)
        self.assertEqual(cand.source, "profit_max_lifetime")

    def test_skips_runners_above_peak_threshold(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        armed = (now - timedelta(hours=100)).isoformat()
        pos = {"profit_armed_at": armed, "recent_high": 1.50}
        self.assertIsNone(
            evaluate_profit_max_lifetime(self._market(), pos, self._params(), now=now)
        )

    def test_skips_when_not_in_profit(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        armed = (now - timedelta(hours=100)).isoformat()
        pos = {"profit_armed_at": armed, "recent_high": 1.02}
        self.assertIsNone(
            evaluate_profit_max_lifetime(self._market(current_price=0.99), pos, self._params(), now=now)
        )


class TestMergeSellFlushAfterProfitArm(unittest.TestCase):
    """eval_worker ACU crash: local import unbound flush_positions in _merge_sell."""

    def test_profit_arm_flush_does_not_raise_unbound_local(self):
        engine = DecisionEngine(market_service=MagicMock())
        engine.config = MagicMock()
        engine.config.raw = {}
        engine.config.exit_sensor_config = {"enabled": False}
        engine.config.max_open_positions = 20

        technical = SignalAnalysis(
            action="HOLD",
            symbol="ACU/USDT",
            timeframe="4h",
            rsi=45.0,
            lower_bb=0.9,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="neutral",
            sources=[],
            confidence=50.0,
        )
        market = MarketContext(
            symbol="ACU/USDT",
            timeframe="4h",
            current_price=1.46,
            has_position=True,
            average_entry=1.0,
            atr_pct=8.0,
            strategy_params={
                "profit_max_lifetime": {
                    "enabled": True,
                    "mode": "live",
                    "arm_gain_pct": 3.0,
                    "max_hours": 96,
                    "min_gain_pct": 1.0,
                    "skip_if_peak_above_pct": 40.0,
                },
            },
        )
        pos = {"amount": 10.0, "average_entry": 1.0}

        with patch("strategies.decision_engine.flush_positions") as flush:
            action, *_rest = engine._merge_sell(
                technical, None, None, [], market=market, position=pos
            )

        self.assertEqual(action, HOLD)
        flush.assert_called()
        self.assertTrue(pos.get("profit_armed_at"))
        self.assertNotIn("flush_positions", DecisionEngine._merge_sell.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()