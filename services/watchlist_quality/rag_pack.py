"""AI1: RAG evidence pack for WQE AI critic (fail-open).

Assembles lessons / trades / events for a symbol into a compact evidence list
suitable for LLM context. Inject ``store`` / ``similar_events_fn`` for tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable


DEFAULT_QUERY_TMPL = (
    "{symbol} watchlist quality soft_block gross loss venue thin "
    "sensor entry rebuy cooloff unlock narrative"
)


@dataclass
class EvidenceItem:
    type: str  # lesson | trade | event | profile
    text: str
    id: str = ""
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RagPack:
    symbol: str
    query: str
    items: list[EvidenceItem] = field(default_factory=list)
    truncated: bool = False
    source: str = "ok"  # ok | empty | error | disabled
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "query": self.query,
            "items": [i.to_dict() for i in self.items],
            "truncated": self.truncated,
            "source": self.source,
            "error": self.error,
            "n": len(self.items),
        }

    def evidence_block(self, *, max_chars: int = 1500) -> str:
        """Flat text block for LLM prompts (≤ max_chars)."""
        lines: list[str] = []
        for it in self.items:
            line = f"[{it.type}] {it.text}".strip()
            if it.id:
                line = f"{line} (id={it.id})"
            lines.append(line)
        blob = "\n".join(lines)
        if len(blob) <= max_chars:
            return blob
        return blob[: max_chars - 20].rstrip() + "\n…[truncated]"


def _clip(s: str, n: int = 280) -> str:
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def build_rag_query(symbol: str, *, template: str | None = None) -> str:
    tmpl = template or DEFAULT_QUERY_TMPL
    return tmpl.format(symbol=(symbol or "").strip())


def build_rag_pack(
    symbol: str,
    *,
    config: dict | None = None,
    store: Any | None = None,
    similar_events_fn: Callable[..., list] | None = None,
    max_lessons: int = 3,
    max_trades: int = 3,
    max_events: int = 4,
    max_chars: int = 1500,
    tenant_id: str = "default",
) -> RagPack:
    """Build evidence pack for ``symbol``. Fail-open → empty pack with source=error|empty."""
    sym = (symbol or "").strip()
    query = build_rag_query(sym)
    if not sym:
        return RagPack(symbol="", query=query, source="empty", error="empty_symbol")

    # kill-switch
    try:
        from services.watchlist_quality.config import ai_config

        ai = ai_config(config)
        if ai.get("enabled") is False or ai.get("rag_enabled") is False:
            return RagPack(symbol=sym, query=query, source="disabled")
    except Exception:
        pass

    items: list[EvidenceItem] = []
    try:
        if store is None:
            from intelligence.memory.store import MemoryStore

            store = MemoryStore()

        # Profile snapshot
        try:
            prof = store.get_profile(sym, tenant_id=tenant_id)
            if prof:
                items.append(
                    EvidenceItem(
                        type="profile",
                        id=f"profile:{sym}",
                        text=_clip(
                            f"{prof.symbol} entry={prof.entry_bias} size={prof.size_bias} "
                            f"win={getattr(prof, 'win_rate', 0):.0%} "
                            f"pnl={getattr(prof, 'total_pnl_usdt', 0)} "
                            f"{getattr(prof, 'rationale', '')}"
                        ),
                    )
                )
        except Exception:
            pass

        # Lessons
        try:
            for les in (store.list_lessons(symbol=sym, limit=max_lessons) or [])[:max_lessons]:
                lid = getattr(les, "lesson_id", None) or getattr(les, "id", "") or ""
                body = getattr(les, "text", None) or getattr(les, "summary", None) or str(les)
                items.append(
                    EvidenceItem(type="lesson", id=str(lid), text=_clip(str(body)))
                )
        except Exception:
            pass

        # Trades
        try:
            for tr in (store.list_trades(symbol=sym, tenant_id=tenant_id, limit=max_trades) or [])[
                :max_trades
            ]:
                tid = getattr(tr, "trade_id", None) or getattr(tr, "id", "") or ""
                pnl = getattr(tr, "pnl_usdt", None)
                side = getattr(tr, "side", "") or ""
                src = getattr(tr, "source", "") or ""
                items.append(
                    EvidenceItem(
                        type="trade",
                        id=str(tid),
                        text=_clip(f"{sym} {side} pnl={pnl} source={src}"),
                    )
                )
        except Exception:
            pass

        # Events via similar_events (injectable)
        try:
            if similar_events_fn is None:
                from intelligence.memory.retriever import similar_events

                similar_events_fn = similar_events
            events = similar_events_fn(query, symbol=sym, k=max_events, store=store) or []
            for ev in events[:max_events]:
                eid = getattr(ev, "event_id", "") or ""
                et = getattr(ev, "event_type", "") or "event"
                desc = getattr(ev, "description", "") or ""
                items.append(
                    EvidenceItem(
                        type="event",
                        id=str(eid),
                        text=_clip(f"{et}: {desc}"),
                    )
                )
        except Exception:
            pass

        # Truncate by char budget
        pack = RagPack(symbol=sym, query=query, items=items, source="ok" if items else "empty")
        blob = pack.evidence_block(max_chars=max_chars)
        if len("\n".join(f"[{i.type}] {i.text}" for i in items)) > max_chars:
            # drop from end until under budget
            kept: list[EvidenceItem] = []
            size = 0
            for it in items:
                add = len(it.text) + 12
                if size + add > max_chars and kept:
                    pack.truncated = True
                    break
                kept.append(it)
                size += add
            pack.items = kept
        _ = blob  # ensure method works
        if not pack.items:
            pack.source = "empty"
        return pack
    except Exception as e:
        return RagPack(
            symbol=sym,
            query=query,
            source="error",
            error=f"{type(e).__name__}:{e}",
        )
