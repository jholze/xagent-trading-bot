"""Mongo-backed RAG chunk store (memory_rag_chunks only).

LEDGER SAFETY: refuses non-memory_* collections. In-memory backend for unit tests.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.memory.embeddings import cosine, embed_text
from logger import log

COL_RAG = "memory_rag_chunks"
_FORBIDDEN = frozenset(
    {
        "orders",
        "positions",
        "trade_history",
        "compound_orders",
        "compound_positions",
        "compound_trade_history",
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunk_id_for(text: str, metadata: dict | None = None) -> str:
    meta = metadata or {}
    key = f"{meta.get('type')}|{meta.get('source_id') or meta.get('symbol')}|{text[:120]}"
    return "rag_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


@dataclass
class RagChunk:
    chunk_id: str
    text: str
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now_iso()

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["_id"] = self.chunk_id
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> RagChunk | None:
        if not doc:
            return None
        try:
            return cls(
                chunk_id=str(doc.get("chunk_id") or doc.get("_id") or ""),
                text=str(doc.get("text") or ""),
                embedding=list(doc.get("embedding") or []),
                metadata=dict(doc.get("metadata") or {}),
                created_at=str(doc.get("created_at") or ""),
            )
        except Exception:
            return None


class InMemoryRagBackend:
    """Thread-safe dict store for tests (no Mongo)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._docs: dict[str, dict[str, Any]] = {}

    def upsert(self, doc: dict[str, Any]) -> bool:
        cid = str(doc.get("_id") or doc.get("chunk_id") or "")
        if not cid:
            return False
        with self._lock:
            self._docs[cid] = dict(doc)
        return True

    def list_docs(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._docs.values())
        return items[: max(1, int(limit))]

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()


class RagStore:
    """CRUD for RAG chunks. Fail-open on Mongo errors."""

    def __init__(self, backend: InMemoryRagBackend | None = None, db=None):
        self._memory = backend
        self._db = db

    @classmethod
    def in_memory(cls) -> "RagStore":
        return cls(backend=InMemoryRagBackend())

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from storage.mongo_client import get_database

        return get_database()

    def _col(self):
        name = COL_RAG
        if name in _FORBIDDEN or not name.startswith("memory_"):
            raise RuntimeError(f"rag store refused collection: {name}")
        return self.db[name]

    def upsert_chunk(self, chunk: RagChunk) -> bool:
        try:
            doc = chunk.to_doc()
            if self._memory is not None:
                return self._memory.upsert(doc)
            self._col().replace_one({"_id": doc["_id"]}, doc, upsert=True)
            return True
        except Exception as e:
            log(f"rag upsert_chunk failed: {e}", "WARNING")
            return False

    def list_chunks(
        self,
        *,
        limit: int = 500,
        symbol: str | None = None,
        chunk_type: str | None = None,
    ) -> list[RagChunk]:
        """List recent chunks; optional metadata filters (Mongo query / in-memory filter)."""
        try:
            if self._memory is not None:
                docs = self._memory.list_docs(limit=max(limit * 3, limit))
                if symbol or chunk_type:
                    filtered = []
                    for d in docs:
                        meta = d.get("metadata") or {}
                        if symbol and meta.get("symbol") != symbol:
                            continue
                        if chunk_type and meta.get("type") != chunk_type:
                            continue
                        filtered.append(d)
                    docs = filtered[: int(limit)]
                else:
                    docs = docs[: int(limit)]
            else:
                q: dict[str, Any] = {}
                if symbol:
                    q["metadata.symbol"] = symbol
                if chunk_type:
                    q["metadata.type"] = chunk_type
                docs = list(
                    self._col().find(q).sort("created_at", -1).limit(int(limit))
                )
            out: list[RagChunk] = []
            for d in docs:
                c = RagChunk.from_doc(d)
                if c and c.text:
                    out.append(c)
            return out
        except Exception as e:
            log(f"rag list_chunks failed: {e}", "WARNING")
            return []

    def ensure_indexes(self) -> None:
        if self._memory is not None:
            return
        try:
            col = self._col()
            col.create_index("metadata.type")
            col.create_index("metadata.symbol")
            col.create_index("metadata.ledger_scope")
            col.create_index("created_at")
        except Exception as e:
            log(f"rag ensure_indexes failed: {e}", "DEBUG")


def filter_matches(metadata: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    meta = metadata or {}
    for key, want in filters.items():
        if key.startswith("$"):
            continue
        have = meta.get(key)
        if isinstance(want, dict) and "$in" in want:
            if have not in list(want["$in"]):
                return False
        elif have != want:
            return False
    return True


def rank_chunks(
    query: str,
    chunks: list[RagChunk],
    *,
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
    embed_fn=None,
) -> list[tuple[float, RagChunk]]:
    """Pure ranking: cosine over embeddings (embed query + chunk text fallback)."""
    embed = embed_fn or embed_text
    qv = embed(query or "")
    scored: list[tuple[float, RagChunk]] = []
    for ch in chunks:
        if not filter_matches(ch.metadata, filters):
            continue
        vec = ch.embedding if ch.embedding else embed(ch.text)
        if len(vec) != len(qv):
            # dim mismatch — re-embed text with same fn
            vec = embed(ch.text)
        scored.append((cosine(qv, vec), ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[: max(1, int(top_k))]
