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
    ) -> dict:
        now = time.time()
        payload = {
            "version": version or int(now),
            "x_signals": x_signals or [],
            "cmc_signals": cmc_signals or [],
            "lc_signals": lc_signals or [],
            "watchlist_hash": _watchlist_hash(watchlist_symbols or []),
            "created_at": now,
        }
        with _lock:
            _snapshot.clear()
            _snapshot.update(payload)
        return payload

    def get(self, max_age_sec: float = 300.0) -> dict | None:
        with _lock:
            if not _snapshot:
                return None
            age = time.time() - float(_snapshot.get("created_at", 0))
            if age > max_age_sec:
                return None
            return dict(_snapshot)

    def clear(self):
        with _lock:
            _snapshot.clear()


signal_snapshot_store = SignalSnapshotStore()