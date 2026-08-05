"""TTL cache for ATR% used by coin_aware_v1 entry policy (no Mongo)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class AtrPctCache:
    def __init__(self, ttl_sec: float = 600.0) -> None:
        self.ttl_sec = float(ttl_sec)
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, float]] = {}  # sym -> (atr_pct, ts)

    def get(self, symbol: str) -> float | None:
        with self._lock:
            hit = self._data.get(symbol)
            if not hit:
                return None
            val, ts = hit
            if time.time() - ts > self.ttl_sec:
                return None
            return float(val)

    def set(self, symbol: str, atr_pct: float) -> None:
        with self._lock:
            self._data[symbol] = (float(atr_pct), time.time())

    def snapshot(self) -> dict[str, float]:
        now = time.time()
        with self._lock:
            return {
                s: v
                for s, (v, ts) in self._data.items()
                if now - ts <= self.ttl_sec
            }

    def ensure_many(
        self,
        symbols: list[str],
        *,
        fetch_fn: Callable[[str], float | None],
    ) -> dict[str, float]:
        """Return atr map for symbols; fetch missing/expired via fetch_fn."""
        out: dict[str, float] = {}
        for sym in symbols:
            cached = self.get(sym)
            if cached is not None:
                out[sym] = cached
                continue
            try:
                val = fetch_fn(sym)
            except Exception:
                val = None
            if val is None:
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            self.set(sym, f)
            out[sym] = f
        return out
