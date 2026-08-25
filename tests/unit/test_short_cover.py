from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from strategies.short_cover import evaluate_short_cover
from strategies.short_math import stop_price


CFG = {
    "shorts": {
        "enabled": True,
        "leverage_default": 2,
        "leverage_cap": 5,
        "liquidation_buffer": 0.05,
        "volatile": {"time_cap_hours": 4, "stop_margin_pct": 0.10},
        "stable": {"time_cap_hours": 8, "stop_margin_pct": 0.08},
    }
}


def _short(*, entry=100.0, mark=100.0, lev=2.0, opened=None, tier="volatile"):
    return {
        "side": "short",
        "amount": 1.0,
        "average_entry": entry,
        "leverage": lev,
        "strategy_tier": tier,
        "entry_at": (opened or datetime.now(timezone.utc)).isoformat(),
        "symbol": "AAA/USDT",
    }, mark


class TestShortCover(unittest.TestCase):
    def test_no_cover_near_entry(self):
        pos, mark = _short(mark=100.5)
        self.assertIsNone(evaluate_short_cover(pos, mark, config_raw=CFG))

    def test_stop_before_liq(self):
        pos, _ = _short()
        stop = stop_price("short", 100.0, 0.10, 2.0)  # 105
        hit = evaluate_short_cover(pos, stop, config_raw=CFG)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "trailing_stop")

    def test_time_cap_volatile_4h(self):
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        pos, mark = _short(mark=100.2, opened=old, tier="volatile")
        hit = evaluate_short_cover(pos, mark, now=datetime.now(timezone.utc), config_raw=CFG)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "time_cap")

    def test_disabled(self):
        pos, mark = _short(mark=130.0)
        self.assertIsNone(evaluate_short_cover(pos, mark, config_raw={"shorts": {"enabled": False}}))

    def test_long_ignored(self):
        pos = {"side": "long", "amount": 1, "average_entry": 100}
        self.assertIsNone(evaluate_short_cover(pos, 50, config_raw=CFG))

    def test_trail_after_arm_and_bounce(self):
        pos, _ = _short(mark=90)
        pos["recent_low"] = 90.0
        hit = evaluate_short_cover(pos, 92.0, config_raw=CFG)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "trailing_take_profit")

    def test_rsi_cover_in_profit(self):
        pos, mark = _short(mark=94.0)
        pos["last_rsi"] = 28
        hit = evaluate_short_cover(pos, mark, config_raw=CFG)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "rsi_cover")
