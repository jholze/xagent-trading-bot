"""DCA recovery vs trail exits (#217) — grace pause + peak re-anchor."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.actions import SELL_FULL
from core.models import MarketContext
from strategies.dca import (
    reanchor_recent_high_after_dca,
    recent_high_reached_after_dca,
    trail_exits_paused_after_dca,
)
from strategies.positions import get_key, get_position, positions, update_position
from strategies.technical_rsi_bb import TechnicalRSIStrategy
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

    def test_ttp_fires_on_post_dca_new_peak_inside_grace(self):
        """#558: a peak printed after the DCA fill arms trailing take-profit.

        The 12h pause stays true. The exact high is not a sell. max_steps=1
        stays SELL_FULL. A trailing stop on that same window stays paused.
        """
        now = datetime(2026, 9, 23, 4, 0, 0)
        dca_at = now - timedelta(hours=2)
        entry = 0.14482
        recent_high = 0.23271  # ~+60.7% vs blended entry
        # 12% trail would sit at high * 0.88. One step through it, not the high.
        pulled_back = recent_high * 0.87
        params = {
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 15.0,
                "min_gain_pct_floor": 8.0,
                "dynamic_trail": True,
                "trail_pct_min": 3.0,
                "trail_pct_max": 12.0,
                "trail_pct_scale_start_pct": 18.0,
                "trail_pct_scale_peak_pct": 45.0,
                "max_steps": 1,
                "cooldown_hours": 0,
            },
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
        pos = {
            "average_entry": entry,
            "recent_high": recent_high,
            "dca_rounds": 1,
            "last_dca_at": dca_at.isoformat(),
            "peak_at": (dca_at + timedelta(hours=1)).isoformat(),
            "trail_tp_steps": 0,
        }
        paused, why = trail_exits_paused_after_dca(pos, params, now=now)
        self.assertTrue(paused)
        self.assertIn("dca_trail_pause", why)
        self.assertTrue(recent_high_reached_after_dca(pos))

        at_high = _mkt("龙虾/USDT", recent_high, entry)
        self.assertIsNone(
            evaluate_trailing_take_profit(at_high, pos, params, now=now)
        )

        market = _mkt("龙虾/USDT", pulled_back, entry)
        cand = evaluate_trailing_take_profit(market, pos, params, now=now)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)
        self.assertEqual(cand.source, "trailing_take_profit")
        # Stop still uses the grace pause, even on this post-DCA high.
        self.assertIsNone(evaluate_trailing_stop(market, pos, params, now=now))

        # Same prices, but the stored peak is the pre-DCA high.
        pre = dict(pos)
        pre["peak_at"] = (dca_at - timedelta(hours=5)).isoformat()
        pre.pop("peak_epoch_high", None)
        self.assertFalse(recent_high_reached_after_dca(pre))
        self.assertIsNone(
            evaluate_trailing_take_profit(market, pre, params, now=now)
        )
        self.assertIsNone(evaluate_trailing_stop(market, pre, params, now=now))

    def test_ttp_ws_bump_above_dca_epoch_inside_grace(self):
        """exit_ws can raise recent_high without refreshing peak_at."""
        now = datetime(2026, 9, 23, 4, 0, 0)
        dca_at = now - timedelta(hours=2)
        entry = 1.0
        pos = {
            "average_entry": entry,
            "recent_high": 1.61,
            "peak_epoch_high": entry,
            "dca_rounds": 1,
            "last_dca_at": dca_at.isoformat(),
            "peak_at": (dca_at - timedelta(hours=3)).isoformat(),
            "trail_tp_steps": 0,
        }
        params = {
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 15.0,
                "min_gain_pct_floor": 8.0,
                "dynamic_trail": True,
                "trail_pct_min": 3.0,
                "trail_pct_max": 12.0,
                "trail_pct_scale_start_pct": 18.0,
                "trail_pct_scale_peak_pct": 45.0,
                "max_steps": 1,
                "cooldown_hours": 0,
            },
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            },
        }
        self.assertTrue(trail_exits_paused_after_dca(pos, params, now=now)[0])
        self.assertTrue(recent_high_reached_after_dca(pos))
        self.assertIsNone(
            evaluate_trailing_take_profit(
                _mkt("龙虾/USDT", 1.61, entry), pos, params, now=now
            )
        )
        cand = evaluate_trailing_take_profit(
            _mkt("龙虾/USDT", 1.40, entry), pos, params, now=now
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)

    def test_fixed_tp_tiers_use_post_dca_peak_and_live_price(self):
        """40/80/120 stay put. A post-DCA peak or a live tick may hit 40."""
        strategy = TechnicalRSIStrategy()
        dca_at = datetime.now() - timedelta(hours=1)
        params = {
            "take_profit_tiers": [40, 80, 120],
            "stop_loss_pct": 50,
        }
        coin = {"symbol": "龙虾/USDT", "timeframe": "1h", "strategy_params": params}

        def analyze(price: float, **state):
            market = MarketContext(
                symbol="龙虾/USDT",
                timeframe="1h",
                current_price=price,
                rsi=55.0,
                lower_bb=1.0,
                has_position=True,
                average_entry=1.0,
                strategy_params=params,
                sim_state={
                    "rsi_sell_tiers_done": {},
                    "last_rsi": 50.0,
                    "dca_rounds": 1,
                    "last_dca_at": dca_at.isoformat(),
                    **state,
                },
            )
            return strategy.analyze(coin, market)

        # Live +29% missed 40; the post-DCA peak at +61% may take the 40 tier.
        post = analyze(
            1.29,
            recent_high=1.61,
            peak_at=(dca_at + timedelta(hours=1)).isoformat(),
        )
        self.assertEqual(post.normalized_action, "SELL_PARTIAL_30")
        self.assertIn("take_profit_40", post.sources)
        self.assertNotIn("take_profit_80", post.sources)

        # Same peak, but it was printed before the fill — live +29% holds.
        pre = analyze(
            1.29,
            recent_high=1.61,
            peak_at=(dca_at - timedelta(hours=5)).isoformat(),
        )
        self.assertEqual(pre.normalized_action, "HOLD")
        self.assertNotIn("take_profit_40", pre.sources)

        # A live tick through the 40 tier still sells without a post-DCA peak.
        live = analyze(
            1.41,
            recent_high=1.41,
            peak_at=(dca_at - timedelta(hours=5)).isoformat(),
        )
        self.assertIn("take_profit_40", live.sources)
        self.assertNotIn("take_profit_80", live.sources)

    def test_post_dca_peak_at_least_40_live_below_entry_holds(self):
        """A +40% post-DCA peak must not take profit once live price is below entry."""
        strategy = TechnicalRSIStrategy()
        dca_at = datetime.now() - timedelta(hours=1)
        params = {
            "take_profit_tiers": [40, 80, 120],
            "stop_loss_pct": 50,
        }
        coin = {"symbol": "龙虾/USDT", "timeframe": "1h", "strategy_params": params}
        market = MarketContext(
            symbol="龙虾/USDT",
            timeframe="1h",
            current_price=0.80,
            rsi=55.0,
            lower_bb=1.0,
            has_position=True,
            average_entry=1.0,
            strategy_params=params,
            sim_state={
                "rsi_sell_tiers_done": {},
                "last_rsi": 50.0,
                "dca_rounds": 1,
                "last_dca_at": dca_at.isoformat(),
                "recent_high": 1.41,
                "peak_at": (dca_at + timedelta(minutes=30)).isoformat(),
            },
        )
        result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "HOLD")
        self.assertNotIn("take_profit_40", result.sources)


if __name__ == "__main__":
    unittest.main()
