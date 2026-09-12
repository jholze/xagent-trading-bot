from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from core.costs import CostModel
from strategies.short_cover import evaluate_short_cover
from strategies.short_math import apply_liq_buffer, liquidation_price_isolated, stop_price


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

    def test_cover_stop_and_liq_fire_at_3x_not_2x(self):
        # short_cover pre-clamps lev to leverage_cap then used to re-clamp to
        # 2.0 inside stop_price / liquidation_price_isolated. A 3x lot's stop
        # and liq therefore fired at the 2x prices (too far from entry).
        cfg = {
            "shorts": {
                "enabled": True,
                "leverage_default": 2,
                "leverage_cap": 3,
                "liquidation_buffer": 0.05,
                "volatile": {"time_cap_hours": 4, "stop_margin_pct": 0.10},
                "stable": {"time_cap_hours": 8, "stop_margin_pct": 0.08},
            }
        }
        entry, lev = 10.0, 3.0
        pos, _ = _short(entry=entry, mark=entry, lev=lev)

        stop_3x = stop_price("short", entry, 0.10, lev)
        stop_2x = entry * (1.0 + 0.10 / 2.0)
        self.assertAlmostEqual(stop_3x, entry * (1.0 + 0.10 / lev))
        self.assertLess(stop_3x, stop_2x)
        hit_stop = evaluate_short_cover(pos, stop_3x, config_raw=cfg)
        self.assertIsNotNone(hit_stop)
        self.assertEqual(hit_stop["source"], "trailing_stop")

        fee_frac = CostModel.from_config(cfg, market="swap").fee_pct("market") / 100.0
        liq_3x = apply_liq_buffer(
            "short",
            entry,
            liquidation_price_isolated("short", entry, lev, fee_frac=fee_frac),
            0.05,
        )
        liq_2x = apply_liq_buffer(
            "short",
            entry,
            liquidation_price_isolated("short", entry, 2.0, fee_frac=fee_frac),
            0.05,
        )
        self.assertLess(liq_3x, liq_2x)
        hit_liq = evaluate_short_cover(pos, liq_3x, config_raw=cfg)
        self.assertIsNotNone(hit_liq)
        self.assertEqual(hit_liq["source"], "liquidation")

    def test_time_cap_volatile_4h(self):
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        pos, mark = _short(mark=100.2, opened=old, tier="volatile")
        hit = evaluate_short_cover(pos, mark, now=datetime.now(timezone.utc), config_raw=CFG)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "time_cap")

    def test_kill_switch_still_covers_open_short(self):
        pos, mark = _short(mark=130.0)
        hit = evaluate_short_cover(
            pos, mark, config_raw={"shorts": {"enabled": False, "leverage_default": 2, "volatile": {"stop_margin_pct": 0.10}}},
        )
        self.assertIsNotNone(hit)

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
