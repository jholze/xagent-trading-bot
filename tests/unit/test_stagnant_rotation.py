"""Unit tests for stagnant-rotation close (capacity-aware, all symbols)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import SELL_FULL
from core.models import MarketContext
from strategies.recovery_hold import source_allowed_under_recovery_hold
from strategies.sell_rotation_policy import (
    POLICY_DEFAULTS,
    apply_rotation_sell_filters,
    evaluate_stagnant_rotation_close,
    rotation_config,
)


NOW = datetime(2026, 8, 12, 12, 0, 0)


def _market(entry: float, price: float, symbol: str = "STAG/USDT") -> MarketContext:
    return MarketContext(
        symbol=symbol,
        timeframe="4h",
        current_price=price,
        has_position=True,
        average_entry=entry,
    )


def _pos(*, idle_hours: float, realized_pnl: float = 0.0) -> dict:
    last = (NOW - timedelta(hours=idle_hours)).isoformat()
    return {
        "symbol": "STAG/USDT",
        "last_trade_at": last,
        "realized_pnl": realized_pnl,
        "amount": 100.0,
    }


def _pos_with(*, last_trade_hours: float, peak_hours: float | None) -> dict:
    """Distinguish last_trade_at (any fill) from peak_at (last real progress)."""
    last_trade = (NOW - timedelta(hours=last_trade_hours)).isoformat()
    row = {
        "symbol": "STAG/USDT",
        "last_trade_at": last_trade,
        "realized_pnl": 0.0,
        "amount": 100.0,
    }
    if peak_hours is not None:
        row["peak_at"] = (NOW - timedelta(hours=peak_hours)).isoformat()
    return row


def _cfg(**over) -> dict:
    cfg = {
        "stagnant_rotation_enabled": True,
        "stagnant_gain_pct": 8.0,
        "stagnant_idle_hours": 24.0,
        "stagnant_slack_slots": 2,
        "evict_min_gain_pct": 0.0,
    }
    cfg.update(over)
    return cfg


class TestEvaluateStagnantRotationClose(unittest.TestCase):
    def test_disabled_returns_none(self):
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10),
            _pos(idle_hours=30),
            _cfg(stagnant_rotation_enabled=False),
            open_full_slots=35,
            eff_cap=36,
            now=NOW,
        )
        self.assertIsNone(cand)

    def test_slots_not_tight_returns_none(self):
        # 20 < 36 - 2 → plenty of slack
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10),
            _pos(idle_hours=30),
            _cfg(),
            open_full_slots=20,
            eff_cap=36,
            now=NOW,
        )
        self.assertIsNone(cand)

    def test_tight_green_flat_long_enough_fires(self):
        # 35 >= 36 - 2, gain 10% >= 8, idle 30h >= 24
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10),
            _pos(idle_hours=30),
            _cfg(),
            open_full_slots=35,
            eff_cap=36,
            now=NOW,
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)
        self.assertEqual(cand.source, "stagnant_rotation")
        self.assertEqual(cand.priority, 4)
        self.assertIn("Stagnant rotation", cand.rationale)

    def test_tight_red_returns_none(self):
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 0.90),
            _pos(idle_hours=30),
            _cfg(),
            open_full_slots=35,
            eff_cap=36,
            now=NOW,
        )
        self.assertIsNone(cand)

    def test_tight_green_too_recent_returns_none(self):
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10),
            _pos(idle_hours=6),
            _cfg(),
            open_full_slots=35,
            eff_cap=36,
            now=NOW,
        )
        self.assertIsNone(cand)

    def test_gain_below_threshold_returns_none(self):
        # 5% < 8%
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.05),
            _pos(idle_hours=30),
            _cfg(),
            open_full_slots=35,
            eff_cap=36,
            now=NOW,
        )
        self.assertIsNone(cand)

    def test_composed_into_apply_rotation_sell_filters(self):
        pos = _pos(idle_hours=30)
        raw = {
            "max_open_positions": 36,
            "sell_policy": {
                "mode": "shadow",
                "rotation": _cfg(),
            },
        }
        out, _audit = apply_rotation_sell_filters(
            [],
            _market(1.0, 1.10),
            pos,
            {},
            raw,
            open_full_slots=35,
            eff_cap=36,
            now=NOW,
        )
        sources = [c[2] for c in out]
        self.assertIn("stagnant_rotation", sources)
        self.assertEqual(out[0][0], SELL_FULL)

    def test_recovery_hold_fail_closed(self):
        """stagnant_rotation is not a recovery-hold allow-list source."""
        self.assertFalse(source_allowed_under_recovery_hold("stagnant_rotation"))

    def test_policy_defaults_include_stagnant_keys(self):
        self.assertFalse(POLICY_DEFAULTS.get("stagnant_rotation_enabled"))
        self.assertEqual(POLICY_DEFAULTS.get("stagnant_gain_pct"), 8.0)
        self.assertEqual(POLICY_DEFAULTS.get("stagnant_idle_hours"), 24.0)
        self.assertEqual(POLICY_DEFAULTS.get("stagnant_slack_slots"), 2)

    def test_partial_sell_does_not_reset_idle_via_peak_at(self):
        # last_trade_at is fresh (a partial sell fired 1h ago), but the real
        # peak was 30h ago — with peak_at present, idle is measured from the
        # peak, so a recent partial fill no longer blocks the rotation.
        pos = _pos_with(last_trade_hours=1, peak_hours=30)
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10), pos, _cfg(), open_full_slots=35, eff_cap=36, now=NOW,
        )
        self.assertIsNotNone(cand)

    def test_recent_peak_blocks_even_with_old_last_trade_at(self):
        # Mirror case: last_trade_at is old, but peak_at is recent (a new
        # high just printed) — position is still actively progressing, so
        # it should NOT be treated as stagnant yet.
        pos = _pos_with(last_trade_hours=40, peak_hours=2)
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10), pos, _cfg(), open_full_slots=35, eff_cap=36, now=NOW,
        )
        self.assertIsNone(cand)

    def test_missing_peak_at_falls_back_to_last_trade_at(self):
        # Positions opened before this field existed still work.
        pos = _pos_with(last_trade_hours=30, peak_hours=None)
        self.assertNotIn("peak_at", pos)
        cand = evaluate_stagnant_rotation_close(
            _market(1.0, 1.10), pos, _cfg(), open_full_slots=35, eff_cap=36, now=NOW,
        )
        self.assertIsNotNone(cand)

    def test_stable_tier_override_applies(self):
        cfg = rotation_config(
            {
                "sell_policy": {
                    "rotation": {
                        "stagnant_gain_pct_stable": 5.0,
                        "stagnant_idle_hours_stable": 12.0,
                    }
                }
            },
            strategy_params={"volatility_tier": "stable"},
        )
        self.assertEqual(cfg["stagnant_gain_pct"], 5.0)
        self.assertEqual(cfg["stagnant_idle_hours"], 12.0)

    def test_volatile_tier_keeps_global_defaults(self):
        cfg = rotation_config(
            {
                "sell_policy": {
                    "rotation": {
                        "stagnant_gain_pct_stable": 5.0,
                        "stagnant_idle_hours_stable": 12.0,
                    }
                }
            },
            strategy_params={"volatility_tier": "volatile"},
        )
        self.assertEqual(cfg["stagnant_gain_pct"], 8.0)
        self.assertEqual(cfg["stagnant_idle_hours"], 24.0)


if __name__ == "__main__":
    unittest.main()
