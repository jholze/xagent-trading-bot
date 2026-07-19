"""Background poller: new memory_rag_chunks → store upsert → WebSocket broadcast."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from tools.memory_viz.mongo_source import (
    chunk_doc_to_node,
    fetch_rag_docs,
    mongo_configured,
)
from tools.memory_viz.store import CortexStore
from tools.memory_viz.ws_hub import get_hub


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


class MemoryWatcher:
    def __init__(
        self,
        store: CortexStore,
        *,
        poll_sec: float | None = None,
        fetch_fn: Callable[..., list[dict[str, Any]]] | None = None,
        broadcast_fn: Callable[[dict[str, Any]], int] | None = None,
    ) -> None:
        self.store = store
        self.poll_sec = poll_sec if poll_sec is not None else _env_float("MEMORY_VIZ_POLL_SEC", 5.0)
        self._fetch = fetch_fn or fetch_rag_docs
        self._broadcast = broadcast_fn or (lambda ev: get_hub().broadcast(ev))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._known: set[str] = set()
        self._last_created = ""
        self.polls = 0
        self.added_total = 0
        self.last_error = ""

    def seed_known_from_store(self) -> None:
        pub = self.store.public_cortex()
        self._known = {str(n.get("id")) for n in pub.get("nodes") or []}
        times = [str(n.get("created_at") or "") for n in pub.get("nodes") or []]
        self._last_created = max(times) if times else ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.seed_known_from_store()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memory-viz-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def poll_once(self) -> list[dict[str, Any]]:
        """One poll cycle; returns public nodes that were newly added."""
        self.polls += 1
        try:
            docs = self._fetch(
                limit=200,
                since_created_at=self._last_created or None,
                exclude_ids=self._known,
            )
        except Exception as e:
            self.last_error = str(e)[:200]
            return []

        # also catch inserts with same/older created_at but new ids
        if not docs:
            try:
                recent = self._fetch(limit=80, exclude_ids=self._known)
                docs = recent
            except Exception as e:
                self.last_error = str(e)[:200]
                return []

        new_public: list[dict[str, Any]] = []
        for doc in docs:
            cid = str(doc.get("chunk_id") or doc.get("_id") or "")
            if not cid or cid in self._known:
                continue
            # provisional index; store reindexes
            node, vec, text = chunk_doc_to_node(doc, index=0)
            added = self.store.add_node(node, vec, text)
            if not added:
                continue
            self._known.add(cid)
            ca = str(doc.get("created_at") or "")
            if ca > self._last_created:
                self._last_created = ca
            full = self.store.get_node(cid)
            if full:
                pub = {
                    "i": full.get("i"),
                    "id": full.get("id"),
                    "pos": full.get("pos"),
                    "col": full.get("col"),
                    "lobe": full.get("lobe"),
                    "symbol": full.get("symbol"),
                    "source": full.get("source"),
                    "type": full.get("type"),
                    "title": full.get("title"),
                    "preview": full.get("preview"),
                    "created_at": full.get("created_at"),
                    "nbs": full.get("nbs") or [],
                }
                new_public.append(pub)
                self.added_total += 1

        if new_public:
            all_links: list[dict] = []
            for p in new_public:
                all_links.extend(self.store.graph_links_for_id(str(p.get("id") or "")))
            self._broadcast(
                {
                    "type": "nodes_added",
                    "nodes": new_public,
                    "links": all_links,
                    "node_count": self.store.node_count,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        return new_public

    def _loop(self) -> None:
        while not self._stop.is_set():
            if mongo_configured() or self._fetch is not fetch_rag_docs:
                self.poll_once()
            self._stop.wait(max(1.0, float(self.poll_sec)))


_WATCHER: MemoryWatcher | None = None


def get_watcher() -> MemoryWatcher | None:
    return _WATCHER


def start_watcher(store: CortexStore, **kwargs) -> MemoryWatcher | None:
    global _WATCHER
    if not mongo_configured() and kwargs.get("fetch_fn") is None:
        # only auto-start for real mongo unless test injects fetch_fn
        if not kwargs.get("force"):
            return None
    _WATCHER = MemoryWatcher(store, **{k: v for k, v in kwargs.items() if k != "force"})
    _WATCHER.start()
    return _WATCHER


def stop_watcher() -> None:
    global _WATCHER
    if _WATCHER:
        _WATCHER.stop()
        _WATCHER = None
