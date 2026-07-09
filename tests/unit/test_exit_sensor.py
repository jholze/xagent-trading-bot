import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import MarketContext
from services.market_service import MarketService
from strategies.exit_sensor import evaluate_exit_sensor_sells, exit_sensor_config


class TestExitSensorMetrics(unittest.TestCase):
    def _ohlcv_df(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_compute_exit_metrics_15m_detects_lower_high_and_close_below_ema(self):
        n = 25
        highs = [10.0] * (n - 3) + [12.0, 11.0, 10.5]
        closes = [10.0] * (n - 3) + [11.2, 10.5, 9.7]
        df = self._ohlcv_df(
            {
                "open": [h - 0.1 for h in closes],
                "high": highs,
                "low": [c - 0.2 for c in closes],
                "close": closes,
                "volume": [1000.0] * n,
            }
        )
        metrics = MarketService.compute_exit_metrics_15m(df, ema_period=20, vol_avg_period=20)
        self.assertIsNotNone(metrics)
        self.assertTrue(metrics["lower_high"])
        self.assertTrue(metrics["close_below_ema"])

    def test_compute_exit_metrics_15m_volume_climax_wick(self):
        n = 25
        df = self._ohlcv_df(
            {
                "open": [10.0] * (n - 1) + [10.0],
                "high": [10.5] * (n - 1) + [12.0],
                "low": [9.8] * n,
                "close": [10.2] * (n - 1) + [10.15],
                "volume": [1000.0] * (n - 1) + [4500.0],
            }
        )
        metrics = MarketService.compute_exit_metrics_15m(df, ema_period=20, vol_avg_period=20)
        self.assertIsNotNone(metrics)
        self.assertGreaterEqual(metrics["volume_spike_ratio"], 3.0)
        self.assertGreaterEqual(metrics["upper_wick_pct"], 50.0)
        self.assertLessEqual(metrics["body_atr_ratio"], 0.5)

    def test_compute_exit_metrics_1h_rsi_rollover(self):
        closes = list(np.linspace(10.0, 13.0, 30))
        closes[-6:] = [12.8, 12.9, 13.0, 12.4, 12.0, 11.6]
        df = self._ohlcv_df(
            {
                "open": closes,
                "high": [c + 0.2 for c in closes],
                "low": [c - 0.2 for c in closes],
                "close": closes,
                "volume": [500.0] * len(closes),
            }
        )
        metrics = MarketService.compute_exit_metrics_1h(df)
        self.assertIsNotNone(metrics)
        self.assertLess(metrics["rsi"], 60.0)
        self.assertGreaterEqual(metrics["rsi_peak_5"], 70.0)
        self.assertTrue(metrics["rsi_rollover"])


class TestExitSensorSells(unittest.TestCase):
    def _market(self, **kwargs):
        defaults = dict(
            symbol="LIT/USDT",
            timeframe="4h",
            current_price=1.095,
            rsi=58.0,
            lower_bb=0.9,
            middle_bb=1.0,
            upper_bb=1.1,
            atr_pct=8.0,
            vol_multiplier=1.1,
            has_position=True,
            average_entry=1.0,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def _cfg(self, **overrides):
        cfg = exit_sensor_config()
        cfg["enabled"] = True
        cfg["mode"] = "live"
        cfg.update(overrides)
        return cfg

    def test_weakness_15m_triggers_partial_sell_above_min_gain(self):
        pos = {"recent_high": 1.10, "rsi_sell_tiers_done": {}}
        metrics_15m = {
            "lower_high": True,
            "close_below_ema": True,
            "volume_spike_ratio": 1.2,
            "body_atr_ratio": 0.5,
            "vol_above_avg": False,
            "upper_wick_pct": 10.0,
        }
        cands = evaluate_exit_sensor_sells(
            self._market(current_price=1.095),
            pos,
            self._cfg(),
            metrics_15m=metrics_15m,
            metrics_1h=None,
            btc_rs_delta=None,
        )
        sources = [c.source for c in cands]
        self.assertIn("exit_15m_weakness", sources)

    def test_weakness_blocked_below_min_gain(self):
        pos = {"recent_high": 1.05, "rsi_sell_tiers_done": {}}
        metrics_15m = {"lower_high": True, "close_below_ema": True}
        cands = evaluate_exit_sensor_sells(
            self._market(current_price=1.04, average_entry=1.0),
            pos,
            self._cfg(),
            metrics_15m=metrics_15m,
            metrics_1h=None,
            btc_rs_delta=None,
        )
        self.assertEqual(cands, [])

    def test_volume_climax_near_recent_high(self):
        pos = {"recent_high": 1.10, "rsi_sell_tiers_done": {}}
        metrics_15m = {
            "lower_high": False,
            "close_below_ema": False,
            "volume_spike_ratio": 3.5,
            "upper_wick_pct": 60.0,
            "body_atr_ratio": 0.2,
            "vol_above_avg": True,
        }
        cands = evaluate_exit_sensor_sells(
            self._market(current_price=1.09),
            pos,
            self._cfg(),
            metrics_15m=metrics_15m,
            metrics_1h=None,
            btc_rs_delta=None,
        )
        self.assertIn("exit_volume_climax", [c.source for c in cands])

    def test_pullback_from_high_with_volume(self):
        pos = {"recent_high": 1.12, "rsi_sell_tiers_done": {}}
        metrics_15m = {
            "lower_high": False,
            "close_below_ema": False,
            "volume_spike_ratio": 1.3,
            "vol_above_avg": True,
            "body_atr_ratio": 0.4,
            "upper_wick_pct": 5.0,
        }
        cands = evaluate_exit_sensor_sells(
            self._market(current_price=1.08),
            pos,
            self._cfg(),
            metrics_15m=metrics_15m,
            metrics_1h=None,
            btc_rs_delta=None,
        )
        self.assertIn("exit_pullback", [c.source for c in cands])

    def test_btc_relative_strength_underperformance(self):
        pos = {"recent_high": 1.10, "rsi_sell_tiers_done": {}}
        cands = evaluate_exit_sensor_sells(
            self._market(current_price=1.09),
            pos,
            self._cfg(),
            metrics_15m=None,
            metrics_1h=None,
            btc_rs_delta=-3.0,
        )
        self.assertIn("exit_btc_rs", [c.source for c in cands])

    def test_1h_rsi_rollover_triggers_sell(self):
        pos = {"recent_high": 1.10, "rsi_sell_tiers_done": {}}
        metrics_1h = {"rsi": 55.0, "rsi_peak_5": 72.0, "rsi_rollover": True}
        cands = evaluate_exit_sensor_sells(
            self._market(current_price=1.09),
            pos,
            self._cfg(),
            metrics_15m=None,
            metrics_1h=metrics_1h,
            btc_rs_delta=None,
        )
        self.assertIn("exit_1h_rsi_rollover", [c.source for c in cands])


if __name__ == "__main__":
    unittest.main()