import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

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
                "send_on_blocked": False,
                "send_on_nav_delta_pct": 2.0,
                "send_on_new_decision": False,
                "min_interval_sec": 900,
                "heartbeat_sec": 3600,
                "hold_explanation_max_per_cycle": 0,
            },
        }
        base["cycle_notifications"].update(overrides)
        return base

    def _cfg(self, **overrides):
        mock_cfg = MagicMock()
        mock_cfg.observability_config = self._observability(**overrides)
        return mock_cfg

    def test_decision_fingerprint_ignores_hold(self):
        fp = decision_fingerprint([
            {"symbol": "BTC/USDT", "normalized_action": "HOLD"},
            {"symbol": "SOL/USDT", "normalized_action": "BUY"},
        ])
        self.assertEqual(fp, "SOL/USDT:BUY")

    def test_delta_skip_quiet_cycle(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_decision_fingerprint="",
            last_summary_at=time.time(),
            process_started_at=time.time(),
        )
        self.assertFalse(
            policy.should_send_summary(
                coin_results=[],
                total_value=100_100.0,
                config=self._cfg(),
            )
        )

    def test_delta_skip_blocked_by_default(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_summary_at=time.time(),
            process_started_at=time.time(),
        )
        self.assertFalse(
            policy.should_send_summary(
                coin_results=[{
                    "symbol": "TREE/USDT",
                    "executed": False,
                    "trade_message": "Cash floor: free $0",
                    "normalized_action": "BUY",
                }],
                total_value=100_000.0,
                config=self._cfg(),
            )
        )

    def test_delta_send_on_blocked_when_enabled(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_summary_at=0.0,
            process_started_at=time.time(),
        )
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[{
                    "symbol": "TREE/USDT",
                    "executed": False,
                    "trade_message": "Cash floor",
                    "normalized_action": "BUY",
                }],
                total_value=100_000.0,
                config=self._cfg(send_on_blocked=True, min_interval_sec=0),
            )
        )

    def test_delta_send_on_trade(self):
        policy = CycleNotificationPolicy(last_nav=100_000.0, last_summary_at=time.time())
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[{
                    "symbol": "SOL/USDT",
                    "executed": True,
                    "normalized_action": "BUY",
                }],
                total_value=100_000.0,
                config=self._cfg(),
            )
        )
        self.assertIn("trade", policy.last_summary_reason)

    def test_trade_bypasses_min_interval(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_summary_at=time.time() - 10,
        )
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[{"symbol": "SOL/USDT", "executed": True}],
                total_value=100_000.0,
                config=self._cfg(min_interval_sec=900),
            )
        )

    def test_nav_respects_min_interval(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_summary_at=time.time() - 10,
        )
        self.assertFalse(
            policy.should_send_summary(
                coin_results=[],
                total_value=103_000.0,
                config=self._cfg(send_on_nav_delta_pct=2.0, min_interval_sec=900),
            )
        )

    def test_delta_send_on_nav_delta(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_summary_at=0.0,
            process_started_at=time.time(),
        )
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[],
                total_value=102_500.0,
                config=self._cfg(min_interval_sec=0),
            )
        )

    def test_delta_send_on_new_decision_when_enabled(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_decision_fingerprint="",
            last_summary_at=0.0,
        )
        cfg = self._cfg(send_on_new_decision=True, min_interval_sec=0, heartbeat_sec=0)
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[{"symbol": "ETH/USDT", "normalized_action": "SELL"}],
                total_value=100_000.0,
                config=cfg,
            )
        )
        self.assertFalse(
            policy.should_send_summary(
                coin_results=[{"symbol": "ETH/USDT", "normalized_action": "SELL"}],
                total_value=100_000.0,
                config=cfg,
            )
        )

    def test_heartbeat_after_quiet_hour(self):
        policy = CycleNotificationPolicy(
            last_nav=100_000.0,
            last_summary_at=time.time() - 4000,
            process_started_at=time.time() - 8000,
        )
        self.assertTrue(
            policy.should_send_summary(
                coin_results=[],
                total_value=100_100.0,
                config=self._cfg(min_interval_sec=0, heartbeat_sec=3600),
            )
        )
        self.assertEqual(policy.last_summary_reason, "heartbeat")

    def test_mode_always_sends(self):
        policy = CycleNotificationPolicy()
        mock_cfg = MagicMock()
        mock_cfg.observability_config = {
            "cycle_notifications": {"mode": "always"},
        }
        self.assertTrue(
            policy.should_send_summary(coin_results=[], total_value=0, config=mock_cfg)
        )

    def test_hold_flush_disabled_by_default(self):
        policy = CycleNotificationPolicy()
        policy.offer_hold_explanation("A/USDT", "reason A", confidence=90, config=self._cfg())
        self.assertEqual(len(policy._hold_candidates), 0)

    def test_hold_flush_picks_strongest(self):
        policy = CycleNotificationPolicy()
        cfg = self._cfg(hold_explanation_max_per_cycle=1)
        policy.offer_hold_explanation("A/USDT", "reason A", confidence=50, config=cfg)
        policy.offer_hold_explanation("B/USDT", "reason B", confidence=90, config=cfg)
        with patch("telegram_notifier.send_hold_explanation_message", return_value=True) as send:
            sent = policy.flush_hold_explanations(config=cfg)
        self.assertEqual(sent, 1)
        send.assert_called_once_with("B/USDT", "reason B", "")

    def test_hold_cooldown_blocks_repeat(self):
        from services.cycle_notification_policy import _reason_hash

        policy = CycleNotificationPolicy()
        cfg = self._cfg(hold_explanation_max_per_cycle=1)
        policy.hold_cooldown[f"SOL/USDT:{_reason_hash('same reason')}"] = time.time()
        policy.offer_hold_explanation("SOL/USDT", "same reason", confidence=80, config=cfg)
        self.assertEqual(len(policy._hold_candidates), 0)

    def test_social_digest_rate_limit(self):
        policy = CycleNotificationPolicy()
        cfg = self._cfg(social_digest_min_interval_sec=1800)
        self.assertTrue(policy.should_send_social_digest(config=cfg))
        self.assertFalse(policy.should_send_social_digest(config=cfg))


if __name__ == "__main__":
    unittest.main()
