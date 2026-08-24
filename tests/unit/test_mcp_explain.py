"""MCP why-pack: orders, memory, facts, RAG — tenant-scoped, fail-open, no embeddings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.mcp_authz import Actor
from services.mcp_explain import (
    list_orders_public,
    memory_pack,
    sanitize_event,
    sanitize_order,
    sanitize_profile,
    why_pack,
)
from services.mcp_tools import tool_memory, tool_orders, tool_why

OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))
OBS = Actor("o", "observer", ("henry",), ("read",))


RAW_ORDER = {
    "id": "abc123",
    "status": "filled",
    "side": "buy",
    "symbol": "BLESS/USDT",
    "timeframe": "1h",
    "source": "gainer_relvol",
    "signal": "BUY",
    "exit_source": None,
    "exit_rationale": None,
    "tenant_id": "default",
    "error": None,
    "request": {
        "price": 0.0135,
        "amount": 185185.0,
        "usdt": 2500.0,
        "huge_blob": "x" * 5000,
        "internal": {"secret": 1},
    },
    "risk": {
        "approved": True,
        "message": "ok",
        "code": "",
        "size_multiplier": 1.0,
        "approved_usdt": 2500.0,
        "checked_at": "2026-08-23T12:00:00",
        "debug_trace": ["a"] * 80,
    },
    "execution": {"filled_price": 0.01351, "filled_amount": 185000.0, "filled_usdt": 2499.0},
    "pnl": None,
    "timestamps": {"created": "2026-08-23T12:00:00", "filled": "2026-08-23T12:00:01"},
    "embedding": [0.1] * 64,
}


def test_sanitize_order_keeps_signal_drops_blob_and_embedding():
    out = sanitize_order(RAW_ORDER)
    assert out["source"] == "gainer_relvol"
    assert out["signal"] == "BUY"
    assert out["request"]["usdt"] == 2500.0
    assert "huge_blob" not in out["request"]
    assert "internal" not in out["request"]
    assert "embedding" not in out
    assert "debug_trace" not in out["risk"]
    assert out["risk"]["approved"] is True
    assert out["execution"]["filled_price"] == 0.01351


def test_sanitize_profile_strips_embedding():
    out = sanitize_profile(
        {
            "symbol": "BLESS/USDT",
            "entry_bias": "prefer",
            "size_bias": 1.1,
            "rationale": "relvol + social",
            "features": {"rsi": 32},
            "embedding": [0.2] * 32,
            "trades_30d": 4,
        }
    )
    assert out["entry_bias"] == "prefer"
    assert out["rationale"] == "relvol + social"
    assert "embedding" not in out
    assert out["features"]["rsi"] == 32


def test_sanitize_event_strips_embedding():
    out = sanitize_event(
        {
            "event_id": "e1",
            "timestamp": "2026-08-23T10:00:00Z",
            "event_type": "catalyst",
            "description": "listing rumor",
            "source": "cmc_ai_updates",
            "impact_score": 0.6,
            "symbols": ["BLESS/USDT"],
            "embedding": [1.0] * 8,
        }
    )
    assert out["event_type"] == "catalyst"
    assert "embedding" not in out


def test_list_orders_public_filters_symbol_and_sanitizes():
    henry_other = dict(RAW_ORDER, id="z", symbol="AAA/USDT", tenant_id="henry")
    bless = dict(RAW_ORDER, id="b1", tenant_id="henry")

    def fake_list(**kw):
        assert kw["tenant_id"] == "henry"
        return [henry_other, bless]

    out = list_orders_public("henry", symbol="BLESS", list_fn=fake_list)
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"
    assert len(out["orders"]) == 1
    assert out["orders"][0]["id"] == "b1"
    assert "embedding" not in out["orders"][0]


def test_list_orders_public_fail_open():
    def boom(**_kw):
        raise RuntimeError("mongo down")

    out = list_orders_public("default", list_fn=boom)
    assert out["ok"] is True
    assert out["orders"] == []
    assert out["errors"]


@dataclass
class FakeProfile:
    symbol: str
    entry_bias: str = "neutral"
    size_bias: float = 1.0
    rationale: str = ""
    features: dict = field(default_factory=dict)
    embedding: list = field(default_factory=list)
    trades_30d: int = 0
    sells_30d: int = 0
    buys_30d: int = 0
    win_rate: float = 0.0
    total_pnl_usdt: float = 0.0
    avg_pnl_usdt: float = 0.0
    dca_count_30d: int = 0
    risk_score: float = 0.5
    as_of: str = "2026-08-23T00:00:00Z"
    version: int = 1
    ledger_scope: str = "paper"
    tenant_id: str = "default"


@dataclass
class FakeEvent:
    event_id: str
    timestamp: str
    event_type: str
    description: str = ""
    source: str = ""
    impact_score: float = 0.0
    symbols: list = field(default_factory=list)
    url: str = ""
    metadata: dict = field(default_factory=dict)
    embedding: list = field(default_factory=list)
    tenant_id: str = "default"


@dataclass
class FakeTrade:
    trade_id: str
    symbol: str
    direction: str = "buy"
    source: str = ""
    reason: str = ""
    outcome: str = "open"
    pnl_usdt: float | None = None
    entry_time: str = ""
    embedding: list = field(default_factory=list)
    tenant_id: str = "default"
    metadata: dict = field(default_factory=dict)


@dataclass
class FakeLesson:
    lesson_id: str
    text: str
    symbols: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    embedding: list = field(default_factory=list)
    tenant_id: str = "default"


class FakeStore:
    def __init__(self):
        self.profiles: dict[str, FakeProfile] = {}
        self.events: list[FakeEvent] = []
        self.trades: list[FakeTrade] = []
        self.lessons: list[FakeLesson] = []

    def get_profile(self, symbol, **_kw):
        return self.profiles.get(symbol)

    def list_profiles(self, *, tenant_id="default", limit=200):
        return [p for p in self.profiles.values() if p.tenant_id == tenant_id][:limit]

    def list_events(self, *, symbol=None, event_type=None, since_iso=None, limit=50):
        out = list(self.events)
        if symbol:
            base = str(symbol).upper().split("/")[0]
            out = [e for e in out if any(base in str(s).upper() for s in e.symbols)]
        return out[:limit]

    def list_trades(self, *, symbol=None, tenant_id="default", limit=200):
        out = [t for t in self.trades if t.tenant_id == tenant_id]
        if symbol:
            out = [t for t in out if t.symbol == symbol]
        return out[:limit]

    def list_lessons(self, *, symbol=None, limit=50):
        out = list(self.lessons)
        if symbol:
            want = str(symbol).upper()
            base = want.split("/")[0]
            out = [
                L
                for L in out
                if want in [s.upper() for s in L.symbols]
                or base in [s.upper().split("/")[0] for s in L.symbols]
                or base.lower() in [t.lower() for t in L.tags]
            ]
        return out[:limit]


def _store() -> FakeStore:
    s = FakeStore()
    s.profiles["BLESS/USDT"] = FakeProfile(
        symbol="BLESS/USDT",
        entry_bias="prefer",
        size_bias=1.15,
        rationale="gainer relvol + oversold",
        features={"rsi_1h": 28, "vol_ratio": 3.2},
        embedding=[9.9] * 16,
        tenant_id="default",
        trades_30d=3,
    )
    s.events.append(
        FakeEvent(
            event_id="ev1",
            timestamp="2026-08-23T09:00:00Z",
            event_type="volume_breakout",
            description="15m relvol spike",
            source="gainer_relvol",
            impact_score=0.7,
            symbols=["BLESS/USDT"],
            embedding=[1.0],
        )
    )
    s.trades.append(
        FakeTrade(
            trade_id="t1",
            symbol="BLESS/USDT",
            direction="buy",
            source="gainer_relvol",
            reason="BUY relvol",
            embedding=[2.0],
        )
    )
    s.lessons.append(
        FakeLesson(
            lesson_id="l1",
            text="Do not chase BLESS after +20% 15m",
            symbols=["BLESS/USDT"],
            embedding=[3.0],
        )
    )
    return s


def test_memory_pack_symbol_strips_embeddings():
    pack = memory_pack(
        "default",
        "BLESS/USDT",
        store=_store(),
        facts_fn=lambda **_k: {"hard_negative": False, "unlock": True, "summary": "unlock rumor"},
        rag_fn=lambda **_k: [{"text": "BLESS oversold dip", "score": 0.81, "metadata": {}, "chunk_id": "c1"}],
    )
    assert pack["ok"] is True
    assert pack["profile"]["entry_bias"] == "prefer"
    assert "embedding" not in pack["profile"]
    assert pack["facts"]["unlock"] is True
    assert pack["events"][0]["event_type"] == "volume_breakout"
    assert "embedding" not in pack["events"][0]
    assert pack["trades"][0]["source"] == "gainer_relvol"
    assert "embedding" not in pack["trades"][0]
    assert pack["lessons"][0]["lesson_id"] == "l1"
    assert pack["rag"][0]["chunk_id"] == "c1"


def test_memory_pack_fail_open_store():
    class Boom:
        def get_profile(self, *_a, **_k):
            raise RuntimeError("down")

        def list_events(self, **_k):
            raise RuntimeError("down")

        def list_trades(self, **_k):
            raise RuntimeError("down")

        def list_lessons(self, **_k):
            raise RuntimeError("down")

    pack = memory_pack("default", "BLESS/USDT", store=Boom(), facts_fn=lambda **_k: {})
    assert pack["ok"] is True
    assert pack["profile"] is None
    assert pack["events"] == []
    assert pack["errors"]


def test_why_pack_assembles_orders_memory_hud():
    def fake_list(**_kw):
        return [RAW_ORDER]

    pack = why_pack(
        "default",
        "BLESS/USDT",
        store=_store(),
        list_fn=fake_list,
        facts_fn=lambda **_k: {"catalyst": True, "summary": "listing"},
        rag_fn=lambda **_k: [{"text": "hit", "score": 0.5, "metadata": {}, "chunk_id": "r1"}],
        snapshot_fn=lambda **_k: {
            "ok": True,
            "tenant_id": "default",
            "lots": [
                {
                    "symbol": "BLESS/USDT",
                    "entry_source": "gainer_relvol",
                    "average_entry": 0.0135,
                    "dca_rounds": 1,
                }
            ],
            "hud": {"memory": {"stance": "OK"}},
            "badges": {"fusion": "RISK_ON"},
            "next_edge": "hold",
            "conflict": None,
        },
    )
    assert pack["ok"] is True
    assert pack["lot"]["entry_source"] == "gainer_relvol"
    assert pack["hud"]["memory"]["stance"] == "OK"
    assert pack["badges"]["fusion"] == "RISK_ON"
    assert pack["orders"][0]["signal"] == "BUY"
    assert pack["profile"]["rationale"] == "gainer relvol + oversold"
    assert pack["facts"]["catalyst"] is True
    assert pack["rag"][0]["chunk_id"] == "r1"


def test_why_pack_missing_symbol():
    pack = why_pack("default", "")
    assert pack["ok"] is False
    assert pack["error"] == "missing_symbol"


def test_tool_orders_operator_forced_off_default():
    calls = []

    def fake_list(**kw):
        calls.append(kw)
        return [dict(RAW_ORDER, tenant_id=kw["tenant_id"], symbol="AAA/USDT")]

    out = tool_orders(HENRY, tenant="default", list_fn=fake_list)
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"
    assert calls[0]["tenant_id"] == "henry"


def test_tool_orders_unauthorized_does_not_list():
    called = []
    out = tool_orders(None, tenant="henry", list_fn=lambda **k: called.append(k) or [])
    assert out["ok"] is False and out["error"] == "unauthorized"
    assert called == []


def test_tool_memory_observer_can_read():
    out = tool_memory(
        OBS,
        tenant="henry",
        symbol="BLESS/USDT",
        memory_fn=lambda **kw: {"ok": True, "tenant_id": kw["tenant_id"], "profile": {"entry_bias": "neutral"}},
    )
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"


def test_tool_why_owner_ctexp():
    out = tool_why(
        OWNER,
        tenant="ctexp",
        symbol="LAB/USDT",
        why_fn=lambda **kw: {
            "ok": True,
            "tenant_id": kw["tenant_id"],
            "symbol": kw["symbol"],
            "orders": [],
        },
    )
    assert out["ok"] is True
    assert out["tenant_id"] == "ctexp"
    assert out["symbol"] == "LAB/USDT"


def test_tool_why_observer_cannot_leave_henry():
    called = []
    out = tool_why(
        OBS,
        tenant="default",
        symbol="LAB/USDT",
        why_fn=lambda **kw: called.append(kw) or {"ok": True, "tenant_id": kw["tenant_id"]},
    )
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"
    assert called[0]["tenant_id"] == "henry"
