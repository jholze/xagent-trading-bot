import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.heartbeats import HeartbeatRegistry
from bus.notifications import NotificationPublisher
from bus.schemas import PRIORITY_CYCLE, PRIORITY_URGENT


class TestNotificationPublisher(unittest.TestCase):
    def test_enqueue_and_worker_delivers(self):
        pub = NotificationPublisher(rate_limit_sec=0)
        sent = []

        def capture(text, **kwargs):
            sent.append(text)
            return True

        pub.start(capture)
        pub.enqueue("urgent", priority=PRIORITY_URGENT)
        pub.enqueue("cycle", priority=PRIORITY_CYCLE)
        deadline = time.time() + 3
        while time.time() < deadline and len(sent) < 2:
            time.sleep(0.05)
        pub.stop()
        self.assertEqual(sent[0], "urgent")
        self.assertIn("cycle", sent)

    def test_priority_order(self):
        pub = NotificationPublisher(rate_limit_sec=0)
        order = []

        def capture(text, **kwargs):
            order.append(text)
            return True

        pub.enqueue("slow", priority=PRIORITY_CYCLE)
        pub.enqueue("fast", priority=PRIORITY_URGENT)
        pub.start(capture)
        deadline = time.time() + 3
        while time.time() < deadline and len(order) < 2:
            time.sleep(0.05)
        pub.stop()
        self.assertEqual(order[0], "fast")


class TestHeartbeats(unittest.TestCase):
    def test_beat_and_stale(self):
        reg = HeartbeatRegistry()
        reg.clear()
        reg.beat("test_worker", ttl_sec=1)
        self.assertIn("test_worker", reg.all_workers())
        time.sleep(1.2)
        self.assertIn("test_worker", reg.stale_workers(ttl_sec=1))


class TestArchitectureRuntime(unittest.TestCase):
    def test_ensure_started_idempotent(self):
        from services.architecture_runtime import ensure_started
        import services.architecture_runtime as rt

        rt._started = False
        rt._last_mode = None
        with patch("telegram_notifier._send_telegram_direct", return_value=True):
            ensure_started()
            ensure_started()
        self.assertTrue(rt._started)


class TestCommandSessions(unittest.TestCase):
    def setUp(self):
        from bus.sessions import session_manager

        session_manager.end()

    def test_heavy_session_blocks_second_start(self):
        from bus.sessions import session_manager

        s1 = session_manager.start("123", "backtest")
        self.assertIsNotNone(s1)
        s2 = session_manager.start("456", "testaccount")
        self.assertIsNone(s2)
        session_manager.end(session_id=s1.session_id)

    def test_defer_cycle_notifications_during_session(self):
        from bus.notifications import NotificationPublisher
        from bus.sessions import session_manager

        pub = NotificationPublisher(rate_limit_sec=0)
        sent = []

        def capture(text, **kwargs):
            sent.append(text)
            return True

        pub.start(capture)
        session_manager.start("1", "testaccount")
        pub.enqueue("deferred", priority=PRIORITY_CYCLE)
        pub.enqueue("urgent", priority=PRIORITY_URGENT)
        deadline = time.time() + 3
        while time.time() < deadline and len(sent) < 1:
            time.sleep(0.05)
        session_manager.end()
        pub.flush_deferred()
        deadline = time.time() + 3
        while time.time() < deadline and len(sent) < 2:
            time.sleep(0.05)
        pub.stop()
        self.assertEqual(sent[0], "urgent")
        self.assertIn("deferred", sent)
        self.assertEqual(pub.deferred_count(), 0)


class TestHeavyJobQueue(unittest.TestCase):
    def setUp(self):
        from bus.jobs import heavy_job_queue
        from bus.sessions import session_manager

        session_manager.end()
        heavy_job_queue.start()

    def test_enqueue_runs_job_and_clears_session(self):
        from bus.jobs import heavy_job_queue
        from bus.sessions import session_manager

        done = []

        def job():
            done.append(True)

        job_id, err = heavy_job_queue.enqueue("backtest", "99", job)
        self.assertIsNone(err)
        self.assertTrue(job_id)
        deadline = time.time() + 5
        while time.time() < deadline and not done:
            time.sleep(0.05)
        self.assertTrue(done)
        self.assertFalse(session_manager.has_heavy_session())


if __name__ == "__main__":
    unittest.main()