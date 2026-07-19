"""Read-only fetch of memory_rag_chunks — never ledger collections."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from tools.memory_viz.hash_embed import resolve_embed_for_rag
from tools.memory_viz.layout import position_for
from tools.memory_viz.lobes import classify_lobe, lobe_color
from tools.memory_viz.store import LEDGER_COLLECTIONS

ALLOWED_COLLECTION = "memory_rag_chunks"


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def mongo_uri() -> str:
    return (
        (os.environ.get("MONGO_URL") or "").strip()
        or (os.environ.get("MONGODB_URI") or "").strip()
    )


def mongo_db_name() -> str:
    return (os.environ.get("MONGODB_DB") or "xagent_test").strip() or "xagent_test"


def mongo_configured() -> bool:
    return bool(mongo_uri())


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_collection_allowed(name: str) -> None:
    n = str(name or "").strip()
    if n in LEDGER_COLLECTIONS or not n.startswith("memory_"):
        raise RuntimeError(f"memory_viz refused collection: {n}")
    if n != ALLOWED_COLLECTION:
        raise RuntimeError(f"memory_viz only allows {ALLOWED_COLLECTION}, got {n}")


def chunk_doc_to_node(
    doc: dict[str, Any],
    *,
    index: int,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> tuple[dict[str, Any], list[float], str]:
    """Convert a Mongo RAG doc → (public node, vector, full_text)."""
    embed = embed_fn or resolve_embed_for_rag()
    meta = dict(doc.get("metadata") or {})
    text = str(doc.get("text") or "")
    cid = str(doc.get("chunk_id") or doc.get("_id") or f"chunk_{index}")
    emb = list(doc.get("embedding") or [])
    if not emb or len(emb) < 8:
        emb = list(embed(f"{meta.get('type', '')} {text}"))
    lobe = classify_lobe(meta)
    title = (
        str(meta.get("title") or meta.get("event_type") or meta.get("type") or cid)[:120]
    )
    preview = text[:160]
    node = {
        "i": index,
        "id": cid,
        "pos": position_for(node_id=cid, lobe=lobe, embedding=emb),
        "col": lobe_color(lobe),
        "lobe": lobe,
        "symbol": str(meta.get("symbol") or ""),
        "source": str(meta.get("source") or meta.get("provider") or ""),
        "type": str(meta.get("type") or meta.get("event_type") or ""),
        "title": title,
        "preview": preview,
        "created_at": str(doc.get("created_at") or _utc_now()),
        "nbs": [],
    }
    return node, emb, text


def fetch_rag_docs(
    *,
    limit: int = 3000,
    since_created_at: str | None = None,
    exclude_ids: set[str] | None = None,
    client_factory=None,
) -> list[dict[str, Any]]:
    """Fetch newest memory_rag_chunks. Fail-open → []."""
    assert_collection_allowed(ALLOWED_COLLECTION)
    uri = mongo_uri()
    if not uri:
        return []
    try:
        if client_factory is not None:
            client = client_factory(uri)
        else:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        try:
            db = client[mongo_db_name()]
            col = db[ALLOWED_COLLECTION]
            q: dict[str, Any] = {}
            if since_created_at:
                q["created_at"] = {"$gt": since_created_at}
            cur = col.find(q, {"text": 1, "embedding": 1, "metadata": 1, "created_at": 1, "chunk_id": 1}).sort(
                "created_at", -1
            ).limit(int(limit))
            docs = list(cur)
        finally:
            try:
                client.close()
            except Exception:
                pass
    except Exception:
        return []

    out = []
    skip = exclude_ids or set()
    for d in docs:
        cid = str(d.get("chunk_id") or d.get("_id") or "")
        if cid and cid in skip:
            continue
        out.append(d)
    return out


def build_cortex_from_docs(
    docs: list[dict[str, Any]],
    *,
    demo: bool = False,
) -> tuple[dict[str, Any], list[list[float]]]:
    embed = resolve_embed_for_rag()
    # oldest first so indices stable-ish when appending newest later
    ordered = list(reversed(docs))
    nodes: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    texts: list[str] = []
    for i, doc in enumerate(ordered):
        node, vec, text = chunk_doc_to_node(doc, index=i, embed_fn=embed)
        nodes.append(node)
        vectors.append(vec)
        texts.append(text)
    dim = len(vectors[0]) if vectors else 0
    cortex = {
        "version": 1,
        "built_at": _utc_now(),
        "embedding_backend": "hash",
        "embedding_dim": dim,
        "demo": demo,
        "node_count": len(nodes),
        "nodes": nodes,
        "texts": texts,
        "source": "mongo" if not demo else "demo",
    }
    return cortex, vectors


def load_cortex_from_mongo(*, max_chunks: int | None = None) -> tuple[dict[str, Any], list[list[float]]]:
    lim = max_chunks
    if lim is None:
        try:
            lim = int(os.environ.get("MEMORY_VIZ_MAX_CHUNKS") or 3000)
        except (TypeError, ValueError):
            lim = 3000
    docs = fetch_rag_docs(limit=lim)
    if not docs:
        return {}, []
    return build_cortex_from_docs(docs, demo=False)
