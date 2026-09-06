"""#305 slice 2: Telegram delivery lanes, 429 retry, TradeResult.code."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bus.notifications import (
    RETRY_BUFFER_MAX,
    TelegramRateLimited,
    NotificationPublisher,
    compute_retry_wait,
    notification_publisher,
    reset_notification_publisher_for_tests,
)
from bus.schemas import PRIORITY_CYCLE, PRIORITY_URGENT, NotificationMessage
from core.models import RiskDecision, TradeOrder, TradeResult
from notifications.user_explain import explain_risk
from telegram_notifier import (
    _send_telegram_for_publisher,
    send_signal_message,
    send_telegram_message,
)


class _FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self._t = float(t)
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _http_response(status: int, payload: dict, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    resp.headers = headers or {}
    return resp


class TestRetryBackoff(unittest.TestCase):
    def test_honours_retry_after_with_exponential_cap(self):
        self.assertEqual(compute_retry_wait(1, 2), 2.0)
        self.assertEqual(compute_retry_wait(2, 2), 4.0)
        self.assertEqual(compute_retry_wait(3, 2), 8.0)
        self.assertEqual(compute_retry_wait(10, 2), 60.0)


class TestUrgentLaneRateLimit(unittest.TestCase):
    def test_burst_of_five_never_exceeds_one_per_second(self):
        clock = _FakeClock()
        pub = NotificationPublisher(
            rate_limit_sec=1.0,
            urgent_rate_limit_sec=1.0,
            clock=clock.time,
            sleeper=clock.sleep,
        )
        sent_at: list[float] = []

        def capture(text, **kwargs):
            sent_at.append(clock.time())
            return True

        pub.start(capture)
        try:
            for i in range(5):
                pub.enqueue(f"fill-{i}", priority=PRIORITY_URGENT)
            self.assertTrue(_wait_until(lambda: len(sent_at) >= 5), sent_at)
        finally:
            pub.stop(persist=False)

        self.assertEqual(len(sent_at), 5)
        gaps = [sent_at[i] - sent_at[i - 1] for i in range(1, 5)]
        self.assertTrue(all(g >= 1.0 - 1e-9 for g in gaps), gaps)


class TestRetryBufferBound(unittest.TestCase):
    def test_oldest_dropped_at_200(self):
        pub = NotificationPublisher(rate_limit_sec=0)
        for i in range(RETRY_BUFFER_MAX + 1):
            pub._buffer_retry(
                NotificationMessage(text=f"alert-{i}", priority=PRIORITY_URGENT),
                attempts=1,
                wait=1.0,
            )
        self.assertEqual(pub.retry_buffer_depth(), RETRY_BUFFER_MAX)
        texts = [e["msg"].text for e in pub._retry]
        self.assertNotIn("alert-0", texts)
        self.assertIn("alert-1", texts)
        self.assertIn(f"alert-{RETRY_BUFFER_MAX}", texts)

        path = Path(__import__("data_manager").resolve_data_path("telegram_retry_buffer.json"))
        self.assertTrue(path.is_file(), path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload.get("entries") or []), RETRY_BUFFER_MAX)


class TestPublisherFallback(unittest.TestCase):
    def test_publisher_not_running_uses_direct_send(self):
        reset_notification_publisher_for_tests()
        self.assertFalse(notification_publisher.running)
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "111"}
        ok_resp = _http_response(200, {"ok": True})
        with patch.dict(os.environ, env, clear=False), \
             patch("telegram_notifier.requests.post", return_value=ok_resp) as mock_post, \
             patch("telegram_notifier.message_prefix", return_value=""), \
             patch("telegram_notifier._headless_tenant_tag", return_value=""):
            ok = send_telegram_message("direct-fallback")
        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        self.assertIn("direct-fallback", payload["text"])


class Test429Retry(unittest.TestCase):
    def tearDown(self):
        reset_notification_publisher_for_tests()

    def test_retry_after_two_seconds_resends_not_dropped(self):
        clock = _FakeClock()
        reset_notification_publisher_for_tests()
        notification_publisher._clock = clock.time
        notification_publisher._sleep = clock.sleep
        notification_publisher._rate_limit_sec = 0.0
        notification_publisher._urgent_rate_limit_sec = 0.0

        limited = _http_response(
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 2",
                "parameters": {"retry_after": 2},
            },
        )
        ok_resp = _http_response(200, {"ok": True})
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "111"}

        with patch.dict(os.environ, env, clear=False), \
             patch("telegram_notifier.requests.post", side_effect=[limited, ok_resp]) as mock_post, \
             patch("telegram_notifier.message_prefix", return_value=""), \
             patch("telegram_notifier._headless_tenant_tag", return_value=""):
            notification_publisher.start(_send_telegram_for_publisher)
            ok = send_telegram_message("stop-loss filled")
            self.assertTrue(ok)
            self.assertTrue(
                _wait_until(lambda: mock_post.call_count >= 2),
                f"posts={mock_post.call_count}",
            )

        self.assertEqual(mock_post.call_count, 2)
        first = mock_post.call_args_list[0].kwargs.get("json") or mock_post.call_args_list[0][1]["json"]
        second = mock_post.call_args_list[1].kwargs.get("json") or mock_post.call_args_list[1][1]["json"]
        self.assertIn("stop-loss filled", first["text"])
        self.assertIn("stop-loss filled", second["text"])
        self.assertEqual(notification_publisher.retry_buffer_depth(), 0)


class TestRetryBufferRestart(unittest.TestCase):
    def test_pending_alert_survives_restart(self):
        clock = _FakeClock()
        pub = NotificationPublisher(
            rate_limit_sec=0,
            clock=clock.time,
            sleeper=clock.sleep,
        )
        pub._buffer_retry(
            NotificationMessage(text="restart-alert", priority=PRIORITY_URGENT),
            attempts=1,
            wait=0.0,
        )
        pub.drain()
        self.assertEqual(pub.retry_buffer_depth(), 0)

        sent: list[str] = []

        def capture(text, **kwargs):
            sent.append(text)
            return True

        pub.start(capture)
        try:
            self.assertTrue(_wait_until(lambda: "restart-alert" in sent), sent)
        finally:
            pub.stop(persist=False)


class TestRiskCodeOnTradeResult(unittest.TestCase):
    def test_max_daily_trades_renders_code_explanation_not_substring(self):
        english = "Daily buy limit reached (5/5)"
        code_text = explain_risk(english, code="max_daily_trades")
        fallback = explain_risk(english, code="")
        self.assertIn("Tageslimit für Käufe", code_text)
        self.assertNotEqual(code_text, fallback)
        self.assertEqual(fallback, english)

        coin = {"symbol": "ARIA/USDT", "name": "Aria AI"}
        result = TradeResult(
            False,
            "BUY",
            "ARIA/USDT",
            message=english,
            code="max_daily_trades",
        )
        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("telegram_notifier.send_telegram_message") as mock_send:
            mock_send.return_value = True
            send_signal_message(
                "BUY",
                coin,
                0.05,
                40.0,
                0.04,
                1.2,
                "🟢",
                "Bullish",
                executed=False,
                trade_message=english,
                trade_result=result,
            )
        self.assertTrue(mock_send.called)
        text = mock_send.call_args[0][0]
        self.assertIn("Tageslimit für Käufe", text)
        self.assertNotIn(english, text)

    def test_trading_service_copies_risk_decision_code(self):
        from services.trading_service import TradingService

        order = TradeOrder(type="BUY", symbol="ARIA/USDT", price=1.0, usdt_amount=10.0)
        decision = RiskDecision(
            approved=False,
            message="Daily buy limit reached (5/5)",
            code="max_daily_trades",
            order=order,
        )
        svc = TradingService()
        with patch.object(svc, "refresh", return_value=svc), \
             patch.object(svc, "can_execute", return_value=(True, "")), \
             patch.object(svc.risk, "evaluate", return_value=decision), \
             patch("services.trading_service.OrderService") as os_cls, \
             patch("strategies.positions.bind_buy_timeframe", return_value="4h"):
            ledger = os_cls.return_value
            ledger.find_by_idempotency_key.return_value = None
            result = svc._execute_order_locked(order)
        self.assertFalse(result.executed)
        self.assertEqual(result.code, "max_daily_trades")
        self.assertEqual(result.message, decision.message)


class TestTelegramRateLimitedRaised(unittest.TestCase):
    def test_publisher_send_raises_on_429(self):
        limited = _http_response(
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 2}},
        )
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "111"}
        with patch.dict(os.environ, env, clear=False), \
             patch("telegram_notifier.requests.post", return_value=limited), \
             patch("telegram_notifier.message_prefix", return_value=""), \
             patch("telegram_notifier._headless_tenant_tag", return_value=""):
            with self.assertRaises(TelegramRateLimited) as ctx:
                _send_telegram_for_publisher("x")
        self.assertEqual(ctx.exception.retry_after, 2.0)


if __name__ == "__main__":
    unittest.main()


class Test429DoesNotStallUrgentLane(unittest.TestCase):
    def test_urgent_sent_after_retry_after_not_after_backoff(self):
        """A 429 on a normal message pauses the publisher for retry_after only.

        The rate-limited message itself retries via the buffer (2s, 4s, ...),
        but an urgent fill enqueued meanwhile must go out as soon as Telegram's
        retry_after has passed -- not after the message's growing backoff.
        """
        clock = _FakeClock()
        pub = NotificationPublisher(
            rate_limit_sec=0.0, urgent_rate_limit_sec=0.0, clock=clock.time, sleeper=clock.sleep
        )
        sent: list[tuple[str, float]] = []
        state = {"normal_attempts": 0}

        def send(text, **kwargs):
            if text == "normal":
                state["normal_attempts"] += 1
                if state["normal_attempts"] <= 3:
                    raise TelegramRateLimited(retry_after=2.0)
            sent.append((text, clock.time()))
            return True

        pub.start(send)
        try:
            pub.enqueue("normal", priority=PRIORITY_CYCLE)
            self.assertTrue(_wait_until(lambda: state["normal_attempts"] >= 1))
            t0 = clock.time()
            pub.enqueue("fill", priority=PRIORITY_URGENT)
            self.assertTrue(_wait_until(lambda: any(txt == "fill" for txt, _ in sent)), sent)
        finally:
            pub.stop(persist=False)
        fill_at = next(ts for txt, ts in sent if txt == "fill")
        # sent once the 2s pause is over, well before the message's 4s/8s backoff
        self.assertGreaterEqual(fill_at - t0, 2.0 - 1e-9)
        self.assertLess(fill_at - t0, 4.0)

