"""In-process cortex store: demo or loaded JSON (no ledger collections)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tools.memory_viz.demo_cortex import build_demo_cortex
from tools.memory_viz.lobes import lobe_legend
from tools.memory_viz.ranking import top_k_cosine

# Explicit denylist — never query these (defense in depth; this package never opens Mongo for ledger)
LEDGER_COLLECTIONS = frozenset(
    {
        "orders",
        "positions",
        "trade_history",
        "compound_orders",
        "compound_positions",
        "compound_trade_history",
    }
)


class CortexStore:
    """Thread-safe read-only cortex + vector matrix."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cortex: dict[str, Any] = {}
        self._vectors: list[list[float]] = []
        self._texts: list[str] = []
        self._by_id: dict[str, int] = {}

    @property
    def node_count(self) -> int:
        with self._lock:
            return len(self._cortex.get("nodes") or [])

    @property
    def is_demo(self) -> bool:
        with self._lock:
            return bool(self._cortex.get("demo"))

    def load_demo(self, *, variants_per_seed: int = 8) -> None:
        cortex, vectors = build_demo_cortex(variants_per_seed=variants_per_seed)
        self._install(cortex, vectors)

    def load_json(self, path: str | Path) -> None:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        nodes = list(data.get("nodes") or [])
        vectors = list(data.get("vectors") or [])
        texts = list(data.get("texts") or [])
        if vectors and len(vectors) != len(nodes):
            raise ValueError("vectors length must match nodes")
        if not vectors:
            from tools.memory_viz.hash_embed import resolve_embed_for_rag

            embed = resolve_embed_for_rag()
            vectors = []
            for i, n in enumerate(nodes):
                t = texts[i] if i < len(texts) else str(n.get("preview") or n.get("title") or "")
                vectors.append(list(embed(t)))
        data = dict(data)
        data["nodes"] = nodes
        data["texts"] = texts or [str(n.get("preview") or "") for n in nodes]
        data["node_count"] = len(nodes)
        data["demo"] = bool(data.get("demo", False))
        self._install(data, vectors)

    def _install(self, cortex: dict[str, Any], vectors: list[list[float]]) -> None:
        nodes = list(cortex.get("nodes") or [])
        texts = list(cortex.get("texts") or [])
        while len(texts) < len(nodes):
            texts.append(str(nodes[len(texts)].get("preview") or ""))
        by_id = {str(n.get("id")): int(n.get("i", i)) for i, n in enumerate(nodes)}
        with self._lock:
            self._cortex = cortex
            self._vectors = vectors
            self._texts = texts
            self._by_id = by_id

    def public_cortex(self) -> dict[str, Any]:
        """Cortex JSON for the browser (no full embedding matrix)."""
        with self._lock:
            nodes = []
            for n in self._cortex.get("nodes") or []:
                nodes.append(
                    {
                        "i": n.get("i"),
                        "id": n.get("id"),
                        "pos": n.get("pos"),
                        "col": n.get("col"),
                        "lobe": n.get("lobe"),
                        "symbol": n.get("symbol"),
                        "source": n.get("source"),
                        "type": n.get("type"),
                        "title": n.get("title"),
                        "preview": n.get("preview"),
                        "created_at": n.get("created_at"),
                        "nbs": n.get("nbs") or [],
                    }
                )
            return {
                "version": self._cortex.get("version", 1),
                "built_at": self._cortex.get("built_at"),
                "embedding_backend": self._cortex.get("embedding_backend"),
                "embedding_dim": self._cortex.get("embedding_dim"),
                "demo": bool(self._cortex.get("demo")),
                "node_count": len(nodes),
                "nodes": nodes,
                "lobes": lobe_legend(),
            }

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "service": "memory-cortex",
                "demo": bool(self._cortex.get("demo")),
                "node_count": len(self._cortex.get("nodes") or []),
                "embedding_backend": self._cortex.get("embedding_backend"),
                "embedding_dim": self._cortex.get("embedding_dim"),
                "built_at": self._cortex.get("built_at"),
                "ledger_blocked": sorted(LEDGER_COLLECTIONS),
            }

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self._lock:
            i = self._by_id.get(str(node_id))
            if i is None:
                return None
            nodes = self._cortex.get("nodes") or []
            if i < 0 or i >= len(nodes):
                return None
            n = nodes[i]
            text = self._texts[i] if i < len(self._texts) else n.get("preview")
            return {
                "i": n.get("i"),
                "id": n.get("id"),
                "lobe": n.get("lobe"),
                "symbol": n.get("symbol"),
                "source": n.get("source"),
                "type": n.get("type"),
                "title": n.get("title"),
                "preview": n.get("preview"),
                "text": text,
                "created_at": n.get("created_at"),
                "pos": n.get("pos"),
                "col": n.get("col"),
                "nbs": n.get("nbs") or [],
            }

    def query(self, text: str, *, top_k: int = 40) -> dict[str, Any]:
        from tools.memory_viz.hash_embed import resolve_embed_for_rag

        q = (text or "").strip()
        if not q:
            return {"query": "", "indices": [], "scores": [], "hits": []}
        with self._lock:
            matrix = self._vectors
            nodes = self._cortex.get("nodes") or []
            texts = self._texts
        qv = resolve_embed_for_rag()(q)
        ranked = top_k_cosine(qv, matrix, k=top_k)
        indices = [i for i, _ in ranked]
        scores = [float(s) for _, s in ranked]
        hits = []
        for i, s in ranked:
            if i < 0 or i >= len(nodes):
                continue
            n = nodes[i]
            hits.append(
                {
                    "i": i,
                    "id": n.get("id"),
                    "score": round(float(s), 6),
                    "title": n.get("title"),
                    "symbol": n.get("symbol"),
                    "lobe": n.get("lobe"),
                    "type": n.get("type"),
                    "preview": n.get("preview"),
                    "text_preview": (texts[i][:120] if i < len(texts) else n.get("preview")),
                }
            )
        return {
            "query": q,
            "top_k": top_k,
            "indices": indices,
            "scores": scores,
            "hits": hits,
        }


_STORE: CortexStore | None = None


def get_store() -> CortexStore:
    global _STORE
    if _STORE is None:
        _STORE = CortexStore()
        _STORE.load_demo()
    return _STORE


def reset_store_for_tests() -> CortexStore:
    global _STORE
    _STORE = CortexStore()
    return _STORE
