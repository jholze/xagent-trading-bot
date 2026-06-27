import unittest
from datetime import datetime, timedelta

from core.models import MarketContext
from strategies.time_profit_exit import (
    evaluate_time_profit_exit,
    symbol_in_active_bucket,
)


class TestTimeProfitExit(unittest.TestCase):
    def _params(self, **tpe):
        base = {
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "time_profit_exit": {
                "enabled": True,
                "hold_hours": 48,
                "min_gain_pct": 0,
                "mode": "active",
                "ab_test_enabled": False,
                "active_fraction": 0.5,
            },
        }
        base["time_profit_exit"].update(tpe)
        return base

    def _market(self, **kwargs):
        defaults = dict(
            symbol="H/USDT",
            timeframe="1h",
            current_price=1.10,
            has_position=True,
            average_entry=1.0,
            atr_pct=10.0,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def test_symbol_bucket_is_stable(self):
        self.assertTrue(symbol_in_active_bucket("H/USDT", 1.0))
        self.assertFalse(symbol_in_active_bucket("H/USDT", 0.0))
        self.assertEqual(
            symbol_in_active_bucket("H/USDT", 0.5),
            symbol_in_active_bucket("H/USDT", 0.5),
        )

    def test_no_trigger_before_hold_hours(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=24)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": False}
        result = evaluate_time_profit_exit(
            self._market(), pos, self._params(), now=now,
        )
        self.assertIsNone(result)

    def test_triggers_after_hold_hours_in_profit(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": False}
        result = evaluate_time_profit_exit(
            self._market(), pos, self._params(), now=now,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "time_profit_exit")
        self.assertEqual(result.action, "SELL_PARTIAL_50")
        self.assertFalse(result.shadow_only)

    def test_skips_when_not_in_profit(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": False}
        market = self._market(current_price=0.95)
        self.assertIsNone(
            evaluate_time_profit_exit(market, pos, self._params(), now=now)
        )

    def test_skips_when_already_done(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": True}
        self.assertIsNone(
            evaluate_time_profit_exit(
                self._market(), pos, self._params(), now=now,
            )
        )

    def test_ab_test_shadow_for_non_active_bucket(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": False}
        params = self._params(ab_test_enabled=True, active_fraction=0.0)
        result = evaluate_time_profit_exit(
            self._market(symbol="H/USDT"), pos, params, now=now,
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.shadow_only)

    def test_ab_test_active_for_full_bucket(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": False}
        params = self._params(ab_test_enabled=True, active_fraction=1.0)
        result = evaluate_time_profit_exit(
            self._market(symbol="H/USDT"), pos, params, now=now,
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.shadow_only)

    def test_mode_shadow_without_ab_test(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at, "time_profit_exit_done": False}
        result = evaluate_time_profit_exit(
            self._market(),
            pos,
            self._params(mode="shadow"),
            now=now,
        )
        self.assertTrue(result.shadow_only)

    def test_disabled_returns_none(self):
        now = datetime(2026, 6, 27, 12, 0, 0)
        entry_at = (now - timedelta(hours=50)).isoformat()
        pos = {"first_buy_at": entry_at}
        self.assertIsNone(
            evaluate_time_profit_exit(
                self._market(),
                pos,
                self._params(enabled=False),
                now=now,
            )
        )


if __name__ == "__main__":
    unittest.main()