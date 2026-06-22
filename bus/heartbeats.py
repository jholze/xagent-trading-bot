"""Worker heartbeats (in-memory; Redis mirror when available)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from bus.redis_client import get_redis

_lock = threading.Lock()
_local: dict[str, dict] = {}


class HeartbeatRegistry:
    def beat(self, worker: str, meta: dict | None = None, *, ttl_sec: int = 120, key_prefix: str = "aria:"):
        now = datetime.now(timezone.utc).isoformat()
        payload = {"worker": worker, "at": now, "meta": meta or {}}
        with _lock:
            _local[worker] = {**payload, "expires_at": time.time() + ttl_sec}
        client = get_redis(key_prefix=key_prefix)
        if client:
            try:
                client.setex(f"{key_prefix}health:{worker}", ttl_sec, now)
            except Exception:
                pass

    def stale_workers(self, *, ttl_sec: int = 120) -> list[str]:
        cutoff = time.time()
        with _lock:
            return [w for w, p in _local.items() if p.get("expires_at", 0) < cutoff]

    def all_workers(self) -> dict[str, dict]:
        with _lock:
            return dict(_local)

    def clear(self):
        with _lock:
            _local.clear()


heartbeat_registry = HeartbeatRegistry()