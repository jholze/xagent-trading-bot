"""In-process cortex store: demo, mongo load, incremental add + live callbacks."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.memory_viz.demo_cortex import build_demo_cortex
from tools.memory_viz.lobes import lobe_legend
from tools.memory_viz.ranking import top_k_cosine

# Explicit denylist — never query these
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CortexStore:
    """Thread-safe cortex + vector matrix with incremental upsert."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cortex: dict[str, Any] = {}
        self._vectors: list[list[float]] = []
        self._texts: list[str] = []
        self._by_id: dict[str, int] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._source = "empty"
        self._revision = 0

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, event: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                pass

    @property
    def node_count(self) -> int:
        with self._lock:
            return len(self._cortex.get("nodes") or [])

    @property
    def is_demo(self) -> bool:
        with self._lock:
            return bool(self._cortex.get("demo"))

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def load_demo(self, *, variants_per_seed: int = 8) -> None:
        cortex, vectors = build_demo_cortex(variants_per_seed=variants_per_seed)
        self._install(cortex, vectors, source="demo")

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
        self._install(data, vectors, source=str(data.get("source") or "json"))

    def load_from_mongo(self, *, max_chunks: int | None = None) -> bool:
        from tools.memory_viz.mongo_source import load_cortex_from_mongo

        cortex, vectors = load_cortex_from_mongo(max_chunks=max_chunks)
        if not cortex or not cortex.get("nodes"):
            return False
        self._install(cortex, vectors, source="mongo")
        return True

    def _install(self, cortex: dict[str, Any], vectors: list[list[float]], *, source: str) -> None:
        nodes = list(cortex.get("nodes") or [])
        texts = list(cortex.get("texts") or [])
        while len(texts) < len(nodes):
            texts.append(str(nodes[len(texts)].get("preview") or ""))
        # reindex
        for i, n in enumerate(nodes):
            n["i"] = i
        by_id = {str(n.get("id")): i for i, n in enumerate(nodes)}
        with self._lock:
            self._cortex = cortex
            self._vectors = vectors
            self._texts = texts
            self._by_id = by_id
            self._source = source
            self._revision += 1
            self._cortex["node_count"] = len(nodes)
            self._cortex["source"] = source

    def add_node(
        self,
        node: dict[str, Any],
        vector: list[float],
        text: str,
    ) -> bool:
        """Append a node if id is new. Returns True if added."""
        cid = str(node.get("id") or "")
        if not cid:
            return False
        with self._lock:
            if cid in self._by_id:
                return False
            nodes = list(self._cortex.get("nodes") or [])
            idx = len(nodes)
            n = dict(node)
            n["i"] = idx
            nodes.append(n)
            self._vectors.append(list(vector))
            self._texts.append(str(text or n.get("preview") or ""))
            self._by_id[cid] = idx
            self._cortex["nodes"] = nodes
            self._cortex["texts"] = list(self._texts)
            self._cortex["node_count"] = len(nodes)
            self._cortex["built_at"] = _utc_now()
            self._cortex["demo"] = False if self._source == "mongo" else self._cortex.get("demo", False)
            self._revision += 1
        self._emit({"type": "node_added", "id": cid, "i": idx})
        return True

    def ingest_text(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Synthetic ingest (demo/tests/live inject) — same path as mongo watcher."""
        from tools.memory_viz.hash_embed import resolve_embed_for_rag
        from tools.memory_viz.mongo_source import chunk_doc_to_node

        meta = dict(metadata or {})
        body = (text or "").strip()
        if not body:
            return None
        cid = node_id or f"live_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        doc = {
            "_id": cid,
            "chunk_id": cid,
            "text": body,
            "metadata": meta,
            "created_at": _utc_now(),
            "embedding": list(resolve_embed_for_rag()(f"{meta.get('type', '')} {body}")),
        }
        node, vec, full = chunk_doc_to_node(doc, index=0)
        if not self.add_node(node, vec, full):
            return None
        return self.get_node(cid)

    def public_cortex(self) -> dict[str, Any]:
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
                "source": self._source,
                "revision": self._revision,
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
                "source": self._source,
                "revision": self._revision,
                "node_count": len(self._cortex.get("nodes") or []),
                "embedding_backend": self._cortex.get("embedding_backend"),
                "embedding_dim": self._cortex.get("embedding_dim"),
                "built_at": self._cortex.get("built_at"),
                "ws": True,
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

    def public_graph(
        self,
        *,
        knn: int = 5,
        min_sim: float = 0.12,
        max_nodes: int = 800,
    ) -> dict[str, Any]:
        """Knowledge graph snapshot for Graph mode (Mode 2)."""
        from tools.memory_viz.graph_build import build_memory_graph

        with self._lock:
            nodes = list(self._cortex.get("nodes") or [])
            vectors = list(self._vectors)
            rev = self._revision
            demo = bool(self._cortex.get("demo"))
            source = self._source
        g = build_memory_graph(
            nodes,
            vectors,
            knn=knn,
            min_sim=min_sim,
            max_nodes=max_nodes,
        )
        g["revision"] = rev
        g["demo"] = demo
        g["source"] = source
        g["mode"] = "graph"
        g["lobes"] = lobe_legend()
        return g

    def graph_links_for_id(self, node_id: str, *, knn: int = 5) -> list[dict[str, Any]]:
        from tools.memory_viz.graph_build import links_for_new_node

        with self._lock:
            i = self._by_id.get(str(node_id))
            if i is None:
                return []
            nodes = list(self._cortex.get("nodes") or [])
            vectors = list(self._vectors)
        return links_for_new_node(i, nodes, vectors, knn=knn)


_STORE: CortexStore | None = None


def get_store() -> CortexStore:
    global _STORE
    if _STORE is None:
        _STORE = CortexStore()
    return _STORE


def reset_store_for_tests() -> CortexStore:
    global _STORE
    _STORE = CortexStore()
    return _STORE
