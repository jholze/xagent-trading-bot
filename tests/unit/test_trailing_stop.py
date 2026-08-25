import unittest

from core.models import MarketContext
from strategies.trailing_stop import (
    compute_stop_price,
    compute_trail_pct,
    evaluate_trailing_stop,
)


class TestTrailingStop(unittest.TestCase):
    def _params(self, **trail):
        base = {
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "trailing_stop": {
                "enabled": True,
                "mode": "live",
                "atr_multiplier": 2.0,
                "activation_gain_pct": 10,
                "min_trail_pct": 8,
                "max_trail_pct": 25,
                "arm_on_peak": True,
                "floor_at_entry": True,
                "be_buffer_pct": 0,
            },
        }
        base["trailing_stop"].update(trail)
        return base

    def test_compute_trail_pct_clamps(self):
        self.assertEqual(compute_trail_pct(2.0, self._params()), 8.0)
        self.assertEqual(compute_trail_pct(20.0, self._params()), 25.0)
        self.assertEqual(compute_trail_pct(10.0, self._params()), 20.0)

    def test_stop_price_floors_at_entry(self):
        # peak only +5%, trail 8% → raw under entry → floor at entry
        entry, peak = 100.0, 105.0
        stop = compute_stop_price(entry, peak, 8.0, floor_at_entry=True)
        self.assertEqual(stop, 100.0)
        stop_nofloor = compute_stop_price(entry, peak, 8.0, floor_at_entry=False)
        self.assertAlmostEqual(stop_nofloor, 96.6, places=1)

    def test_stop_price_above_entry_after_big_peak(self):
        # LAB-like: peak +19.3%, trail 10% → stop still green
        entry = 0.1458
        peak = entry * 1.193
        stop = compute_stop_price(entry, peak, 10.0, floor_at_entry=True)
        self.assertGreater(stop, entry)
        self.assertAlmostEqual(stop, peak * 0.9, places=6)

    def test_no_trigger_below_activation_gain(self):
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=1.05,
            has_position=True,
            average_entry=1.0,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.10}  # peak +10% = activation edge, drop 4.5% < trail
        self.assertIsNone(evaluate_trailing_stop(market, pos, self._params()))

    def test_triggers_on_drop_from_recent_high(self):
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.95,
            has_position=True,
            average_entry=0.85,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.2}
        cand = evaluate_trailing_stop(market, pos, self._params())
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "trailing_stop")
        self.assertIn("Trail", cand.rationale)

    def test_stable_profile_uses_trailing_when_configured(self):
        market = MarketContext(
            symbol="BTC/USDT",
            timeframe="4h",
            current_price=100,
            has_position=True,
            average_entry=90,
            atr_pct=2.0,
        )
        params = self._params()
        params["strategy_profile"] = "hermes_baseline+stable_sell"
        params["volatility_tier"] = "stable"
        params["trailing_stop"]["activation_gain_pct"] = 5
        result = evaluate_trailing_stop(market, {"recent_high": 110}, params)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "trailing_stop")

    def test_skips_profile_without_trailing_config(self):
        market = MarketContext(
            symbol="BTC/USDT",
            timeframe="4h",
            current_price=100,
            has_position=True,
            average_entry=90,
            atr_pct=2.0,
        )
        params = {"strategy_profile": "hermes_baseline", "volatility_tier": "stable"}
        self.assertIsNone(evaluate_trailing_stop(market, {"recent_high": 110}, params))

    def test_shadow_mode_flag(self):
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.95,
            has_position=True,
            average_entry=0.85,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.2}
        cand = evaluate_trailing_stop(market, pos, self._params(mode="shadow"))
        self.assertTrue(cand.shadow_only)

    def test_arm_on_peak_survives_giveback_to_entry(self):
        """IDOL-class: peak +10%, price ~entry → still armed and can fire."""
        market = MarketContext(
            symbol="IDOL/USDT",
            timeframe="1h",
            current_price=0.01586,
            has_position=True,
            average_entry=0.01588,
            atr_pct=3.0,
        )
        pos = {"recent_high": 0.017508}
        cand = evaluate_trailing_stop(
            market,
            pos,
            self._params(
                activation_gain_pct=5,
                min_trail_pct=8,
                atr_multiplier=2.0,
                arm_on_peak=True,
            ),
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "trailing_stop")

    def test_lab_style_stop_level_is_green(self):
        """After peak +19%, stop sits above entry — continuous eval sells green."""
        entry = 0.1458
        peak = entry * 1.193
        stop = compute_stop_price(entry, peak, 10.0, floor_at_entry=True)
        # price just below stop but still green
        price = stop * 0.999
        market = MarketContext(
            symbol="LAB/USDT",
            timeframe="4h",
            current_price=price,
            has_position=True,
            average_entry=entry,
            atr_pct=5.0,  # trail clamp → 10%
        )
        cand = evaluate_trailing_stop(
            market,
            {"recent_high": peak},
            self._params(
                activation_gain_pct=5,
                min_trail_pct=8,
                max_trail_pct=25,
                atr_multiplier=2.0,
            ),
        )
        self.assertIsNotNone(cand)
        self.assertGreater(price, entry)

    def test_floor_at_entry_does_not_sell_crash_through(self):
        """Henry HANA 2026-08-20: stop at entry, price already -32% — trail must not dump.

        Paper fills at market, so a late eval below the floor realises a fat loser.
        Hard SL / DCA own that zone. Tiny BE wiggle (IDOL ~-0.1%) still fires.
        """
        entry = 0.031303
        market = MarketContext(
            symbol="HANA/USDT",
            timeframe="1h",
            current_price=0.021243,
            has_position=True,
            average_entry=entry,
            atr_pct=6.0,
        )
        pos = {"recent_high": entry * 1.124}  # peak +12.4% as in the live rationale
        cand = evaluate_trailing_stop(
            market,
            pos,
            self._params(activation_gain_pct=4, min_trail_pct=8, max_trail_pct=25),
        )
        self.assertIsNone(cand)

    def test_floor_at_entry_still_fires_green_giveback(self):
        """Peak +20%, price still +8% vs entry and below stop → trail sells (winner)."""
        entry = 1.0
        peak = 1.20
        market = MarketContext(
            symbol="WIN/USDT",
            timeframe="1h",
            current_price=1.08,
            has_position=True,
            average_entry=entry,
            atr_pct=4.0,
        )
        cand = evaluate_trailing_stop(
            market,
            {"recent_high": peak},
            self._params(activation_gain_pct=5, min_trail_pct=8),
        )
        self.assertIsNotNone(cand)
        self.assertGreater(market.current_price, entry)

    def test_no_fire_above_stop(self):
        entry = 100.0
        peak = 120.0  # +20%
        # trail 8% → stop 110.4
        market = MarketContext(
            symbol="X/USDT",
            timeframe="1h",
            current_price=112.0,
            has_position=True,
            average_entry=entry,
            atr_pct=3.0,
        )
        self.assertIsNone(
            evaluate_trailing_stop(
                market,
                {"recent_high": peak},
                self._params(activation_gain_pct=5, min_trail_pct=8),
            )
        )

    def test_grind_skips_be_trail_like_h_usdt(self):
        from strategies.oracle_climax import ClimaxDecision, MODE_GRIND

        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.95,
            has_position=True,
            average_entry=0.85,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.2}
        raw = {"sell_policy": {"oracle_climax": {"enabled": True}}}
        self.assertIsNone(
            evaluate_trailing_stop(
                market,
                pos,
                self._params(),
                climax_decision=ClimaxDecision(MODE_GRIND, ("grind",), {}),
                config_raw=raw,
            )
        )
        live = evaluate_trailing_stop(market, pos, self._params())
        self.assertIsNotNone(live)


if __name__ == "__main__":
    unittest.main()
