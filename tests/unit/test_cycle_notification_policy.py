import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.cycle_notification_policy import (
    CycleNotificationPolicy,
    decision_fingerprint,
)


class TestCycleNotificationPolicy(unittest.TestCase):
    def _observability(self, **overrides):
        base = {
            "notify_on_cycle": True,
            "cycle_notifications": {
                "mode": "delta",
                "send_on_trade": True,
                "send_on_blocked": True,
                "send_on_nav_delta_pct": 0.5,
                "send_on_new_decision": True,
                "hold_explanation_max_per_cycle": 1,
                "hold_explanation_cooldown_hours": 6,
            },
        }
        base["cycle_notifications"].update(overrides)
        return base

    def test_decision_fingerprint_ignores_hold(self):
        fp = decision_fingerprint([
            {"symbol": "BTC/USDT", "normalized_action": "HOLD"},
            {"symbol": "SOL/USDT", "normalized_action": "BUY"},
        ])
        self.assertEqual(fp, "SOL/USDT:BUY")

    def test_delta_skip_quiet_cycle(self):
        policy = CycleNotificationPolicy(last_nav=100_000.0, last_decision_fingerprint="")
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.observability_config = self._observability()
        self.assertFalse(
            policy.should_send_summary(
                coin_results=[],
                total_value=100_100.0,
                config=mock_cfg,
            )
        )

    def test_delta_send_on_trade(self):
        policy = CycleNotificationPolicy(last_nav=100_000.0)
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.observability_config = self._observability()
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[{"symbol": "SOL/USDT", "executed": True, "normalized_action": "BUY"}],
                total_value=100_000.0,
                config=mock_cfg,
            )
        )

    def test_delta_send_on_nav_delta(self):
        policy = CycleNotificationPolicy(last_nav=100_000.0)
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.observability_config = self._observability()
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[],
                total_value=100_600.0,
                config=mock_cfg,
            )
        )

    def test_delta_send_on_new_decision(self):
        policy = CycleNotificationPolicy(last_nav=100_000.0, last_decision_fingerprint="")
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.observability_config = self._observability()
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[{"symbol": "ETH/USDT", "normalized_action": "SELL"}],
                total_value=100_000.0,
                config=mock_cfg,
            )
        )
        self.assertFalse(
            policy.should_send_summary(
                coin_results=[{"symbol": "ETH/USDT", "normalized_action": "SELL"}],
                total_value=100_000.0,
                config=mock_cfg,
            )
        )

    def test_mode_always_sends(self):
        policy = CycleNotificationPolicy()
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.observability_config = {
            "cycle_notifications": {"mode": "always"},
        }
        self.assertTrue(
            policy.should_send_summary(coin_results=[], total_value=0, config=mock_cfg)
        )

    def test_hold_flush_picks_strongest(self):
        policy = CycleNotificationPolicy()
        policy.offer_hold_explanation("A/USDT", "reason A", confidence=50)
        policy.offer_hold_explanation("B/USDT", "reason B", confidence=90)
        with patch("telegram_notifier.send_hold_explanation_message", return_value=True) as send:
            sent = policy.flush_hold_explanations()
        self.assertEqual(sent, 1)
        send.assert_called_once_with("B/USDT", "reason B", "")

    def test_hold_cooldown_blocks_repeat(self):
        from services.cycle_notification_policy import _reason_hash

        policy = CycleNotificationPolicy()
        policy.hold_cooldown[f"SOL/USDT:{_reason_hash('same reason')}"] = time.time()
        policy.offer_hold_explanation("SOL/USDT", "same reason", confidence=80)
        self.assertEqual(len(policy._hold_candidates), 0)


if __name__ == "__main__":
    unittest.main()