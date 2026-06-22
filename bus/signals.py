"""In-memory signal snapshot cache (optional Phase-4 fast path)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

_lock = threading.Lock()
_snapshot: dict[str, Any] = {}


def _watchlist_hash(symbols: list[str]) -> str:
    raw = ",".join(sorted(symbols))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SignalSnapshotStore:
    def publish(
        self,
        *,
        x_signals: list | None = None,
        cmc_signals: list | None = None,
        lc_signals: list | None = None,
        watchlist_symbols: list[str] | None = None,
        version: int | None = None,
        accuracy: dict | None = None,
    ) -> dict:
        now = time.time()
        payload = {
            "version": version or int(now),
            "x_signals": x_signals or [],
            "cmc_signals": cmc_signals or [],
            "lc_signals": lc_signals or [],
            "watchlist_hash": _watchlist_hash(watchlist_symbols or []),
            "created_at": now,
            "accuracy": accuracy or {},
        }
        with _lock:
            _snapshot.clear()
            _snapshot.update(payload)
        return self._redis_view(payload)

    def publish_objects(
        self,
        *,
        x_signals: list | None = None,
        cmc_signals: list | None = None,
        lc_signals: list | None = None,
        watchlist_symbols: list[str] | None = None,
        version: int | None = None,
        accuracy: dict | None = None,
    ) -> dict:
        """Store live signal objects for in-process consumers."""
        now = time.time()
        payload = {
            "version": version or int(now),
            "x_signals": x_signals or [],
            "cmc_signals": cmc_signals or [],
            "lc_signals": lc_signals or [],
            "watchlist_hash": _watchlist_hash(watchlist_symbols or []),
            "created_at": now,
            "accuracy": accuracy or {},
            "_objects": True,
        }
        with _lock:
            _snapshot.clear()
            _snapshot.update(payload)
        return self._redis_view(payload)

    @staticmethod
    def _redis_view(payload: dict) -> dict:
        def _ser(items):
            out = []
            for item in items or []:
                if isinstance(item, dict):
                    out.append(item)
                else:
                    out.append(getattr(item, "__dict__", {"repr": repr(item)}))
            return out[:50]

        return {
            "version": payload.get("version"),
            "x_signals": _ser(payload.get("x_signals")),
            "cmc_signals": _ser(payload.get("cmc_signals")),
            "lc_signals": _ser(payload.get("lc_signals")),
            "watchlist_hash": payload.get("watchlist_hash"),
            "created_at": payload.get("created_at"),
            "accuracy": payload.get("accuracy") or {},
        }

    def get(self, max_age_sec: float = 300.0) -> dict | None:
        with _lock:
            if not _snapshot:
                return None
            age = time.time() - float(_snapshot.get("created_at", 0))
            if age > max_age_sec:
                return None
            return dict(_snapshot)

    def get_signals(self, max_age_sec: float = 300.0) -> tuple[list, list, list] | None:
        snap = self.get(max_age_sec)
        if not snap:
            return None
        return snap.get("x_signals") or [], snap.get("cmc_signals") or [], snap.get("lc_signals") or []

    def clear(self):
        with _lock:
            _snapshot.clear()


signal_snapshot_store = SignalSnapshotStore()