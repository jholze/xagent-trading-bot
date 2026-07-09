import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.eval_queue import (
    PRIORITY_ENTRY_15M,
    PRIORITY_POSITION_HEARTBEAT,
    PRIORITY_WEBHOOK,
    enqueue_eval,
    eval_queue_enabled,
    peek_eval_queue,
    pop_eval_batch,
    queue_depth,
)
from services.eval_queue_runtime import (
    enqueue_webhook_eval,
    reset_eval_runtime_for_tests,
    seed_meta_producers,
    worker_stats,
)


def _mock_redis_client():
    zset: dict[str, float] = {}
    hashes: dict[str, dict[str, str]] = {}

    client = MagicMock()

    def _zadd(key, mapping):
        zset.update(mapping)

    def _zpopmin(key, count=1):
        if not zset:
            return []
        items = sorted(zset.items(), key=lambda x: x[1])[:count]
        out = []
        for member, score in items:
            del zset[member]
            out.append((member, score))
        return out

    def _zcard(key):
        return len(zset)

    def _zrange(key, start, end, withscores=False):
        items = sorted(zset.items(), key=lambda x: x[1])
        sliced = items[start : end + 1]
        return sliced if withscores else [m for m, _ in sliced]

    def _zscore(key, member):
        return zset.get(member)

    def _zremrangebyrank(key, start, end):
        items = sorted(zset.items(), key=lambda x: x[1])
        for member, _ in items[start:]:
            zset.pop(member, None)

    def _hget(hash_key, field):
        return hashes.get(hash_key, {}).get(field)

    def _hset(hash_key, field, value):
        hashes.setdefault(hash_key, {})[field] = value

    client.zadd.side_effect = _zadd
    client.zpopmin.side_effect = _zpopmin
    client.zcard.side_effect = _zcard
    client.zrange.side_effect = _zrange
    client.zscore.side_effect = _zscore
    client.zremrangebyrank.side_effect = _zremrangebyrank
    client.hget.side_effect = _hget
    client.hset.side_effect = _hset
    client.pipeline.return_value = client
    client.execute.return_value = True
    return client, zset, hashes


CFG = {
    "architecture": {
        "eval_queue_enabled": True,
        "key_prefix": "test:",
        "eval_debounce_sec": 45,
        "eval_meta_interval_sec": 0,
        "eval_position_heartbeat_sec": 300,
        "eval_stale_sec": 7200,
    }
}


class TestEvalQueue(unittest.TestCase):
    def setUp(self):
        reset_eval_runtime_for_tests()

    def test_disabled_when_flag_off(self):
        cfg = {"architecture": {"eval_queue_enabled": False}}
        self.assertFalse(eval_queue_enabled(cfg))
        self.assertFalse(enqueue_eval("BTC/USDT", "4h", reason="x", priority=0, config_raw=cfg))

    def test_enqueue_respects_priority_order(self):
        client, zset, _ = _mock_redis_client()
        with patch("bus.eval_queue._client", return_value=client):
            self.assertTrue(
                enqueue_eval("ETH/USDT", "4h", reason="stale", priority=50, config_raw=CFG)
            )
            self.assertTrue(
                enqueue_eval("BTC/USDT", "4h", reason="webhook", priority=PRIORITY_WEBHOOK, config_raw=CFG)
            )
            jobs = pop_eval_batch(2, config_raw=CFG)
        self.assertEqual(jobs[0].symbol, "BTC/USDT")
        self.assertEqual(jobs[0].priority, PRIORITY_WEBHOOK)
        self.assertEqual(jobs[1].symbol, "ETH/USDT")

    def test_debounce_blocks_duplicate_lower_priority(self):
        client, _, _ = _mock_redis_client()
        with patch("bus.eval_queue._client", return_value=client):
            self.assertTrue(
                enqueue_eval("SOL/USDT", "4h", reason="heartbeat", priority=PRIORITY_POSITION_HEARTBEAT, config_raw=CFG)
            )
            blocked = enqueue_eval(
                "SOL/USDT", "4h", reason="stale", priority=50, config_raw=CFG,
            )
        self.assertFalse(blocked)

    def test_force_bypasses_debounce(self):
        client, _, _ = _mock_redis_client()
        with patch("bus.eval_queue._client", return_value=client):
            enqueue_eval("DOGE/USDT", "4h", reason="heartbeat", priority=30, config_raw=CFG)
            self.assertTrue(
                enqueue_eval(
                    "DOGE/USDT", "4h", reason="webhook", priority=PRIORITY_WEBHOOK,
                    config_raw=CFG, force=True,
                )
            )
            jobs = pop_eval_batch(1, config_raw=CFG)
        self.assertEqual(jobs[0].reason, "webhook")

    def test_queue_depth_and_peek(self):
        client, _, _ = _mock_redis_client()
        with patch("bus.eval_queue._client", return_value=client):
            enqueue_eval("A/USDT", "4h", reason="a", priority=40, config_raw=CFG)
            enqueue_eval("B/USDT", "4h", reason="b", priority=15, config_raw=CFG)
            self.assertEqual(queue_depth(CFG), 2)
            peek = peek_eval_queue(2, config_raw=CFG)
        self.assertEqual(peek[0]["symbol"], "B/USDT")
        self.assertEqual(peek[0]["priority"], PRIORITY_ENTRY_15M)

    def test_webhook_enqueue_helper(self):
        client, _, _ = _mock_redis_client()
        with patch("bus.eval_queue._client", return_value=client):
            self.assertTrue(enqueue_webhook_eval("RAVE/USDT", "4h", config_raw=CFG))
            self.assertEqual(queue_depth(CFG), 1)


class TestEvalMetaProducers(unittest.TestCase):
    def setUp(self):
        reset_eval_runtime_for_tests()

    def test_seed_enqueues_positions_and_stale(self):
        client, zset, _ = _mock_redis_client()
        watchlist = [
            {"symbol": "BTC/USDT", "timeframe": "4h", "active": True},
            {"symbol": "ETH/USDT", "timeframe": "4h", "active": True},
        ]
        open_positions = [{"symbol": "BTC/USDT", "timeframe": "4h", "amount": 1.0}]
        with patch("bus.eval_queue._client", return_value=client), \
             patch("bus.eval_queue.last_processed_at", return_value=None), \
             patch("strategies.watch_15m_state.list_watched", return_value=[]):
            counts = seed_meta_producers(
                watchlist=watchlist,
                open_positions=open_positions,
                config_raw=CFG,
            )
            self.assertGreaterEqual(counts.get("positions", 0), 1)
            self.assertGreaterEqual(counts.get("stale", 0), 1)
            self.assertGreaterEqual(queue_depth(CFG), 2)

    def test_worker_stats_reports_depth(self):
        client, _, _ = _mock_redis_client()
        with patch("bus.eval_queue._client", return_value=client):
            enqueue_eval("X/USDT", "4h", reason="test", priority=40, config_raw=CFG)
            stats = worker_stats()
        self.assertEqual(stats["queue_depth"], 1)
        self.assertFalse(stats["running"])


if __name__ == "__main__":
    unittest.main()