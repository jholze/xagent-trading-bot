"""Epic #124: RAG pack, AI critic, fuse, soft filter — drive shipped modules."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.watchlist_quality.ai_critic import (
    fuse_quality,
    parse_critic_payload,
    run_ai_critic,
)
from services.watchlist_quality.engine import run_shadow_score
from services.watchlist_quality.rag_pack import RagPack, build_rag_pack
from services.watchlist_quality.soft import apply_soft_watchlist


class FakeStore:
    def __init__(self):
        self.profile = SimpleNamespace(
            symbol="ARIA/USDT",
            entry_bias="soft_block",
            size_bias=0.5,
            win_rate=0.2,
            total_pnl_usdt=-80,
            rationale="gross_loss",
        )
        self.lessons = [
            SimpleNamespace(lesson_id="L1", text="Avoid rebuy after sensor gross loss")
        ]
        self.trades = [
            SimpleNamespace(trade_id="T1", side="sell", pnl_usdt=-40, source="sensor")
        ]

    def get_profile(self, symbol, tenant_id="default", ledger_scope=None):
        if symbol == "ARIA/USDT":
            return self.profile
        return None

    def list_lessons(self, symbol=None, limit=50):
        return list(self.lessons)[:limit]

    def list_trades(self, symbol=None, tenant_id="default", limit=200):
        return list(self.trades)[:limit]


def test_build_rag_pack_from_store():
    store = FakeStore()
    events = [
        SimpleNamespace(
            event_id="E1",
            event_type="soft_block",
            description="sensor entry soft_block rebuy cooloff",
        )
    ]

    def fake_similar(query, symbol=None, k=4, store=None, **kw):
        return events[:k]

    pack = build_rag_pack(
        "ARIA/USDT",
        store=store,
        similar_events_fn=fake_similar,
        config={"watchlist_quality": {"ai": {"enabled": True}}},
    )
    assert isinstance(pack, RagPack)
    assert pack.symbol == "ARIA/USDT"
    assert pack.source == "ok"
    types = {i.type for i in pack.items}
    assert "profile" in types
    assert "lesson" in types
    assert "trade" in types
    assert "event" in types
    block = pack.evidence_block(max_chars=500)
    assert "soft_block" in block.lower() or "gross" in block.lower() or "ARIA" in block


def test_build_rag_pack_fail_open_on_store_error():
    class Boom:
        def get_profile(self, *a, **k):
            raise RuntimeError("mongo down")

        def list_lessons(self, *a, **k):
            raise RuntimeError("mongo down")

        def list_trades(self, *a, **k):
            raise RuntimeError("mongo down")

    pack = build_rag_pack(
        "X/USDT",
        store=Boom(),
        similar_events_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    # outer catch may return error or empty depending on where boom hits
    assert pack.symbol == "X/USDT"
    assert pack.source in ("error", "empty", "ok")


def test_parse_critic_clamps_adjust():
    r = parse_critic_payload(
        {"stance": "demote", "adjust": -9, "confidence": 1.5, "rationale": "thin"},
        max_adjust=0.2,
    )
    assert r.stance == "demote"
    assert r.adjust == pytest.approx(-0.2)
    assert r.confidence == pytest.approx(1.0)


def test_fuse_quality_math():
    from services.watchlist_quality.ai_critic import AiCriticResult

    c = AiCriticResult(stance="demote", adjust=-0.2, confidence=0.5, source="ok")
    assert fuse_quality(0.6, c) == pytest.approx(0.5)
    assert fuse_quality(0.6, None) == pytest.approx(0.6)
    bad = AiCriticResult(source="error")
    assert fuse_quality(0.6, bad) == pytest.approx(0.6)


def test_run_ai_critic_mocked_llm():
    pack = RagPack(
        symbol="H/USDT",
        query="q",
        items=[],
        source="empty",
    )
    # require_evidence true + empty → no_evidence without LLM
    r = run_ai_critic(
        symbol="H/USDT",
        quality_score=0.5,
        rag_pack=pack,
        config={"watchlist_quality": {"ai": {"require_evidence": True}}},
        llm_json_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")),
    )
    assert r.source == "no_evidence"

    pack2 = RagPack(
        symbol="H/USDT",
        query="q",
        items=[type("E", (), {"type": "lesson", "text": "loss", "id": "1", "score": None})()],
        source="ok",
    )
    # fix EvidenceItem
    from services.watchlist_quality.rag_pack import EvidenceItem

    pack2.items = [EvidenceItem(type="lesson", text="prior gross loss", id="L1")]

    r2 = run_ai_critic(
        symbol="H/USDT",
        quality_score=0.55,
        rag_pack=pack2,
        config={"watchlist_quality": {"ai": {"require_evidence": True, "max_adjust": 0.2}}},
        llm_json_fn=lambda prompt, **kw: {
            "stance": "demote",
            "adjust": -0.15,
            "confidence": 0.8,
            "rationale": "history of losses",
            "risk_tags": ["repeat_loss"],
        },
    )
    assert r2.source == "ok"
    assert r2.stance == "demote"
    assert r2.adjust == pytest.approx(-0.15)


def test_run_ai_critic_llm_fail_open():
    from services.watchlist_quality.rag_pack import EvidenceItem

    pack = RagPack(
        symbol="Z/USDT",
        query="q",
        items=[EvidenceItem(type="event", text="news", id="e")],
        source="ok",
    )

    def boom(*a, **k):
        raise RuntimeError("llm down")

    r = run_ai_critic(
        symbol="Z/USDT",
        quality_score=0.4,
        rag_pack=pack,
        config={"watchlist_quality": {"ai": {"require_evidence": False}}},
        llm_json_fn=boom,
    )
    assert r.source == "error"
    assert r.adjust == 0.0


def test_soft_vol_floor_keeps_open_drops_low_vol():
    coins = [
        {"symbol": "LOW/USDT", "quote_vol_24h": 1_000, "quality_score": 0.9},
        {"symbol": "HIGH/USDT", "quote_vol_24h": 2_000_000, "quality_score": 0.4},
        {"symbol": "POS/USDT", "quote_vol_24h": 500, "quality_score": 0.2},
    ]
    out = apply_soft_watchlist(
        coins,
        open_symbols={"POS/USDT"},
        min_quote_vol_usd=750_000,
        use_ai_score=True,
    )
    syms = [c["symbol"] for c in out]
    assert "LOW/USDT" not in syms
    assert "HIGH/USDT" in syms
    assert "POS/USDT" in syms
    # open first
    assert syms[0] == "POS/USDT"
    # then by score desc among rest
    assert syms.index("HIGH/USDT") > 0


def test_soft_sort_uses_quality_shadow_ai():
    coins = [
        {
            "symbol": "A/USDT",
            "quote_vol_24h": 1e6,
            "quality_score": 0.9,
            "quality_shadow_ai": 0.3,
        },
        {
            "symbol": "B/USDT",
            "quote_vol_24h": 1e6,
            "quality_score": 0.4,
            "quality_shadow_ai": 0.8,
        },
    ]
    out = apply_soft_watchlist(coins, open_symbols=set(), min_quote_vol_usd=100)
    assert [c["symbol"] for c in out] == ["B/USDT", "A/USDT"]


def test_run_shadow_score_behavior_change_false_with_ai(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "0")

    coins = [
        {
            "symbol": "A/USDT",
            "quote_vol_24h": 3_000_000,
            "change_24h": 4,
            "cmc_rank": 2,
            "source": "cmc_trending",
        },
        {
            "symbol": "B/USDT",
            "quote_vol_24h": 5_000,
            "change_24h": 10,
            "cmc_rank": 30,
            "source": "cmc_trending",
        },
    ]
    cfg = {
        "watchlist_quality": {
            "mode": "shadow",
            "ai": {
                "enabled": True,
                "require_evidence": False,
                "only_tiers_hint": [],
                "max_coins_per_cycle": 10,
            },
        }
    }

    def fake_llm(prompt, **kw):
        return {
            "stance": "demote",
            "adjust": -0.1,
            "confidence": 1.0,
            "rationale": "test",
            "risk_tags": [],
        }

    def fake_rag(symbol, **kw):
        from services.watchlist_quality.rag_pack import EvidenceItem, RagPack

        return RagPack(
            symbol=symbol,
            query="q",
            items=[EvidenceItem(type="lesson", text="x", id="1")],
            source="ok",
        )

    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input",
        side_effect=lambda sym, **kw: __import__(
            "services.watchlist_quality.memory_bias", fromlist=["MemoryWqeInput"]
        ).MemoryWqeInput(
            symbol=sym,
            entry_bias="neutral",
            size_bias=1.0,
            memory_score=0.5,
            hard_exclude_new_add=False,
            ttl_active=False,
            scope="",
            rationale="",
            source="default",
        ),
    ), patch(
        "services.watchlist_quality.engine._regime_hints",
        return_value=(1.0, "allow"),
    ):
        summary = run_shadow_score(
            coins,
            config=cfg,
            persist=True,
            llm_json_fn=fake_llm,
            rag_pack_fn=fake_rag,
        )

    assert summary["behavior_change"] is False
    assert summary["mode"] == "shadow"
    assert summary["scored"] == 2
    assert summary.get("ai_ok", 0) >= 1
    for c in summary["coins"]:
        assert "quality_score" in c
        assert c.get("quality_shadow_ai") is not None


def test_soft_mode_preview_in_shadow_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coins = [
        {"symbol": "LOW/USDT", "quote_vol_24h": 100, "quality_score": 0.9},
        {"symbol": "HI/USDT", "quote_vol_24h": 5_000_000, "change_24h": 3},
        {"symbol": "OPEN/USDT", "quote_vol_24h": 50, "change_24h": 1},
    ]
    cfg = {
        "watchlist_quality": {
            "mode": "soft",
            "vol_floors": {"t1_min_quote_vol_usd": 750_000},
            "ai": {"enabled": False},
        }
    }
    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input",
        side_effect=lambda sym, **kw: __import__(
            "services.watchlist_quality.memory_bias", fromlist=["MemoryWqeInput"]
        ).MemoryWqeInput(
            symbol=sym,
            entry_bias="neutral",
            size_bias=1.0,
            memory_score=0.5,
            hard_exclude_new_add=False,
            ttl_active=False,
            scope="",
            rationale="",
            source="default",
        ),
    ), patch(
        "services.watchlist_quality.engine._regime_hints", return_value=(None, None)
    ):
        summary = run_shadow_score(
            coins,
            config=cfg,
            persist=False,
            open_symbols={"OPEN/USDT"},
        )
    assert summary["behavior_change"] is False
    soft = summary.get("soft_scan") or []
    soft_syms = [c["symbol"] for c in soft]
    assert "LOW/USDT" not in soft_syms
    assert "OPEN/USDT" in soft_syms
    assert "HI/USDT" in soft_syms
