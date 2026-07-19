#!/usr/bin/env python3
"""P1: Fixed-query retrieval quality for Trading Memory / RAG.

Measures hit-rate of keyword/type expectations against:
  - RagRetriever (Mongo ± Weaviate RAG chunks)
  - similar_events (Mongo events ± Weaviate)

Offline-safe: uses store if Mongo available; else runs fixture-seeded MemoryStore
in-process and scores pure retrieval helpers.

Usage:
  python3 scripts/eval_memory_retrieval.py
  python3 scripts/eval_memory_retrieval.py --json
  python3 scripts/eval_memory_retrieval.py --live   # require Mongo/Weaviate env
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Fixed eval set: (query, optional_symbol, needles_any, notes)
EVAL_CASES: list[dict[str, Any]] = [
    {
        "id": "gross_loss",
        "query": "sensor entry gross loss soft_block rebuy cooloff",
        "symbol": None,
        "needles": ["loss", "soft_block", "sensor", "gross"],
        "kind": "rag_or_event",
    },
    {
        "id": "dca_lesson",
        "query": "DCA decision harvest skip cash_mode size_mult",
        "symbol": None,
        "needles": ["dca", "harvest", "size_mult", "cash"],
        "kind": "rag_or_event",
    },
    {
        "id": "macro_fed",
        "query": "FOMC Fed rate decision macro calendar pre window",
        "symbol": "BTC/USDT",
        "needles": ["fomc", "fed", "macro", "calendar"],
        "kind": "event",
    },
    {
        "id": "session_asia",
        "query": "Asia session open low liquidity fakeout",
        "symbol": None,
        "needles": ["asia", "session", "fakeout", "london", "ny"],
        "kind": "event",
    },
    {
        "id": "unlock",
        "query": "token unlock vesting sell pressure",
        "symbol": None,
        "needles": ["unlock", "vesting", "token"],
        "kind": "rag_or_event",
    },
    {
        "id": "social_lc",
        "query": "lunarcrush social spike sentiment extreme",
        "symbol": None,
        "needles": ["lc_", "social", "lunar", "sentiment", "spike"],
        "kind": "event",
    },
]


@dataclass
class CaseResult:
    id: str
    hit: bool
    sources: list[str]
    matched_needles: list[str]
    hit_count: int
    sample: str


def _blob_from_rag_hits(hits: list[Any]) -> str:
    parts = []
    for h in hits or []:
        parts.append(str(getattr(h, "text", "") or ""))
        md = getattr(h, "metadata", None) or {}
        if isinstance(md, dict):
            parts.append(json.dumps(md, default=str))
    return " ".join(parts).lower()


def _blob_from_events(events: list[Any]) -> str:
    parts = []
    for e in events or []:
        if hasattr(e, "description"):
            parts.append(str(e.description or ""))
            parts.append(str(e.event_type or ""))
            parts.append(str(e.source or ""))
        elif isinstance(e, dict):
            parts.append(json.dumps(e, default=str))
    return " ".join(parts).lower()


def _needles_hit(blob: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n.lower() in blob]


class _FixtureStore:
    """Minimal store for offline eval — no Mongo."""

    def __init__(self):
        self._events: dict[str, Any] = {}

    def upsert_event(self, event) -> bool:
        self._events[event.event_id] = event
        return True

    def get_event(self, event_id: str):
        return self._events.get(event_id)

    def list_events(
        self,
        symbol: str | None = None,
        event_type: str | None = None,
        since_iso: str | None = None,
        limit: int = 80,
        **_: Any,
    ) -> list:
        out = []
        for e in self._events.values():
            if event_type and e.event_type != event_type:
                continue
            if symbol and symbol not in (e.symbols or []):
                # allow global BTC-tagged
                if not any(symbol.split("/")[0] in s for s in (e.symbols or [])):
                    continue
            if since_iso and (e.timestamp or "") < since_iso:
                continue
            out.append(e)
        return out[: int(limit)]


def _seed_fixture_store():
    """In-memory store for offline eval (no Mongo)."""
    from intelligence.memory.models import MarketEvent, utc_now_iso

    store = _FixtureStore()
    fixtures = [
        MarketEvent(
            event_id="eval_macro_fomc",
            timestamp=utc_now_iso(),
            event_type="macro_scheduled",
            symbols=["BTC/USDT"],
            impact_score=0.5,
            description="FOMC scheduled high impact Fed rate decision",
            source="macro",
        ),
        MarketEvent(
            event_id="eval_session_asia",
            timestamp=utc_now_iso(),
            event_type="session_open",
            symbols=["BTC/USDT"],
            impact_score=0.0,
            description="session_open asia fakeout risk window",
            source="session",
        ),
        MarketEvent(
            event_id="eval_unlock",
            timestamp=utc_now_iso(),
            event_type="token_unlock",
            symbols=["ARB/USDT"],
            impact_score=0.6,
            description="Major token unlock vesting cliff sell pressure",
            source="coin_facts",
        ),
        MarketEvent(
            event_id="eval_lc",
            timestamp=utc_now_iso(),
            event_type="lc_social_spike",
            symbols=["SOL/USDT"],
            impact_score=0.3,
            description="LunarCrush social spike extreme sentiment",
            source="lunarcrush",
        ),
        MarketEvent(
            event_id="eval_dca",
            timestamp=utc_now_iso(),
            event_type="dca_decision",
            symbols=["ETH/USDT"],
            impact_score=-0.1,
            description="DCA decision ETH/USDT: action=skip cash_mode=HARVEST size_mult=0.4",
            source="dca_policy",
        ),
        MarketEvent(
            event_id="eval_loss",
            timestamp=utc_now_iso(),
            event_type="trade_outcome",
            symbols=["BDX/USDT"],
            impact_score=-0.8,
            description="sensor entry gross loss soft_block avoid rebuy",
            source="rebuild",
        ),
    ]
    for ev in fixtures:
        store.upsert_event(ev)
    return store


def run_eval(*, live: bool = False) -> dict[str, Any]:
    results: list[CaseResult] = []
    store = None
    rag_ok = False

    if live:
        try:
            from hermes.memory.rag_retriever import RagRetriever
            from intelligence.memory.rag_config import rag_enabled

            rag_ok = bool(rag_enabled())
            retriever = RagRetriever() if rag_ok else None
        except Exception:
            retriever = None
            rag_ok = False
        try:
            from intelligence.memory.store import MemoryStore

            store = MemoryStore()
        except Exception:
            store = None
    else:
        retriever = None
        store = _seed_fixture_store()
        # Also seed RAG in-memory if available
        try:
            from hermes.memory.rag_retriever import RagRetriever

            retriever = RagRetriever.in_memory(
                config={"memory": {"rag": {"enabled": True, "embedding_backend": "hash"}}}
            )
            for text, meta in (
                ("sensor entry gross loss soft_block rebuy cooloff BDX", {"type": "trade", "symbol": "BDX/USDT"}),
                ("DCA decision harvest skip cash_mode size_mult policy", {"type": "dca", "symbol": "ETH/USDT"}),
                ("token unlock vesting sell pressure ARB", {"type": "fact", "symbol": "ARB/USDT"}),
            ):
                retriever.add_to_memory(text, meta)
            rag_ok = True
        except Exception:
            rag_ok = False

    for case in EVAL_CASES:
        sources: list[str] = []
        blob_parts: list[str] = []
        hit_count = 0
        sample = ""

        if rag_ok and retriever is not None and case["kind"] in ("rag_or_event", "rag"):
            try:
                filters = {"symbol": case["symbol"]} if case.get("symbol") else None
                hits = retriever.retrieve(case["query"], top_k=8, filters=filters)
                if not hits and filters:
                    hits = retriever.retrieve(case["query"], top_k=8, filters=None)
                hit_count += len(hits or [])
                b = _blob_from_rag_hits(hits)
                blob_parts.append(b)
                if hits:
                    sources.append("rag")
                    sample = (hits[0].text or "")[:160]
            except Exception as e:
                sources.append(f"rag_err:{e}")

        if store is not None and case["kind"] in ("rag_or_event", "event"):
            try:
                from intelligence.memory.retriever import similar_events

                events = similar_events(
                    case["query"],
                    symbol=case.get("symbol"),
                    k=8,
                    store=store,
                )
                hit_count += len(events or [])
                b = _blob_from_events(events)
                blob_parts.append(b)
                if events:
                    sources.append("events")
                    if not sample and events:
                        sample = str(getattr(events[0], "description", "") or "")[:160]
            except Exception as e:
                sources.append(f"events_err:{e}")

        blob = " ".join(blob_parts)
        matched = _needles_hit(blob, list(case["needles"]))
        hit = len(matched) >= 1
        results.append(
            CaseResult(
                id=case["id"],
                hit=hit,
                sources=sources,
                matched_needles=matched,
                hit_count=hit_count,
                sample=sample,
            )
        )

    n = len(results) or 1
    hit_rate = sum(1 for r in results if r.hit) / n
    return {
        "mode": "live" if live else "fixture",
        "rag_ok": rag_ok,
        "store_ok": store is not None,
        "hit_rate": round(hit_rate, 4),
        "hits": sum(1 for r in results if r.hit),
        "total": len(results),
        "cases": [asdict(r) for r in results],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="Use live Mongo/Weaviate if configured")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-rate", type=float, default=0.5, help="Exit 1 if hit_rate below this")
    args = ap.parse_args()
    report = run_eval(live=args.live)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"mode={report['mode']} hit_rate={report['hit_rate']:.0%} "
              f"({report['hits']}/{report['total']}) rag_ok={report['rag_ok']} store_ok={report['store_ok']}")
        for c in report["cases"]:
            mark = "HIT " if c["hit"] else "MISS"
            print(f"  {mark} {c['id']:14} needles={c['matched_needles']} src={c['sources']} n={c['hit_count']}")
            if c["sample"]:
                print(f"         sample={c['sample'][:100]!r}")
    ok = report["hit_rate"] + 1e-9 >= float(args.min_rate)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
