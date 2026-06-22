"""Cross-cycle post dedup — Redis SET NX with in-memory fallback."""

from __future__ import annotations

import threading
import time

from bus.redis_client import get_redis

_mem_lock = threading.Lock()
_mem_seen: dict[str, float] = {}


def _mem_claim(key: str, ttl_sec: int) -> bool:
    now = time.time()
    with _mem_lock:
        expired = [k for k, exp in _mem_seen.items() if exp < now]
        for k in expired:
            del _mem_seen[k]
        if key in _mem_seen:
            return False
        _mem_seen[key] = now + ttl_sec
        return True


def try_claim_id(
    namespace: str,
    post_id: str,
    *,
    ttl_sec: int = 86400,
    key_prefix: str = "aria:",
    redis_url: str | None = None,
) -> bool:
    """Return True if this id was not seen before (caller should process)."""
    pid = str(post_id or "").strip()
    if not pid:
        return True
    key = f"{key_prefix}dedup:{namespace}:{pid}"
    client = get_redis(redis_url, key_prefix=key_prefix)
    if client:
        try:
            return bool(client.set(key, "1", nx=True, ex=ttl_sec))
        except Exception:
            pass
    return _mem_claim(key, ttl_sec)


def clear_memory():
    with _mem_lock:
        _mem_seen.clear()