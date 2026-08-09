import unittest

from core.models import MarketContext
from strategies.market_structure import evaluate_market_structure_sells


class TestMarketStructure(unittest.TestCase):
    def _market(self, **kwargs):
        defaults = dict(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.636,
            rsi=68.4,
            lower_bb=0.5,
            middle_bb=0.55,
            upper_bb=0.627,
            atr_pct=49.0,
            vol_multiplier=0.69,
            has_position=True,
            average_entry=0.28,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def test_bb_upper_triggers_sell_30(self):
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.636}
        params = {
            "bb_sell_enabled": True,
            "bb_sell_upper_ratio": 0.99,
            "bb_sell_rsi_min": 62,
            "vol_exhaustion_sell_enabled": False,
            "vol_dump_sell_enabled": False,
        }
        cands = evaluate_market_structure_sells(self._market(), params, pos)
        sources = [c.source for c in cands]
        self.assertIn("bb_upper", sources)

    def test_bb_upper_blocked_below_min_gain(self):
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.3036}
        market = self._market(
            current_price=0.3036,
            rsi=65.0,
            upper_bb=0.305,
            average_entry=0.3036,
        )
        params = {
            "bb_sell_enabled": True,
            "bb_sell_upper_ratio": 0.99,
            "bb_sell_rsi_min": 58,
            "bb_sell_min_gain_pct": 12,
            "vol_exhaustion_sell_enabled": False,
            "vol_dump_sell_enabled": False,
        }
        cands = evaluate_market_structure_sells(market, params, pos)
        self.assertEqual(cands, [])

    def test_bb_upper_stable_floor_blocks_tao_micro_gain(self):
        """stable_altcoin bb_sell_min_gain_pct=2 blocks +0.1% BB full-noise (TAO case)."""
        entry = 208.88
        px = 209.01  # ~+0.06%
        pos = {"rsi_sell_tiers_done": {}, "recent_high": px}
        market = self._market(
            symbol="TAO/USDT",
            current_price=px,
            rsi=76.0,
            upper_bb=206.77,
            average_entry=entry,
        )
        params = {
            "bb_sell_enabled": True,
            "bb_sell_upper_ratio": 0.99,
            "bb_sell_rsi_min": 62,
            "bb_sell_min_gain_pct": 2,
            "bb_sell_requires_ta": False,
            "vol_exhaustion_sell_enabled": False,
            "vol_dump_sell_enabled": False,
        }
        cands = evaluate_market_structure_sells(market, params, pos)
        self.assertEqual(cands, [])
        # Above 2% floor → still allowed
        market2 = self._market(
            symbol="TAO/USDT",
            current_price=entry * 1.03,
            rsi=76.0,
            upper_bb=entry * 1.01,
            average_entry=entry,
        )
        pos2 = {"rsi_sell_tiers_done": {}, "recent_high": entry * 1.03}
        cands2 = evaluate_market_structure_sells(market2, params, pos2)
        self.assertTrue(any(c.source == "bb_upper" for c in cands2))

    def test_bb_upper_requires_ta_when_configured(self):
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.636}
        params = {
            "bb_sell_enabled": True,
            "bb_sell_upper_ratio": 0.99,
            "bb_sell_rsi_min": 62,
            "bb_sell_requires_ta": True,
            "vol_exhaustion_sell_enabled": False,
            "vol_dump_sell_enabled": False,
        }
        cands = evaluate_market_structure_sells(self._market(), params, pos, ta_bearish=False)
        self.assertEqual(cands, [])
        cands_ta = evaluate_market_structure_sells(self._market(), params, pos, ta_bearish=True)
        self.assertTrue(any(c.source == "bb_upper" for c in cands_ta))

    def test_vol_exhaustion_at_peak(self):
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.636}
        params = {
            "bb_sell_enabled": False,
            "vol_exhaustion_sell_enabled": True,
            "vol_exhaustion_max": 0.75,
            "vol_exhaustion_rsi_min": 60,
            "vol_exhaustion_min_gain_pct": 25,
            "vol_dump_sell_enabled": False,
        }
        cands = evaluate_market_structure_sells(self._market(), params, pos)
        self.assertTrue(any(c.source == "vol_exhaustion" for c in cands))

    def test_no_signal_without_position(self):
        market = self._market(has_position=False, average_entry=0)
        cands = evaluate_market_structure_sells(market, {}, {})
        self.assertEqual(cands, [])

    def test_stg_low_rsi_no_vol_exhaustion(self):
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.26}
        market = self._market(
            symbol="STG/USDT",
            current_price=0.25,
            rsi=35.0,
            vol_multiplier=1.2,
            average_entry=0.24,
        )
        params = {
            "bb_sell_enabled": False,
            "vol_exhaustion_sell_enabled": True,
            "vol_exhaustion_max": 0.75,
            "vol_exhaustion_rsi_min": 60,
            "vol_exhaustion_min_gain_pct": 25,
            "vol_dump_sell_enabled": False,
        }
        cands = evaluate_market_structure_sells(market, params, pos)
        self.assertEqual(cands, [])