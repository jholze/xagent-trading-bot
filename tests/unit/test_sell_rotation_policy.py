"""Tests for rotation-safe sell policy."""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import SELL_FULL, SELL_PARTIAL_30
from core.models import MarketContext
from strategies.positions import count_open_full_slots, get_key, positions, update_position
from strategies.sell_rotation_policy import (
    apply_rotation_sell_filters,
    can_rotation_evict,
    evaluate_ladder_terminal,
    filter_profit_full_close,
    filter_trail_exclusive,
    is_tail_position,
)


class TestSellRotationPolicy(unittest.TestCase):
    def setUp(self):
        self.symbol = "ROT/USDT"
        self.tf = "4h"
        self.key = get_key(self.symbol, self.tf)
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        self.cfg = {
            "trail_exclusive": True,
            "evict_min_gain_pct": 0,
            "arm_gain_pct": 12,
            "tail_exempt_sold_pct": 0.5,
            "tail_exempt_notional_usdt": 800,
            "trail_exit_full_close": True,
        }
        self.raw = {"sell_policy": {"mode": "shadow", "rotation": self.cfg}}

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
        )

    def test_blocks_eviction_when_loser(self):
        pos = {"realized_pnl": 0}
        self.assertFalse(can_rotation_evict(self._market(1.0, 0.9), pos, self.cfg))

    def test_allows_eviction_when_plus(self):
        self.assertTrue(can_rotation_evict(self._market(1.0, 1.05), {}, self.cfg))

    def test_trail_exclusive_blocks_bb_when_trail_armed(self):
        market = self._market(1.0, 1.20)
        pos = {"recent_high": 1.20}
        cands = [(SELL_PARTIAL_30, 3, "bb_upper")]
        params = {
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 15,
            },
        }
        kept, blocked = filter_trail_exclusive(
            cands, market, pos, self.cfg, strategy_params=params,
        )
        self.assertEqual(kept, [])
        self.assertEqual(blocked, ["bb_upper"])

    def test_trail_exclusive_passes_without_trail_config(self):
        market = self._market(1.0, 1.05)
        pos = {}
        cands = [(SELL_PARTIAL_30, 3, "bb_upper")]
        kept, blocked = filter_trail_exclusive(cands, market, pos, self.cfg)
        self.assertEqual(kept, cands)
        self.assertEqual(blocked, [])

    def test_ladder_terminal_on_completed_ladder(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = positions[self.key]
        pos["exit_ladder_step"] = 3
        pos["peak_amount"] = 1000.0
        params = {
            "exit_ladder": {"enabled": True, "tiers": [0.35, 0.35, 0.3]},
        }
        cand = evaluate_ladder_terminal(self._market(1.0, 1.1), pos, params, self.cfg)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)

    def test_tail_position_detection(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 600)
        pos = positions[self.key]
        pos["peak_amount"] = 1000.0
        pos["sold_percent"] = 0.6
        pos["amount"] = Decimal("400")
        self.assertTrue(is_tail_position(pos, self.cfg))

    def test_count_open_full_slots_excludes_tails(self):
        update_position("FULL/USDT", self.tf, "BUY", 10.0, 100)
        update_position("TAIL/USDT", self.tf, "BUY", 1.0, 1000)
        tail = positions[get_key("TAIL/USDT", self.tf)]
        tail["peak_amount"] = 1000.0
        tail["sold_percent"] = 0.6
        tail["amount"] = Decimal("400")
        self.assertEqual(count_open_full_slots(self.raw), 1)

    def test_profit_full_close_upgrades_partial_on_plus(self):
        cfg = {**self.cfg, "profit_exit_full_close": True}
        market = self._market(1.0, 1.08)
        pos = {}
        cands = [(SELL_PARTIAL_30, 3, "technical")]
        out = filter_profit_full_close(cands, market, pos, cfg)
        self.assertEqual(out[0][0], SELL_FULL)

    def test_profit_full_close_skips_losers(self):
        cfg = {**self.cfg, "profit_exit_full_close": True}
        market = self._market(1.0, 0.92)
        pos = {}
        cands = [(SELL_PARTIAL_30, 3, "technical")]
        out = filter_profit_full_close(cands, market, pos, cfg)
        self.assertEqual(out[0][0], SELL_PARTIAL_30)

    def test_rotation_filter_blocks_loser_ladder_terminal(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = positions[self.key]
        pos["exit_ladder_step"] = 3
        pos["peak_amount"] = 1000.0
        params = {"exit_ladder": {"enabled": True, "tiers": [0.35, 0.35, 0.3]}}
        cands = []
        out, audit = apply_rotation_sell_filters(
            cands, self._market(1.0, 0.92), pos, params, self.raw,
        )
        self.assertEqual(out, [])
        self.assertFalse(audit.ladder_terminal_would_close)


if __name__ == "__main__":
    unittest.main()