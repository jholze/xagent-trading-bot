"""Trading Memory layer tests (Epic #30) — no live ledger writes."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.memory.cache import get_size_bias, invalidate_cache
from intelligence.memory.embeddings import cosine, embed_text
from intelligence.memory.event_ingest import (
    impact_from_text,
    ingest_news_item,
    ingest_regime_event,
    ingest_webhook_signal,
    ingest_x_post,
)
from intelligence.memory.models import CoinProfile, MarketEvent, TradeMemory
from intelligence.memory.rebuild import compute_profile_from_trades, orders_to_trade_memories
from intelligence.memory.reflector import reflect
from intelligence.memory.retriever import compact_context, similar_coin_situations, similar_events
from intelligence.memory.store import InMemoryMemoryStore, resolve_memory_scope
from intelligence.memory.vector_weaviate import WeaviateIndex, _uuid_from_str
from webhooks.schemas import ExternalSignal


class TestMemoryModelsStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()
        invalidate_cache()

    def test_profile_roundtrip(self):
        scope = resolve_memory_scope()
        p = CoinProfile(
            symbol="SOL/USDT",
            size_bias=0.7,
            rationale="test",
            sells_30d=5,
            ledger_scope=scope,
        )
        self.assertTrue(self.store.upsert_profile(p))
        got = self.store.get_profile("SOL/USDT")
        self.assertIsNotNone(got)
        self.assertEqual(got.symbol, "SOL/USDT")
        self.assertAlmostEqual(got.size_bias, 0.7)
        self.assertEqual(got.ledger_scope, scope)

    def test_event_dedupe_id(self):
        e = MarketEvent(
            event_id="news:abc",
            timestamp="2026-07-17T12:00:00Z",
            event_type="news",
            description="BTC hack rumor",
            impact_score=-0.5,
            symbols=["BTC/USDT"],
        )
        self.store.upsert_event(e)
        self.assertEqual(self.store.get_event("news:abc").event_type, "news")

    def test_forbidden_collections_enforced(self):
        from intelligence.memory.store import MemoryStore

        with self.assertRaises(RuntimeError):
            MemoryStore()._col("orders")


class TestRebuild(unittest.TestCase):
    def test_orders_to_trades_and_profile(self):
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        orders = [
            {
                "id": "1",
                "status": "filled",
                "side": "buy",
                "symbol": "SOL/USDT",
                "source": "dca",
                "tenant_id": "default",
                "request": {"price": 100, "usdt": 150},
                "timestamps": {"filled": ts},
            },
            {
                "id": "2",
                "status": "filled",
                "side": "sell",
                "symbol": "SOL/USDT",
                "source": "auto",
                "tenant_id": "default",
                "pnl": -12.0,
                "request": {"price": 90, "usdt": 140},
                "timestamps": {"filled": ts},
            },
            {
                "id": "3",
                "status": "filled",
                "side": "sell",
                "symbol": "SOL/USDT",
                "pnl": -8.0,
                "request": {"usdt": 100},
                "timestamps": {"filled": ts},
            },
            {
                "id": "4",
                "status": "filled",
                "side": "sell",
                "symbol": "SOL/USDT",
                "pnl": -5.0,
                "request": {"usdt": 100},
                "timestamps": {"filled": ts},
            },
        ]
        trades = orders_to_trade_memories(orders, ledger_scope="live", tenant_id="default")
        self.assertGreaterEqual(len(trades), 4)
        sells = [t for t in trades if t.direction == "sell"]
        self.assertEqual(len(sells), 3)
        prof = compute_profile_from_trades(
            "SOL/USDT", trades, ledger_scope="live", tenant_id="default", min_samples=3
        )
        self.assertLess(prof.size_bias, 1.0)
        self.assertEqual(prof.entry_bias, "soft_block")
        self.assertIn("weak", prof.rationale.lower())


class TestEventIngest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()

    def test_impact_keywords(self):
        self.assertLess(impact_from_text("major exchange hack reported"), 0)
        self.assertGreater(impact_from_text("ETF approval breakthrough"), 0)

    def test_ingest_news_and_regime(self):
        ev = ingest_news_item(
            title="SEC charges crypto exchange",
            url="https://example.com/a",
            source="test",
            store=self.store,
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "news")
        self.assertLess(ev.impact_score, 0)
        ev2 = ingest_news_item(
            title="SEC charges crypto exchange",
            url="https://example.com/a",
            source="test",
            store=self.store,
        )
        self.assertEqual(ev.event_id, ev2.event_id)
        r = ingest_regime_event(
            source="oracle", regime="RISK_OFF", size_mult=0.35, store=self.store
        )
        self.assertEqual(r.event_type, "regime_change")

    def test_webhook_news_alert_creates_event(self):
        sig = ExternalSignal(
            source="cmc",
            symbol="ETH/USDT",
            event_type="news_alert",
            strength=0.8,
            raw={
                "title": "Ethereum upgrade mainnet breakthrough",
                "url": "https://example.com/eth-upgrade",
                "body": "successful upgrade",
            },
        )
        ev = ingest_webhook_signal(sig, store=self.store)
        self.assertIsNotNone(ev)
        self.assertIn(ev.event_type, ("news", "news_alert", "webhook_news_alert"))
        self.assertTrue(self.store.get_event(ev.event_id))
        self.assertIn("ETH", " ".join(ev.symbols))

    def test_process_signal_webhook_news_alert_to_memory(self):
        """Production path: process_signal_webhook → ingest_webhook_signal → MarketEvent."""
        from services.signal_webhook_service import process_signal_webhook

        store = InMemoryMemoryStore()
        cfg = {
            "architecture": {
                "signal_webhook_enabled": True,
                "signal_webhook_rate_limit_per_min": 100,
            },
            "entry_sensor_15m": {"enabled": False},
            "memory": {"enabled": True},
        }

        def _ingest(sig):
            return ingest_webhook_signal(sig, store=store)

        with patch("webhooks.store.publish_redis", return_value=False), patch(
            "intelligence.memory.store.memory_enabled", return_value=True
        ), patch(
            "intelligence.memory.event_ingest.ingest_webhook_signal",
            side_effect=_ingest,
        ):
            result = process_signal_webhook(
                {
                    "symbol": "BTC/USDT",
                    "event_type": "news_alert",
                    "strength": 0.9,
                    "title": "SEC charges exchange for fraud",
                    "url": "https://example.com/sec-charges-prod",
                    "body": "fraud investigation",
                },
                source="generic",
                config_raw=cfg,
            )
        self.assertTrue(result.ok)
        events = store.list_events(event_type="news") + store.list_events(
            event_type="webhook_news_alert"
        )
        # event_type may be "news" after map
        all_ev = store.list_events(limit=20)
        self.assertTrue(all_ev, "webhook should create at least one MarketEvent")
        self.assertTrue(
            any("SEC" in (e.description or "") or "fraud" in (e.description or "").lower() for e in all_ev)
        )

    def test_x_bridge_feature_flagged_off(self):
        ev = ingest_x_post(
            text="BTC looking strong after ETF flows",
            author="trader",
            enabled=False,
            store=self.store,
        )
        self.assertIsNone(ev)

    def test_x_bridge_feature_flagged_on(self):
        ev = ingest_x_post(
            text="SOL hack exploit rumor circulating",
            author="newsbot",
            url="https://x.com/1",
            enabled=True,
            store=self.store,
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "social_headline")
        self.assertLess(ev.impact_score, 0)


class TestReflectRetrieve(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()
        self.scope = resolve_memory_scope()
        ts = "2026-07-10T12:00:00Z"
        for i, pnl in enumerate([-10, -8, -12, 2]):
            self.store.upsert_trade(
                TradeMemory(
                    trade_id=f"t{i}",
                    symbol="PEPE/USDT",
                    entry_time=ts,
                    direction="sell",
                    pnl_usdt=pnl,
                    outcome="loss" if pnl < 0 else "win",
                    tenant_id="default",
                    ledger_scope=self.scope,
                )
            )
        self.store.upsert_profile(
            CoinProfile(
                symbol="PEPE/USDT",
                size_bias=1.0,
                rationale="init",
                ledger_scope=self.scope,
            )
        )

    def test_reflect_creates_lesson(self):
        out = reflect(self.store, min_samples=3)
        self.assertGreaterEqual(out["lessons"], 1)
        les = self.store.list_lessons(symbol="PEPE/USDT")
        self.assertTrue(les)
        prof = self.store.get_profile("PEPE/USDT", ledger_scope=self.scope)
        self.assertIsNotNone(prof)
        self.assertLessEqual(prof.size_bias, 0.7)
        self.assertGreaterEqual(out["profile_updates"], 1)

    def test_reflect_updates_demo_scope_profiles(self):
        """Skeptic repro: rebuild stamps demo profiles; reflect must not default to live-only."""
        store = InMemoryMemoryStore()
        ts = "2026-07-10T12:00:00Z"
        for i, pnl in enumerate([-10.0, -8.0, -12.0]):
            store.upsert_trade(
                TradeMemory(
                    trade_id=f"demo_t{i}",
                    symbol="WEAK/USDT",
                    entry_time=ts,
                    direction="sell",
                    pnl_usdt=pnl,
                    outcome="loss",
                    tenant_id="default",
                    ledger_scope="demo",
                )
            )
        store.upsert_profile(
            CoinProfile(
                symbol="WEAK/USDT",
                size_bias=1.0,
                rationale="init",
                ledger_scope="demo",
                tenant_id="default",
            )
        )
        # Ensure live key is empty (the bug path looked only here)
        self.assertIsNone(store.get_profile("WEAK/USDT", ledger_scope="live"))
        with patch(
            "intelligence.memory.store.resolve_memory_scope",
            side_effect=lambda explicit=None: explicit or "demo",
        ):
            out = reflect(store, min_samples=3, ledger_scope="demo")
        self.assertGreaterEqual(out["lessons"], 1)
        self.assertGreaterEqual(
            out["profile_updates"],
            1,
            "reflect must update demo profiles (not miss them via live default)",
        )
        prof = store.get_profile("WEAK/USDT", ledger_scope="demo")
        self.assertIsNotNone(prof)
        self.assertLessEqual(prof.size_bias, 0.7)
        self.assertIn("weak", (prof.rationale or "").lower())

    def test_compact_context_uses_active_scope(self):
        store = InMemoryMemoryStore()
        store.upsert_profile(
            CoinProfile(
                symbol="CTX/USDT",
                rationale="demo memory rationale for CTX",
                ledger_scope="demo",
            )
        )
        with patch(
            "intelligence.memory.store.resolve_memory_scope",
            side_effect=lambda explicit=None: explicit or "demo",
        ):
            ctx = compact_context("CTX/USDT", store=store, ledger_scope="demo")
        self.assertIn("mem:", ctx)
        self.assertIn("demo memory", ctx)

    def test_similar_coin_situations_str_uses_scope(self):
        store = InMemoryMemoryStore()
        a = CoinProfile(
            symbol="A/USDT",
            size_bias=0.6,
            entry_bias="soft_block",
            rationale="weak history losses",
            ledger_scope="demo",
            embedding=embed_text("A soft_block weak history losses"),
        )
        b = CoinProfile(
            symbol="B/USDT",
            size_bias=0.55,
            entry_bias="soft_block",
            rationale="weak history losses churn",
            ledger_scope="demo",
            embedding=embed_text("B soft_block weak history losses churn"),
        )
        store.upsert_profile(a)
        store.upsert_profile(b)
        with patch(
            "intelligence.memory.store.resolve_memory_scope",
            side_effect=lambda explicit=None: explicit or "demo",
        ):
            sims = similar_coin_situations("A/USDT", store=store, ledger_scope="demo", k=3)
        self.assertTrue(sims)
        self.assertEqual(sims[0].symbol, "B/USDT")

    def test_similar_events_local(self):
        self.store.upsert_event(
            MarketEvent(
                event_id="e1",
                timestamp="2026-07-17T00:00:00Z",
                event_type="news",
                description="exchange hack causes crash",
                impact_score=-0.8,
                symbols=["BTC/USDT"],
                embedding=embed_text("news exchange hack causes crash"),
            )
        )
        hits = similar_events("hack exploit exchange", store=self.store, k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].event_id, "e1")

    def test_similar_coin_situations_local(self):
        scope = self.scope
        weak = CoinProfile(
            symbol="WEAK2/USDT",
            size_bias=0.6,
            entry_bias="soft_block",
            rationale="weak history losses",
            ledger_scope=scope,
            embedding=embed_text("WEAK2 soft_block weak history losses"),
        )
        peer = CoinProfile(
            symbol="PEER/USDT",
            size_bias=0.55,
            entry_bias="soft_block",
            rationale="weak history losses churn",
            ledger_scope=scope,
            embedding=embed_text("PEER soft_block weak history losses churn"),
        )
        strong = CoinProfile(
            symbol="STRONG/USDT",
            size_bias=1.1,
            entry_bias="prefer",
            rationale="strong win rate momentum",
            ledger_scope=scope,
            embedding=embed_text("STRONG prefer strong win rate momentum"),
        )
        for p in (weak, peer, strong):
            self.store.upsert_profile(p)
        sims = similar_coin_situations(weak, store=self.store, k=3, ledger_scope=scope)
        self.assertTrue(sims)
        self.assertEqual(sims[0].symbol, "PEER/USDT")

    def test_size_bias_fail_open(self):
        invalidate_cache()
        b = get_size_bias("NOEXIST/USDT")
        self.assertEqual(b, 1.0)


class TestEmbeddings(unittest.TestCase):
    def test_cosine_self(self):
        v = embed_text("bitcoin etf approval")
        self.assertAlmostEqual(cosine(v, v), 1.0, places=5)
        self.assertGreater(cosine(v, embed_text("bitcoin etf news")), 0.1)


class TestWeaviateClient(unittest.TestCase):
    def test_uuid_stable(self):
        a = _uuid_from_str("event:abc")
        b = _uuid_from_str("event:abc")
        self.assertEqual(a, b)

    def test_disabled_without_url(self):
        with patch.dict(os.environ, {"WEAVIATE_URL": ""}, clear=False):
            idx = WeaviateIndex(base_url="")
            self.assertFalse(idx.ready())
            self.assertFalse(idx.upsert_event("e1", "test"))
            self.assertEqual(idx.search_events("q"), [])

    def test_mock_insert_and_query(self):
        """BYO vector insert + graphql search path (mock HTTP)."""
        calls = []

        def fake_req(method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("/ready"):
                return {}
            if path == "/v1/schema":
                return {}
            if path == "/v1/objects" or path.startswith("/v1/objects/"):
                return {"id": "x"}
            if path == "/v1/graphql":
                return {
                    "data": {
                        "Get": {
                            "MemoryEvent": [
                                {"event_id": "news:1", "description": "hack"},
                            ]
                        }
                    }
                }
            return {}

        idx = WeaviateIndex(base_url="http://weaviate.test")
        with patch.object(idx, "_req", side_effect=fake_req):
            self.assertTrue(idx.ready())
            idx.ensure_schema()
            ok = idx.upsert_event(
                "news:1",
                "exchange hack",
                event_type="news",
                symbols=["BTC/USDT"],
                vector=embed_text("news exchange hack"),
            )
            self.assertTrue(ok)
            ids = idx.search_events("hack", symbol="BTC/USDT", k=3)
            self.assertEqual(ids, ["news:1"])
            profiles = idx.search_similar_profiles("weak soft_block", k=2)
            # empty when graphql not matching profile class in this fake
            self.assertIsInstance(profiles, list)


class TestNewsProviders(unittest.TestCase):
    def test_scrape_list_page_fixture(self):
        from intelligence.memory.news_providers import scrape_list_page

        html = """
        <html><body>
        <a href="/article/one">Major exchange hack reported today</a>
        <a href="https://ex.com/two">ETF approval breakthrough news here</a>
        <a href="/short">no</a>
        </body></html>
        """
        with patch("intelligence.memory.news_providers._http_get", return_value=html.encode()):
            items = scrape_list_page("https://news.example.com/", limit=5)
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(any("hack" in i["title"].lower() for i in items))

    def test_poll_ingest_with_fixture_rss(self):
        from intelligence.memory.news_providers import poll_and_ingest_news

        rss = b"""<?xml version="1.0"?>
        <rss><channel>
          <item><title>BTC ETF approval breakthrough</title>
          <link>https://example.com/etf</link>
          <description>big news</description></item>
        </channel></rss>"""
        store = InMemoryMemoryStore()

        def fake_get(url, timeout=15.0):
            if "rss" in url or "feed" in url or "coindesk" in url:
                return rss
            raise RuntimeError("skip")

        with patch("intelligence.memory.news_providers._http_get", side_effect=fake_get), patch(
            "intelligence.memory.news_providers.fetch_coingecko_news", return_value=[]
        ), patch(
            "intelligence.memory.news_providers.fetch_free_crypto_news", return_value=[]
        ), patch(
            "intelligence.memory.news_providers.ingest_defillama_events", return_value=0
        ):
            counts = poll_and_ingest_news(
                store,
                rss_feeds=["https://example.com/rss"],
                use_coingecko=False,
                use_free_crypto_news=False,
                use_defillama=False,
                max_per_source=5,
                config={"memory": {"news": {}, "onchain": {}}},
            )
        self.assertGreaterEqual(counts["rss"], 1)
        self.assertTrue(store.list_events(event_type="news"))


class TestRiskManagerMemory(unittest.TestCase):
    """TM-7: size_bias multiply, soft_block reject, sells never blocked by memory."""

    def setUp(self):
        invalidate_cache()

    def _cfg(self, **raw_extra):
        from core.config import BotConfig
        from data_manager import get_config

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["memory"] = {"enabled": True}
        raw.update(raw_extra)
        cfg = BotConfig()
        cfg._raw = raw
        return cfg

    def test_soft_block_rejects_new_entry(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = self._cfg()
        risk = RiskManager(cfg)
        prof = CoinProfile(
            symbol="MEMBLK/USDT",
            size_bias=0.6,
            entry_bias="soft_block",
            rationale="weak history",
            ledger_scope="demo",
        )

        with patch("data_manager.resolve_ledger_scope", return_value="demo"), patch(
            "intelligence.memory.cache.get_entry_bias", return_value="soft_block"
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=prof
        ), patch(
            "risk.risk_manager.get_position", return_value={"amount": 0}
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"active": False, "block_buys": False},
        ), patch.object(risk, "_daily_trades_count", return_value=0):
            decision = risk.evaluate(
                TradeOrder("BUY", "MEMBLK/USDT", 1.0, 0, usdt_amount=50),
                "4h",
                source="auto",
            )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "coin_memory_soft_block")

    def test_sell_not_blocked_by_soft_block(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = self._cfg()
        risk = RiskManager(cfg)
        with patch("risk.risk_manager.get_position", return_value={"amount": 10, "entry_price": 1.0}), patch(
            "intelligence.memory.cache.get_entry_bias", return_value="soft_block"
        ), patch.object(risk, "_daily_sells_count", return_value=0), patch.object(
            risk, "_effective_max_daily_sells", return_value=0
        ), patch.object(
            risk, "_partial_sell_blocked", return_value=(False, "")
        ), patch.object(
            risk, "_trade_cooldown_blocked", return_value=(False, "")
        ), patch.object(
            risk,
            "_resolve_sell_order",
            side_effect=lambda o, *a, **k: o,
        ):
            decision = risk.evaluate(
                TradeOrder("SELL", "MEMBLK/USDT", 1.0, 5, signal="SELL_FULL"),
                "4h",
                source="auto",
            )
        self.assertTrue(decision.approved)
        self.assertNotEqual(getattr(decision, "code", None), "coin_memory_soft_block")

    def test_size_bias_multiplies_auto_buy(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = self._cfg()
        risk = RiskManager(cfg)
        order = TradeOrder("BUY", "BIAS/USDT", 1.0, 0, usdt_amount=100)
        common = dict(
            base_usdt=100.0,
            order=order,
            timeframe="4h",
            source="auto",
            trust_score=90.0,
            confidence=80.0,
            indicators={"atr_pct": 2.0},
        )

        with patch(
            "intelligence.memory.cache.get_size_bias", return_value=0.5
        ), patch(
            "intelligence.memory.cache.get_coin_profile",
            return_value=CoinProfile(symbol="BIAS/USDT", size_bias=0.5, rationale="cut"),
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"active": False, "apply_size_mult": False},
        ):
            sized, factors = risk._dynamic_size(**common)
        self.assertIn("coin_size_bias", factors)
        self.assertAlmostEqual(factors["coin_size_bias"], 0.5, places=2)
        with patch(
            "intelligence.memory.cache.get_size_bias", return_value=1.0
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"active": False, "apply_size_mult": False},
        ):
            sized_full, _ = risk._dynamic_size(**common)
        self.assertLess(sized, sized_full)

    def test_evaluate_sell_never_uses_soft_block_code(self):
        """Sells go through evaluate(); soft_block must never be the rejection code."""
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = self._cfg()
        risk = RiskManager(cfg)
        with patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 10, "entry_price": 1.0, "average_entry": 1.0},
        ), patch(
            "intelligence.memory.cache.get_entry_bias", return_value="soft_block"
        ), patch.object(risk, "_daily_sells_count", return_value=0), patch.object(
            risk, "_effective_max_daily_sells", return_value=0
        ), patch.object(
            risk, "_partial_sell_blocked", return_value=(False, "")
        ), patch.object(
            risk, "_trade_cooldown_blocked", return_value=(False, "")
        ), patch.object(
            risk, "_resolve_sell_order", side_effect=lambda o, *a, **k: o
        ):
            decision = risk.evaluate(
                TradeOrder("SELL", "ANY/USDT", 1.0, 3, signal="SELL_FULL"),
                "4h",
                source="auto",
            )
        self.assertTrue(decision.approved)
        self.assertNotEqual(getattr(decision, "code", None), "coin_memory_soft_block")


class TestServiceLiveEvidence(unittest.TestCase):
    def test_record_hermes_outcome_rates(self):
        from intelligence.memory import service as svc

        svc._STATE["live_evidence"] = {
            "mode": "dual",
            "promotions": 0,
            "rejections": 0,
            "live_vetoes": 0,
            "cycles": 0,
        }

        class R:
            symbol = "A/USDT"
            verdict = "promoted"
            promoted = True
            variable = "rsi"
            live_veto = False

        out = svc._record_hermes_outcome(R())
        self.assertTrue(out["promoted"])
        self.assertEqual(svc._STATE["live_evidence"]["promotions"], 1)
        self.assertAlmostEqual(svc._STATE["live_evidence"]["promotion_rate"], 1.0)

        class R2:
            symbol = "B/USDT"
            verdict = "live_veto: loss"
            promoted = False
            variable = "sl"
            live_veto = True

        svc._record_hermes_outcome(R2())
        self.assertEqual(svc._STATE["live_evidence"]["live_vetoes"], 1)
        self.assertGreater(svc._STATE["live_evidence"]["veto_rate"], 0)


if __name__ == "__main__":
    unittest.main()
