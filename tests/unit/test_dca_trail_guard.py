"""DCA recovery vs trail exits (#217) — grace pause + peak re-anchor."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.models import MarketContext
from strategies.dca import (
    reanchor_recent_high_after_dca,
    trail_exits_paused_after_dca,
)
from strategies.positions import get_key, get_position, positions, update_position
from strategies.trailing_stop import evaluate_trailing_stop
from strategies.trailing_take_profit import evaluate_trailing_take_profit


def _mkt(symbol: str, price: float, entry: float, atr: float = 5.0) -> MarketContext:
    return MarketContext(
        symbol=symbol,
        timeframe="1h",
        current_price=price,
        has_position=True,
        average_entry=entry,
        atr_pct=atr,
        strategy_params={},
    )


class TestDcaTrailGuard(unittest.TestCase):
    def test_pause_active_within_grace(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        pos = {
            "dca_rounds": 1,
            "last_dca_at": (now - timedelta(hours=1)).isoformat(),
        }
        params = {
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            }
        }
        paused, why = trail_exits_paused_after_dca(pos, params, now=now)
        self.assertTrue(paused)
        self.assertIn("dca_trail_pause", why)

    def test_pause_expires_after_grace(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        pos = {
            "dca_rounds": 1,
            "last_dca_at": (now - timedelta(hours=13)).isoformat(),
        }
        params = {
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            }
        }
        paused, why = trail_exits_paused_after_dca(pos, params, now=now)
        self.assertFalse(paused)
        self.assertEqual(why, "")

    def test_pause_can_be_disabled(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        pos = {
            "dca_rounds": 2,
            "last_dca_at": (now - timedelta(minutes=5)).isoformat(),
        }
        params = {"dca": {"pause_trail_exits_after_dca": False}}
        paused, _ = trail_exits_paused_after_dca(pos, params, now=now)
        self.assertFalse(paused)

    def test_beat_like_no_trailing_stop_right_after_dca(self):
        """Old peak high, DCA lower, price under old stop → must NOT trail-stop in grace."""
        now = datetime(2026, 8, 6, 6, 38, 39)
        entry = 2.20  # blended avg after DCA
        recent_high = 2.73  # pre-dump peak (if not reanchored)
        price = 2.131
        pos = {
            "average_entry": entry,
            "recent_high": recent_high,
            "dca_rounds": 1,
            "last_dca_at": (now - timedelta(seconds=21)).isoformat(),
        }
        params = {
            "trailing_stop": {
                "enabled": True,
                "activation_gain_pct": 5.0,
                "min_trail_pct": 5.0,
                "max_trail_pct": 25.0,
                "atr_multiplier": 1.0,
                "floor_at_entry": True,
                "arm_on_peak": True,
            },
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            },
        }
        market = _mkt("BEAT/USDT", price, entry, atr=6.0)
        # Without guard this would fire (peak ~24%, drop ~22%)
        cand = evaluate_trailing_stop(market, pos, params, now=now)
        self.assertIsNone(cand)

        pos_old = dict(pos)
        pos_old["last_dca_at"] = (now - timedelta(hours=13)).isoformat()
        # After grace, still underwater vs blended entry → DCA zone, not trail
        cand2 = evaluate_trailing_stop(market, pos_old, params, now=now)
        self.assertIsNone(cand2)

        # Recovered above entry, still below trail stop → trail may fire
        market_green = _mkt("BEAT/USDT", 2.25, entry, atr=6.0)
        cand3 = evaluate_trailing_stop(market_green, pos_old, params, now=now)
        self.assertIsNotNone(cand3)
        self.assertEqual(cand3.source, "trailing_stop")

    def test_ttp_also_paused_in_grace(self):
        now = datetime(2026, 8, 6, 12, 0, 0)
        entry = 1.0
        pos = {
            "average_entry": entry,
            "recent_high": 1.25,
            "dca_rounds": 1,
            "last_dca_at": (now - timedelta(hours=2)).isoformat(),
            "trail_tp_steps": 0,
        }
        params = {
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 10.0,
                "min_gain_pct": 5.0,
                "trail_pct": 6.0,
                "dynamic_trail": False,
                "max_steps": 1,
            },
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            },
        }
        # Price dropped 8% from high with gain still positive
        market = _mkt("X/USDT", 1.15, entry)
        self.assertIsNone(
            evaluate_trailing_take_profit(market, pos, params, now=now)
        )

    def test_reanchor_peak_after_dca(self):
        pos = {"average_entry": 2.20, "recent_high": 2.80}
        h = reanchor_recent_high_after_dca(pos, fill_price=2.18)
        self.assertAlmostEqual(h, 2.20)  # max(fill, avg)
        self.assertAlmostEqual(pos["recent_high"], 2.20)
        self.assertIsNotNone(pos.get("trail_peak_reanchored_at"))

    def test_update_position_dca_reanchors_peak(self):
        symbol = "BEATDCA/USDT"
        tf = "1h"
        key = get_key(symbol, tf)
        backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        try:
            update_position(symbol, tf, "BUY", 2.50, 100)
            pos = get_position(symbol, tf)
            pos["recent_high"] = 2.80
            update_position(symbol, tf, "BUY_DCA", 2.18, 100)
            pos2 = get_position(symbol, tf)
            self.assertEqual(int(pos2.get("dca_rounds") or 0), 1)
            # Peak must not remain 2.80 after re-anchor
            self.assertLessEqual(float(pos2.get("recent_high") or 0), 2.50)
            self.assertGreaterEqual(float(pos2.get("recent_high") or 0), 2.18)
        finally:
            positions.clear()
            positions.update(backup)

    def test_no_dca_trail_still_fires(self):
        """Without DCA history, trail stop behaves as before."""
        now = datetime(2026, 8, 6, 12, 0, 0)
        entry = 1.0
        pos = {
            "average_entry": entry,
            "recent_high": 1.20,
            "dca_rounds": 0,
        }
        params = {
            "trailing_stop": {
                "enabled": True,
                "activation_gain_pct": 5.0,
                "min_trail_pct": 5.0,
                "max_trail_pct": 25.0,
                "atr_multiplier": 1.0,
                "floor_at_entry": True,
                "arm_on_peak": True,
            },
            "dca": {"pause_trail_exits_after_dca": True},
        }
        # drop 10% from peak 1.20 → 1.08, trail ~5% → stop ~1.14, fire
        market = _mkt("Y/USDT", 1.08, entry, atr=5.0)
        cand = evaluate_trailing_stop(market, pos, params, now=now)
        self.assertIsNotNone(cand)


if __name__ == "__main__":
    unittest.main()
