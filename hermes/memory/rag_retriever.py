"""RagRetriever — minimal RAG for Hermes / ask-bridge (no LangChain, no ledger writes).

Public API:
  add_to_memory(text, metadata) -> chunk_id
  retrieve(query, top_k=5, filters=None) -> list[RagHit]
  build_rag_prompt(current_context, user_query, template=...) -> str
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from intelligence.memory.embeddings import embed_for_rag
from intelligence.memory.rag_config import rag_config, rag_enabled
from intelligence.memory.rag_store import RagChunk, RagStore, chunk_id_for, rank_chunks
from logger import log

# Hard safety: never import order/position writers in this module.


@dataclass
class RagHit:
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "score": round(float(self.score), 4),
            "metadata": dict(self.metadata or {}),
            "chunk_id": self.chunk_id,
        }


def merge_hits(
    *groups: list[RagHit],
    top_k: int = 5,
    prefer_symbol: str | None = None,
) -> list[RagHit]:
    """Dedupe by chunk_id/text; prefer prefer_symbol, then higher score."""
    by_key: dict[str, RagHit] = {}
    for group in groups:
        for h in group or []:
            key = (h.chunk_id or "").strip() or (h.text or "")[:200]
            if not key:
                continue
            prev = by_key.get(key)
            if prev is None or float(h.score) > float(prev.score):
                by_key[key] = h

    def _sort_key(h: RagHit) -> tuple:
        meta = h.metadata or {}
        sym_match = 0 if (prefer_symbol and meta.get("symbol") == prefer_symbol) else 1
        return (sym_match, -float(h.score or 0))

    ordered = sorted(by_key.values(), key=_sort_key)
    return ordered[: max(1, int(top_k))]


class RagRetriever:
    """Fail-open retrieval + prompt assembly.

    Embeddings for persisted chunks: always 384-d via embed_for_rag (unless embed_fn set).
    Weaviate client is reused per retriever instance (ready/schema once).
    """

    def __init__(
        self,
        store: RagStore | None = None,
        *,
        config: dict | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
        weaviate_index=None,
    ):
        self._store = store if store is not None else RagStore()
        self._config_raw = config
        # Default RAG contract: 384-d (Weaviate MemoryRagChunk + Mongo)
        self._embed: Callable[[str], list[float]] = embed_fn or embed_for_rag
        self._wv = weaviate_index  # optional inject for tests
        self._wv_ready: bool | None = None  # None=unchecked, True=down, True=ok

    @classmethod
    def in_memory(cls, config: dict | None = None) -> "RagRetriever":
        return cls(store=RagStore.in_memory(), config=config)

    def prepare_weaviate(self) -> bool:
        """One-shot ready+schema for batch indexing. Returns True if usable."""
        return self._ensure_weaviate() is not None

    def _ensure_weaviate(self):
        """Lazy Weaviate handle; caches negative result for this instance."""
        cfg = rag_config(self._config_raw)
        if not cfg.get("use_weaviate_rag", True):
            self._wv_ready = False
            return None
        if self._wv_ready is False:
            return None
        if self._wv is not None and self._wv_ready is True:
            return self._wv
        try:
            from intelligence.memory.vector_weaviate import WeaviateIndex, weaviate_enabled

            if not weaviate_enabled():
                self._wv_ready = False
                return None
            idx = self._wv if self._wv is not None else WeaviateIndex()
            if not idx.ready():
                self._wv_ready = False
                return None
            idx.ensure_schema()
            self._wv = idx
            self._wv_ready = True
            return idx
        except Exception as e:
            log(f"RagRetriever weaviate unavailable: {e}", "DEBUG")
            self._wv_ready = False
            return None

    def add_to_memory(self, text: str, metadata: dict | None = None) -> str:
        """Index text; dual-write Mongo + optional Weaviate. Empty string on fail/disabled."""
        cfg = rag_config(self._config_raw)
        if not cfg.get("enabled"):
            return ""
        body = (text or "").strip()
        if not body:
            return ""
        meta = dict(metadata or {})
        cid = chunk_id_for(body, meta)
        try:
            emb = list(self._embed(body))
            chunk = RagChunk(
                chunk_id=cid,
                text=body[:8000],
                embedding=emb,
                metadata=meta,
            )
            ok = self._store.upsert_chunk(chunk)
            if cfg.get("use_weaviate_rag", True):
                self._weaviate_upsert(chunk)
            if ok:
                return cid
        except Exception as e:
            log(f"RagRetriever.add_to_memory failed: {e}", "WARNING")
        return ""

    def _weaviate_upsert(self, chunk: RagChunk) -> None:
        idx = self._ensure_weaviate()
        if idx is None:
            return
        try:
            meta = chunk.metadata or {}
            idx.upsert_rag_chunk(
                chunk.chunk_id,
                chunk.text,
                chunk_type=str(meta.get("type") or ""),
                symbol=str(meta.get("symbol") or ""),
                source=str(meta.get("source") or ""),
                ledger_scope=str(meta.get("ledger_scope") or ""),
                created_at=chunk.created_at or "",
                vector=list(chunk.embedding) if chunk.embedding else None,
            )
        except Exception as e:
            log(f"RagRetriever weaviate upsert skipped: {e}", "DEBUG")

    def _weaviate_retrieve(
        self, query: str, top_k: int, filters: dict | None
    ) -> list[RagHit]:
        idx = self._ensure_weaviate()
        if idx is None:
            return []
        try:
            filt = filters or {}
            symbol = filt.get("symbol") if isinstance(filt.get("symbol"), str) else None
            chunk_type = filt.get("type") if isinstance(filt.get("type"), str) else None
            qv = self._embed(query or "")
            rows = idx.search_rag_chunks(
                query or "",
                k=top_k,
                symbol=symbol,
                chunk_type=chunk_type,
                vector=qv,
            )
            hits: list[RagHit] = []
            for row in rows:
                dist = None
                add = row.get("_additional") or {}
                if isinstance(add, dict) and add.get("distance") is not None:
                    try:
                        dist = float(add["distance"])
                    except Exception:
                        dist = None
                score = (1.0 - dist) if dist is not None else 0.0
                hits.append(
                    RagHit(
                        text=str(row.get("text") or ""),
                        score=score,
                        metadata={
                            "type": row.get("chunk_type") or "",
                            "symbol": row.get("symbol") or "",
                            "source": row.get("source") or "",
                        },
                        chunk_id=str(row.get("chunk_id") or ""),
                    )
                )
            return hits
        except Exception as e:
            log(f"RagRetriever weaviate retrieve skipped: {e}", "DEBUG")
            return []

    def _mongo_retrieve(
        self, query: str, top_k: int, filters: dict | None
    ) -> list[RagHit]:
        cfg = rag_config(self._config_raw)
        limit = int(cfg.get("mongo_scan_limit") or 500)
        filt = filters or {}
        symbol = filt.get("symbol") if isinstance(filt.get("symbol"), str) else None
        chunk_type = filt.get("type") if isinstance(filt.get("type"), str) else None
        chunks = self._store.list_chunks(
            limit=limit, symbol=symbol, chunk_type=chunk_type
        )
        ranked = rank_chunks(
            query or "",
            chunks,
            top_k=top_k,
            filters=filters,
            embed_fn=self._embed,
        )
        return [
            RagHit(
                text=ch.text,
                score=float(score),
                metadata=dict(ch.metadata or {}),
                chunk_id=ch.chunk_id,
            )
            for score, ch in ranked
        ]

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[RagHit]:
        """Top-k hits: merge Weaviate + Mongo (dedupe), symbol preference via filters."""
        cfg = rag_config(self._config_raw)
        if not cfg.get("enabled"):
            return []
        try:
            k = int(top_k or cfg.get("top_k") or 5)
            prefer = None
            if filters and isinstance(filters.get("symbol"), str):
                prefer = filters["symbol"]
            groups: list[list[RagHit]] = []
            if cfg.get("use_weaviate_rag", True):
                wv = self._weaviate_retrieve(query, k, filters)
                if wv:
                    groups.append(wv)
            mongo = self._mongo_retrieve(query, k, filters)
            if mongo:
                groups.append(mongo)
            if not groups:
                return []
            return merge_hits(*groups, top_k=k, prefer_symbol=prefer)
        except Exception as e:
            log(f"RagRetriever.retrieve failed: {e}", "WARNING")
            return []

    def build_rag_prompt(
        self,
        current_context: dict,
        user_query: str,
        *,
        template: str = "default",
        hits: list[RagHit] | None = None,
        top_k: int | None = None,
        filters: dict | None = None,
    ) -> str:
        """Build enriched prompt; retrieves if hits not provided."""
        cfg = rag_config(self._config_raw)
        max_chars = int(cfg.get("max_prompt_chars") or 6000)
        ctx = current_context if isinstance(current_context, dict) else {}
        q = (user_query or "").strip() or "Summarize relevant trading memory."

        if hits is None:
            flt = dict(filters or {})
            symbol = ctx.get("symbol") or flt.get("symbol")
            if symbol and "symbol" not in flt:
                flt["symbol"] = symbol
            hits = self.retrieve(
                q, top_k=top_k or cfg.get("top_k") or 5, filters=flt or None
            )
            # If symbol-scoped empty, one unfiltered fill
            if not hits and symbol:
                hits = self.retrieve(q, top_k=top_k or cfg.get("top_k") or 5, filters=None)

        retrieved_block = _format_hits(hits)
        ctx_block = _format_context(ctx)

        if template == "propose_experiment_rag":
            prompt = (
                "You are Hermes, a self-improving crypto trading agent.\n\n"
                f"CURRENT_CONTEXT:\n{ctx_block}\n\n"
                f"RETRIEVED_MEMORY:\n{retrieved_block}\n\n"
                f"TASK: {q}\n\n"
                "Propose the next single-variable experiment. Return ONLY valid JSON:\n"
                '{"variable":"...","old_value":number,"new_value":number,"hypothesis":"..."}\n'
            )
        elif template == "dca_advice_rag":
            prompt = (
                "Du bist der Trading-Assistent. Antworte kurz auf Deutsch (max 10 Sätze).\n"
                "Du gibst nur Beratung — keine Order-Ausführung.\n\n"
                f"FRAGE: {q}\n\n"
                f"AKTUELLER KONTEXT:\n{ctx_block}\n\n"
                f"RETRIEVED_MEMORY (Trades/Lessons):\n{retrieved_block}\n\n"
                "Beantworte: letzte relevante Trades, ob Nachkauf (DCA) sinnvoll wirkt, "
                "und grobe Größenordnung — mit Begründung. Wenn unklar: sage es ehrlich.\n"
            )
        else:
            prompt = (
                "You are a trading memory assistant. Answer using context + retrieved memory only.\n"
                "Do not place orders. If evidence is weak, say so.\n\n"
                f"USER_QUERY: {q}\n\n"
                f"CURRENT_CONTEXT:\n{ctx_block}\n\n"
                f"RETRIEVED_MEMORY:\n{retrieved_block}\n"
            )

        if len(prompt) > max_chars:
            prompt = prompt[: max_chars - 20] + "\n…[truncated]"
        return prompt


def _format_hits(hits: list[RagHit] | None) -> str:
    if not hits:
        return "(no retrieved memory hits)"
    lines = []
    for i, h in enumerate(hits, 1):
        meta = h.metadata or {}
        tag = meta.get("type") or meta.get("source") or "chunk"
        sym = meta.get("symbol") or ""
        head = f"[{i}] ({tag}"
        if sym:
            head += f" {sym}"
        head += f" score={h.score:.3f})"
        lines.append(f"{head}\n{(h.text or '')[:500]}")
    return "\n\n".join(lines)


def _format_context(ctx: dict) -> str:
    if not ctx:
        return "{}"
    try:
        import json

        return json.dumps(ctx, ensure_ascii=False, indent=2, default=str)[:2500]
    except Exception:
        return str(ctx)[:2500]


def get_default_retriever(config: dict | None = None) -> RagRetriever:
    """Factory for process-wide use; fail-open if RAG disabled still returns object."""
    return RagRetriever(config=config)
