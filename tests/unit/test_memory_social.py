"""Epic #42: CMC + LunarCrush → Trading Memory — real path tests."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.memory.models import CoinProfile, TradeMemory
from intelligence.memory.social_ingest import (
    EVT_CMC_SOCIAL,
    EVT_CMC_TRENDING,
    EVT_LC_FADE,
    EVT_LC_SENTIMENT,
    EVT_LC_SPIKE,
    float_or,
    impact_from_action,
    ingest_social_signal,
    is_quotes_fallback,
    join_social_events_to_trades,
    normalize_symbol,
    reflect_social,
    sync_cmc_memory,
    sync_lc_memory,
    sync_social_memory,
)
from intelligence.memory.store import InMemoryMemoryStore, MemoryStore

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "memory_social"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class TestSocialPrimitives(unittest.TestCase):
    def test_normalize_symbol(self):
        self.assertEqual(normalize_symbol("ARIA"), "ARIA/USDT")
        self.assertEqual(normalize_symbol("sol/usdt"), "SOL/USDT")
        self.assertEqual(normalize_symbol("BTCUSDT"), "BTC/USDT")

    def test_impact_from_action(self):
        self.assertGreater(impact_from_action("BUY", 80), 0)
        self.assertLess(impact_from_action("SELL", 80), 0)
        self.assertEqual(impact_from_action("HOLD", 50), 0.0)

    def test_quotes_fallback_detection(self):
        self.assertTrue(
            is_quotes_fallback({"post_id": "cmc_quote_TRX_neutral_2026-07-01"})
        )
        self.assertFalse(
            is_quotes_fallback({"post_id": "cmc_community_ARIA_bull_1"})
        )

    def test_ingest_social_dedupe(self):
        store = InMemoryMemoryStore()
        a = ingest_social_signal(
            source="cmc",
            event_type=EVT_CMC_SOCIAL,
            symbol="ARIA",
            impact=0.4,
            description="test bull",
            stable_key="k1",
            store=store,
        )
        b = ingest_social_signal(
            source="cmc",
            event_type=EVT_CMC_SOCIAL,
            symbol="ARIA",
            impact=0.4,
            description="test bull",
            stable_key="k1",
            store=store,
        )
        self.assertEqual(a.event_id, b.event_id)
        self.assertEqual(len(store.list_events()), 1)

    def test_store_refuses_ledger(self):
        with self.assertRaises(RuntimeError):
            MemoryStore()._col("orders")


class TestCmcLcSync(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()
        self.cmc_posts = _load_fixture("cmc_posts.json")["posts"]
        self.lc_signals = _load_fixture("lc_signals.json")["signals"]
        self.cfg = {
            "memory": {
                "social": {
                    "cmc": {
                        "enabled": True,
                        "include_quotes_fallback": False,
                        "min_confidence": 60,
                        "trending": False,
                        "quote_extreme": False,
                        "max_events_per_cycle": 40,
                        "lookback_hours": 720,
                    },
                    "lunarcrush": {
                        "enabled": True,
                        "min_galaxy_delta": 8,
                        "sentiment_extreme_low": 35,
                        "sentiment_extreme_high": 75,
                        "min_confidence": 55,
                        "max_events_per_cycle": 40,
                        "lookback_hours": 720,
                    },
                    "join_window_hours": 48,
                    "reflect_social": True,
                }
            }
        }

    def test_cmc_sync_excludes_quotes_creates_events_and_features(self):
        out = sync_cmc_memory(
            self.store, config=self.cfg, posts=self.cmc_posts
        )
        self.assertGreaterEqual(out["cmc_events"], 2)
        events = self.store.list_events(limit=50)
        types = {e.event_type for e in events}
        self.assertIn(EVT_CMC_SOCIAL, types)
        self.assertIn(EVT_CMC_TRENDING, types)
        # quotes fallback must not create cmc_social for TRX quote post
        trx_social = [
            e
            for e in events
            if e.event_type == EVT_CMC_SOCIAL and "TRX" in " ".join(e.symbols)
        ]
        self.assertEqual(trx_social, [])
        prof = self.store.get_profile("ARIA/USDT")
        self.assertIsNotNone(prof)
        self.assertIn("cmc", prof.features or {})
        self.assertEqual(prof.entry_bias, "neutral")  # no soft_block from social alone

    def test_lc_sync_spike_and_fade(self):
        out = sync_lc_memory(
            self.store, config=self.cfg, signals=self.lc_signals
        )
        self.assertGreaterEqual(out["lc_events"], 1)
        events = self.store.list_events(limit=50)
        types = {e.event_type for e in events}
        self.assertTrue(types & {EVT_LC_SPIKE, EVT_LC_FADE})
        sol = self.store.get_profile("SOL/USDT")
        self.assertIsNotNone(sol)
        self.assertIn("lc", sol.features or {})

    def test_lc_empty_fail_open(self):
        out = sync_lc_memory(self.store, config=self.cfg, signals=[])
        self.assertEqual(out["lc_events"], 0)
        self.assertEqual(out["lc_features"], 0)

    def test_lc_sentiment_zero_is_extreme_not_default_50(self):
        """sentiment=0 must not become 50 via `x or 50` — emit extreme event + feature 0."""
        self.assertEqual(float_or(0, 50.0), 0.0)
        self.assertEqual(float_or(None, 50.0), 50.0)
        signals = [
            {
                "timestamp": "2026-07-15T15:00:00",
                "signal_id": "lc_DEAD_sent0",
                "coin": "DEAD",
                "action": "HOLD",
                "confidence": 40,
                "rationale": "Galaxy 40 (0), Sentiment 0%",
                "galaxy_score": 40.0,
                "galaxy_delta": 0.0,
                "alt_rank": 500,
                "sentiment": 0,  # legitimate zero
                "source": "lc",
            }
        ]
        out = sync_lc_memory(self.store, config=self.cfg, signals=signals)
        self.assertGreaterEqual(out["lc_events"], 1)
        events = self.store.list_events(limit=20)
        sent_ev = [
            e
            for e in events
            if e.event_type == EVT_LC_SENTIMENT and any("DEAD" in s for s in e.symbols)
        ]
        self.assertTrue(sent_ev, "sentiment=0 must trigger lc_sentiment_extreme")
        self.assertLess(sent_ev[0].impact_score, 0)
        prof = self.store.get_profile("DEAD/USDT")
        self.assertIsNotNone(prof)
        self.assertEqual(float((prof.features or {}).get("lc", {}).get("sentiment")), 0.0)

    def test_sync_social_memory_entry_point(self):
        with patch(
            "intelligence.memory.social_ingest._load_cmc_posts_combined",
            return_value=self.cmc_posts,
        ), patch(
            "intelligence.memory.social_ingest._load_lc_signals_combined",
            return_value=self.lc_signals,
        ):
            out = sync_social_memory(self.store, config=self.cfg)
        self.assertGreaterEqual(out.get("cmc_events", 0), 1)
        self.assertGreaterEqual(out.get("lc_events", 0), 1)

    def test_join_events_to_trades(self):
        sync_cmc_memory(self.store, config=self.cfg, posts=self.cmc_posts)
        self.store.upsert_trade(
            TradeMemory(
                trade_id="t1",
                symbol="ARIA/USDT",
                entry_time="2026-07-15T10:30:00Z",
                direction="sell",
                pnl_usdt=-12.0,
                outcome="loss",
                ledger_scope="demo",
            )
        )
        n = join_social_events_to_trades(self.store, config=self.cfg)
        self.assertGreaterEqual(n, 1)
        tr = self.store.list_trades(symbol="ARIA/USDT")[0]
        self.assertTrue(tr.related_event_ids)

    def test_reflect_social_hype_fade(self):
        sync_cmc_memory(self.store, config=self.cfg, posts=self.cmc_posts)
        ts = "2026-07-15T12:00:00Z"
        for i, pnl in enumerate([-10.0, -8.0, -5.0]):
            self.store.upsert_trade(
                TradeMemory(
                    trade_id=f"s{i}",
                    symbol="ARIA/USDT",
                    entry_time=ts,
                    direction="sell",
                    pnl_usdt=pnl,
                    outcome="loss",
                    ledger_scope="demo",
                )
            )
        self.store.upsert_profile(
            CoinProfile(
                symbol="ARIA/USDT",
                size_bias=1.0,
                entry_bias="neutral",
                rationale="init",
                ledger_scope="demo",
            )
        )
        with patch(
            "intelligence.memory.social_ingest.resolve_memory_scope",
            return_value="demo",
        ):
            out = reflect_social(self.store, config=self.cfg, min_samples=3)
        self.assertGreaterEqual(out["social_lessons"], 1)
        les = self.store.list_lessons(symbol="ARIA/USDT")
        self.assertTrue(any("hype" in (L.text or "").lower() or "hype_fade" in L.tags for L in les))


class TestDualWriteOnDuplicate(unittest.TestCase):
    def test_log_cmc_post_dual_writes_even_when_json_has_id(self):
        """If JSON already has post_id, still call append_social_feed (retry path)."""
        from data_manager import log_cmc_post

        class Sig:
            post_id = "cmc_dup_1"
            coin = "ARIA"
            action = "BUY"
            confidence = 70
            rationale = "dup test"
            votes_bullish = 10
            votes_bearish = 1
            quotes_fallback = False

        calls = []

        def capture(entry):
            calls.append(dict(entry))
            return True

        with patch("data_manager.load_cmc_posts", return_value={
            "posts": [{"post_id": "cmc_dup_1", "coin": "ARIA"}]
        }), patch("data_manager.save_cmc_posts", return_value=True), patch(
            "intelligence.memory.social_ingest.append_social_feed", side_effect=capture
        ):
            log_cmc_post(Sig())
            # second call: already in JSON — must still dual-write
            log_cmc_post(Sig())
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[-1].get("post_id"), "cmc_dup_1")

    def test_log_lc_signal_dual_writes_even_when_json_has_id(self):
        from data_manager import log_lc_signal

        class Sig:
            post_id = "lc_dup_1"
            coin = "SOL"
            action = "BUY"
            confidence = 65
            rationale = "Galaxy 70 (+10)"
            galaxy_score = 70
            alt_rank = 20
            sentiment = 0

        calls = []

        def capture(entry):
            calls.append(dict(entry))
            return True

        with patch("data_manager.load_lc_signals", return_value={
            "signals": [{"signal_id": "lc_dup_1", "coin": "SOL"}]
        }), patch("data_manager.save_lc_signals", return_value=True), patch(
            "intelligence.memory.social_ingest.append_social_feed", side_effect=capture
        ):
            log_lc_signal(Sig(), signal_id="lc_dup_1")
            log_lc_signal(Sig(), signal_id="lc_dup_1")
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(float(calls[-1].get("sentiment")), 0.0)


class TestServiceWiresSocial(unittest.TestCase):
    def test_run_memory_cycle_calls_social(self):
        from intelligence.memory import service as svc

        store = InMemoryMemoryStore()
        cmc_posts = _load_fixture("cmc_posts.json")["posts"]
        lc_signals = _load_fixture("lc_signals.json")["signals"]

        with patch.object(svc, "rebuild_from_orders", return_value={"orders_read": 0}), patch.object(
            svc, "sync_fusion_events", return_value=0
        ), patch.object(svc, "poll_and_ingest_news", return_value={}), patch.object(
            svc, "reflect", return_value={"lessons": 0}
        ), patch.object(svc, "weaviate_enabled", return_value=False), patch.dict(
            os.environ, {"HERMES_RUN_LEARNING": "0"}
        ), patch(
            "intelligence.memory.social_ingest._load_cmc_posts_combined",
            return_value=cmc_posts,
        ), patch(
            "intelligence.memory.social_ingest._load_lc_signals_combined",
            return_value=lc_signals,
        ):
            # widen lookback via config default in social_ingest from get_bot_config —
            # pass through by patching social_config
            with patch(
                "intelligence.memory.social_ingest.social_config",
                return_value={
                    "cmc": {
                        "enabled": True,
                        "include_quotes_fallback": False,
                        "min_confidence": 60,
                        "trending": False,
                        "lookback_hours": 720,
                        "max_events_per_cycle": 40,
                    },
                    "lunarcrush": {
                        "enabled": True,
                        "min_galaxy_delta": 8,
                        "sentiment_extreme_low": 35,
                        "sentiment_extreme_high": 75,
                        "min_confidence": 55,
                        "lookback_hours": 720,
                        "max_events_per_cycle": 40,
                    },
                    "join_window_hours": 48,
                    "reflect_social": True,
                },
            ):
                result = svc.run_memory_cycle(store)
        self.assertIn("social", result)
        self.assertGreaterEqual(result["social"].get("cmc_events", 0), 1)


class TestRiskSocialAudit(unittest.TestCase):
    def test_dynamic_size_includes_coin_social_fail_open(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["memory"] = {"enabled": True}
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder("BUY", "SOCIAL/USDT", 1.0, 0, usdt_amount=100)
        prof = CoinProfile(
            symbol="SOCIAL/USDT",
            size_bias=0.8,
            rationale="mem",
            features={"social_summary": "cmc:BUY@78 lc:g72/s78"},
        )
        with patch(
            "intelligence.memory.cache.get_size_bias", return_value=0.8
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=prof
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"active": False, "apply_size_mult": False},
        ):
            _, factors = risk._dynamic_size(
                base_usdt=100.0,
                order=order,
                timeframe="4h",
                source="auto",
                trust_score=90.0,
                confidence=80.0,
                indicators={"atr_pct": 2.0},
            )
        self.assertIn("coin_social", factors)
        self.assertIn("cmc:BUY", factors["coin_social"])


if __name__ == "__main__":
    unittest.main()
