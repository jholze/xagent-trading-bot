import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from hermes.churn_replay import analyze_churn
from hermes.replay_engine import ReplaySignal, run_signals, signals_from_orders


class TestChurnReplay(unittest.TestCase):
    def test_run_signals_orders_signals(self):
        orders = [
            {
                "symbol": "H/USDT",
                "side": "buy",
                "timestamps": {"filled": "2026-06-14T10:00:00"},
                "source": "auto",
            },
            {
                "symbol": "H/USDT",
                "side": "sell",
                "timestamps": {"filled": "2026-06-14T11:00:00"},
                "source": "auto",
            },
            {
                "symbol": "H/USDT",
                "side": "buy",
                "timestamps": {"filled": "2026-06-14T11:30:00"},
                "source": "auto",
            },
        ]
        signals = signals_from_orders(orders, symbol="H/USDT")
        self.assertEqual(len(signals), 3)
        result = run_signals(signals)
        self.assertEqual(result.metrics["n_signals"], 3)

    def test_analyze_churn_detects_fast_rebuy(self):
        now = datetime.now()
        orders = [
            {"symbol": "H/USDT", "side": "sell", "timestamps": {"filled": (now - timedelta(hours=2)).isoformat()}},
            {"symbol": "H/USDT", "side": "buy", "timestamps": {"filled": (now - timedelta(hours=1)).isoformat()}},
        ]

        class FakeSvc:
            def __init__(self, scope=None):
                pass

            def list_orders(self, **kwargs):
                return orders, 1

        with unittest.mock.patch("hermes.churn_replay.OrderService", FakeSvc):
            result = analyze_churn("H/USDT", min_hours_rebuy=4.0)

        self.assertEqual(result["blocked_rebuys"], 1)
        self.assertEqual(result["churn_pairs"], 1)


if __name__ == "__main__":
    unittest.main()