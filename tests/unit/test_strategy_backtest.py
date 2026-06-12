import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.strategy_backtest import (
    StrategyBacktester,
    classify_coin,
    detect_volume_profile,
)
from services.strategy_auto_tuner import StrategyAutoTuner
from services.strategy_review_scheduler import StrategyReviewScheduler


def _sample_bars(rows: int = 80) -> list:
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    bars = []
    price = 1.0
    for i in range(rows):
        ts = int((start + timedelta(hours=4 * i)).timestamp() * 1000)
        price *= 1 + np.sin(i / 5) * 0.02
        vol = 1000 + (500 if 10 <= i % 24 <= 16 else 100)
        bars.append([ts, price, price * 1.01, price * 0.99, price, vol])
    return bars


class TestStrategyBacktest(unittest.TestCase):
    def _config(self):
        return {
            "slippage_percent": 1.5,
            "strategy_backtest": {
                "days": 30,
                "min_improvement_pct": 10,
                "min_signals_for_valid": 2,
                "auto_apply": True,
                "post_apply_validation_hours": 36,
                "min_review_hours": 12,
                "max_review_hours": 336,
                "base_review_hours": {"meme": 24, "mid_cap": 48, "large_cap": 72, "default": 48},
                "us_market": {
                    "enabled": True,
                    "timezone": "UTC",
                    "open": "14:00",
                    "close": "21:00",
                    "prefer_review_after_close": False,
                },
                "guardrails": {
                    "rsi_buy_low": {"min": 20, "max": 35, "max_delta": 5},
                    "rsi_buy_high": {"min": 40, "max": 60, "max_delta": 5},
                    "volume_multiplier": {"min": 1.0, "max": 2.0, "max_delta": 0.3},
                },
            },
            "strategies": [
                {"symbol": "ARIA/USDT", "timeframe": "4h", "description": "Meme/High-Vol",
                 "rsi_buy_low": 28, "rsi_buy_high": 45, "volume_multiplier": 1.4},
            ],
        }

    def test_classify_coin_meme_and_large_cap(self):
        self.assertEqual(classify_coin("ARIA/USDT", {"description": "Meme/High-Vol"}), "meme")
        self.assertEqual(classify_coin("BTC/USDT", {}), "large_cap")

    def test_simulation_runs_on_mock_ohlcv(self):
        bt = StrategyBacktester(self._config(), ohlcv_fetcher=lambda s, t, d: _sample_bars())
        result = bt.run("ARIA/USDT", "4h", self._config()["strategies"][0])
        self.assertGreaterEqual(result.metrics.signal_churn, 0)
        self.assertIn("us_session_volume_ratio", result.metrics.to_dict())

    def test_compare_variants_returns_result(self):
        bt = StrategyBacktester(self._config(), ohlcv_fetcher=lambda s, t, d: _sample_bars())
        result = bt.compare_variants("ARIA/USDT", "4h", self._config()["strategies"][0])
        self.assertEqual(result.symbol, "ARIA/USDT")

    def test_volume_profile_detects_us_session(self):
        bars = _sample_bars()
        df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")
        profile = detect_volume_profile(df, {"enabled": True, "timezone": "UTC", "open": "14:00", "close": "21:00"})
        self.assertGreater(profile.us_open_spike, 0)

    def test_review_scheduler_post_apply_shorter_interval(self):
        bt = StrategyBacktester(self._config(), ohlcv_fetcher=lambda s, t, d: _sample_bars())
        result = bt.run("ARIA/USDT", "4h", self._config()["strategies"][0])
        sched = StrategyReviewScheduler(self._config())
        nxt, hours, reason = sched.compute_next_review(
            result, self._config()["strategies"][0], param_applied=True
        )
        self.assertLessEqual(hours, 36)
        self.assertIn("validation", reason)

    def test_auto_tuner_guardrails_clamp(self):
        tuner = StrategyAutoTuner(self._config())
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            import json
            with open(cfg_path, "w") as f:
                json.dump(self._config(), f)
            with patch("services.strategy_auto_tuner.get_config", return_value=self._config()), \
                 patch("services.strategy_auto_tuner.save_config", return_value=True), \
                 patch("services.strategy_auto_tuner.reload_config"):
                ok, applied, msg = tuner.apply(
                    "ARIA/USDT", "4h",
                    {"rsi_buy_high": 99, "volume_multiplier": 5.0},
                )
        self.assertTrue(ok or not applied)
        if applied:
            self.assertLessEqual(applied.get("rsi_buy_high", 60), 60)

    def test_worker_queue_picks_due_coin(self):
        from services.strategy_backtest_worker import StrategyBacktestWorker

        worker = StrategyBacktestWorker()
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        with patch("services.strategy_backtest_worker.list_strategy_targets", return_value=[
            {"symbol": "ARIA/USDT", "timeframe": "4h", "rsi_buy_low": 28, "rsi_buy_high": 45, "volume_multiplier": 1.4},
        ]), patch("services.strategy_backtest_worker.get_strategy_backtest_entry", return_value={
            "next_review_at": past,
        }):
            target = worker._next_due_target()
        self.assertIsNotNone(target)
        self.assertEqual(target[0], "ARIA/USDT")


if __name__ == "__main__":
    unittest.main()