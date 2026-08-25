import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.eval_queue import PRIORITY_SOCIAL, enqueue_eval, queue_depth
from services.eval_queue_runtime import reset_eval_runtime_for_tests, seed_meta_producers
from services.social_pipeline import SocialPipeline
from x_analyzer import XAnalyzer, XSignal
from x_data_provider import RawPost


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
        for member, _ in items:
            del zset[member]
        return items

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
    return client, zset


class TestSignalArenaOptimizations(unittest.TestCase):
    def test_process_new_posts_batches_prices(self):
        analyzer = MagicMock()
        analyzer.accounts = ["acc1"]
        analyzer.get_trust_score.return_value = 80
        analyzer.parse_tweets_batch.return_value = {
            "p1": XSignal("acc1", "BTC", "BUY", 80, post_id="p1"),
            "p2": XSignal("acc1", "ETH", "BUY", 75, post_id="p2"),
        }
        analyzer.track_and_recommend.return_value = {"recommended": False, "action": "IGNORE"}

        pipeline = SocialPipeline(analyzer)
        pipeline._perf = {"defer_ingest_eval": True}
        pipeline.provider.fetch_new_posts = MagicMock(
            return_value=[
                RawPost("p1", "acc1", "buy btc"),
                RawPost("p2", "acc1", "buy eth"),
            ]
        )
        pipeline._already_logged = MagicMock(return_value=False)

        with patch("services.social_pipeline.get_prices_batch") as mock_batch:
            mock_batch.return_value = {"BTC/USDT": 100.0, "ETH/USDT": 50.0}
            with patch("services.social_pipeline.send_x_recommendation_message"), \
                 patch("services.social_pipeline.add_coin"):
                pipeline.process_new_posts()

        mock_batch.assert_called_once()
        symbols = mock_batch.call_args[0][0]
        self.assertEqual(set(symbols), {"BTC/USDT", "ETH/USDT"})
        self.assertEqual(analyzer.track_and_recommend.call_count, 2)
        for call in analyzer.track_and_recommend.call_args_list:
            self.assertTrue(call.kwargs.get("defer_eval"))

    def test_track_and_recommend_skips_eval_when_deferred(self):
        analyzer = XAnalyzer.__new__(XAnalyzer)
        analyzer.min_confidence = 50
        analyzer.effective_confidence_threshold = lambda _account: 50
        signal = XSignal("acc", "BTC", "BUY", 80, post_id="p1")
        signal.trust_score = 80
        signal.effective_confidence = 64

        with patch.object(XAnalyzer, "get_trust_score", return_value=80), \
             patch("x_analyzer.load_effective_watchlist", return_value=[{"symbol": "BTC/USDT"}]), \
             patch("strategies.decision_engine.DecisionEngine.evaluate") as mock_eval:
            rec = analyzer.track_and_recommend(
                "buy btc",
                "acc",
                current_price=100.0,
                signal=signal,
                defer_eval=True,
            )
        mock_eval.assert_not_called()
        self.assertEqual(rec["action"], "IGNORE")

    def test_seed_meta_social_only_watchlist_coins(self):
        reset_eval_runtime_for_tests()
        client, zset = _mock_redis_client()

        class _Sig:
            def __init__(self, coin, confidence, eff=None):
                self.coin = coin
                self.confidence = confidence
                self.effective_confidence = eff if eff is not None else confidence

        watchlist = [{"symbol": "BTC/USDT", "timeframe": "4h", "active": True}]
        x_signals = [_Sig("BTC", 80, 75), _Sig("LAB", 85, 80)]
        cmc_signals = [_Sig("DRV", 70), _Sig("BTC", 65)]

        with patch("bus.eval_queue._client", return_value=client), \
             patch("bus.eval_queue.last_processed_at", return_value=None), \
             patch("strategies.watch_15m_state.list_watched", return_value=[]):
            counts = seed_meta_producers(
                watchlist=watchlist,
                open_positions=[],
                x_signals=x_signals,
                cmc_signals=cmc_signals,
                lc_signals=[],
                config_raw=CFG,
            )

        self.assertEqual(counts.get("social", 0), 1)
        queued = set(zset.keys())
        self.assertTrue(any("BTC/USDT" in m for m in queued))
        self.assertFalse(any("LAB/USDT" in m for m in queued))
        self.assertFalse(any("DRV/USDT" in m for m in queued))

    def test_load_lc_signals_uses_mtime_cache(self):
        import data_manager as dm

        dm._lc_signals_cache["mtime"] = 0.0
        dm._lc_signals_cache["data"] = {"signals": []}

        path = dm.get_data_file(dm.LC_SIGNALS_FILE)
        dm.save_lc_signals({"signals": [{"coin": "BTC", "confidence": 70}]})
        first = dm.load_lc_signals()
        mtime = os.path.getmtime(path)
        dm._lc_signals_cache["mtime"] = mtime

        with patch("builtins.open", side_effect=AssertionError("should use cache")):
            second = dm.load_lc_signals()

        self.assertEqual(first, second)
        self.assertEqual(second["signals"][0]["coin"], "BTC")


if __name__ == "__main__":
    unittest.main()