"""Worker heartbeats (in-memory; Redis mirror when available)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from bus.redis_client import get_redis

_lock = threading.Lock()
_local: dict[str, dict] = {}
_now = time.time


class HeartbeatRegistry:
    def beat(self, worker: str, meta: dict | None = None, *, ttl_sec: int = 120, key_prefix: str = "aria:"):
        now = datetime.now(timezone.utc).isoformat()
        payload = {"worker": worker, "at": now, "meta": meta or {}}
        with _lock:
            _local[worker] = {**payload, "expires_at": _now() + ttl_sec}
        client = get_redis(key_prefix=key_prefix)
        if client:
            try:
                client.setex(f"{key_prefix}health:{worker}", ttl_sec, now)
            except Exception:
                pass

    def drop(self, worker: str) -> None:
        """Remove a worker from the local registry (stops permanent stale spam)."""
        with _lock:
            _local.pop(worker, None)

    def stale_workers(self, *, ttl_sec: int = 120) -> list[str]:
        cutoff = _now()
        with _lock:
            return [w for w, p in _local.items() if p.get("expires_at", 0) < cutoff]

    def all_workers(self) -> dict[str, dict]:
        with _lock:
            return dict(_local)

    def clear(self):
        with _lock:
            _local.clear()

    def redis_alive(
        self,
        worker: str,
        *,
        key_prefix: str = "aria:",
        max_age_sec: float | None = None,
    ) -> bool:
        """True if Redis health key exists (and optional max age). Fail-open if Redis down."""
        client = get_redis(key_prefix=key_prefix)
        if not client:
            return True
        key = f"{key_prefix}health:{worker}"
        try:
            val = client.get(key)
            if not val:
                return False
            if max_age_sec is None:
                return True
            try:
                raw = str(val).replace("Z", "+00:00")
                ts = datetime.fromisoformat(raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                return age <= float(max_age_sec)
            except Exception:
                return True
        except Exception:
            return True


heartbeat_registry = HeartbeatRegistry()
