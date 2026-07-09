import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from aria_bot import app
from services.signal_webhook_service import process_signal_webhook
from strategies import watch_15m_state
from webhooks.adapters import parse_signal_payload
from webhooks.adapters.tradingview import parse_tradingview
from webhooks.auth import signal_webhook_token_ok
from webhooks.store import reset_for_tests


class TestSignalWebhookAdapters(unittest.TestCase):
    def test_generic_json_symbol(self):
        signal = parse_signal_payload(
            {"symbol": "VELVET", "event_type": "volume_spike", "strength": 0.8},
            source="generic",
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "VELVET/USDT")
        self.assertEqual(signal.event_type, "volume_spike")

    def test_tradingview_text(self):
        signal = parse_tradingview("VELVETUSDT VOLUME SPIKE")
        self.assertEqual(signal.symbol, "VELVET/USDT")
        self.assertEqual(signal.event_type, "volume_spike")


class TestSignalWebhookService(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        watch_15m_state.clear_all_watches_for_tests()

    def tearDown(self):
        reset_for_tests()
        watch_15m_state.clear_all_watches_for_tests()

    def test_process_sets_watch_with_priority(self):
        cfg = {
            "architecture": {
                "signal_webhook_enabled": True,
                "signal_webhook_rate_limit_per_min": 100,
            },
            "entry_sensor_15m": {
                "enabled": True,
                "webhook_priority_poll": True,
                "watch_ttl_hours": 24,
            },
        }
        with patch("webhooks.store.publish_redis", return_value=True):
            result = process_signal_webhook(
                {"symbol": "RAVE/USDT", "event_type": "volume_spike", "source": "tradingview"},
                source="tradingview",
                config_raw=cfg,
            )
        self.assertTrue(result.ok)
        self.assertTrue(result.watch_set)
        entry = watch_15m_state.get_watch_entry("RAVE/USDT")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.get("priority_poll"))
        self.assertTrue(watch_15m_state.consume_priority_poll("RAVE/USDT"))

    def test_token_required_when_configured(self):
        with patch.dict(os.environ, {"SIGNAL_WEBHOOK_TOKEN": "secret"}, clear=False):
            self.assertFalse(signal_webhook_token_ok("bad", {}))
            self.assertTrue(signal_webhook_token_ok("secret", {}))


class TestSignalWebhookRoute(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        watch_15m_state.reset_cache_for_tests()
        self.client = app.test_client()

    def tearDown(self):
        reset_for_tests()
        watch_15m_state.reset_cache_for_tests()

    def test_post_signal_webhook(self):
        with patch("services.signal_webhook_service.process_signal_webhook") as mock_proc, \
             patch("webhooks.auth.signal_webhook_token_ok", return_value=True), \
             patch("services.signal_webhook_service.signal_webhook_enabled", return_value=True):
            from services.signal_webhook_service import SignalWebhookResult
            from webhooks.schemas import ExternalSignal

            mock_proc.return_value = SignalWebhookResult(
                ok=True,
                signal=ExternalSignal(source="tradingview", symbol="VELVET/USDT", event_type="volume_spike"),
                watch_set=True,
                message="accepted",
                redis_published=True,
            )
            resp = self.client.post(
                "/api/signals/webhook?source=tradingview",
                json={"symbol": "VELVET", "event_type": "volume_spike"},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["watch_set"])


class TestEntrySensorPriorityPoll(unittest.TestCase):
    def setUp(self):
        watch_15m_state.reset_cache_for_tests()

    def test_consume_priority_poll_one_shot(self):
        watch_15m_state.set_watch("TEST/USDT", "4h", reason="webhook:tv", priority_poll=True)
        self.assertTrue(watch_15m_state.consume_priority_poll("TEST/USDT"))
        self.assertFalse(watch_15m_state.consume_priority_poll("TEST/USDT"))


if __name__ == "__main__":
    unittest.main()