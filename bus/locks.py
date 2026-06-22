"""Distributed ledger lock — thread lock + optional Redis SET NX."""

from __future__ import annotations

import threading
import time
import uuid

from bus.redis_client import get_redis
from logger import log

_thread_lock = threading.Lock()


class LedgerLock:
    def __init__(
        self,
        scope: str,
        *,
        ttl_sec: int = 30,
        key_prefix: str = "aria:",
        redis_url: str | None = None,
        wait_sec: float = 15.0,
        enabled: bool = True,
    ):
        self.scope = scope or "paper"
        self.ttl_sec = max(5, int(ttl_sec))
        self.key_prefix = key_prefix
        self.redis_url = redis_url
        self.wait_sec = max(0.0, float(wait_sec))
        self.enabled = enabled
        self._redis_key = f"{key_prefix}lock:ledger:{self.scope}"
        self._token = uuid.uuid4().hex
        self._redis = None
        self._held_redis = False

    def __enter__(self):
        _thread_lock.acquire()
        if not self.enabled:
            return self
        self._redis = get_redis(self.redis_url, key_prefix=self.key_prefix)
        if not self._redis:
            return self
        deadline = time.time() + self.wait_sec
        while time.time() < deadline:
            try:
                if self._redis.set(self._redis_key, self._token, nx=True, ex=self.ttl_sec):
                    self._held_redis = True
                    return self
            except Exception as e:
                log(f"Ledger redis lock error ({self.scope}): {e}", "WARNING")
                return self
            time.sleep(0.05)
        log(f"Ledger lock timeout ({self.scope})", "WARNING")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._held_redis and self._redis:
            try:
                current = self._redis.get(self._redis_key)
                if current == self._token:
                    self._redis.delete(self._redis_key)
            except Exception:
                pass
        _thread_lock.release()
        return False


def ledger_lock(scope: str | None = None, *, cfg=None):
    from core.config import get_bot_config
    from data_manager import resolve_ledger_scope

    cfg = cfg or get_bot_config()
    arch = cfg.architecture_config
    scope = scope or resolve_ledger_scope(cfg.trading_mode)
    return LedgerLock(
        scope,
        ttl_sec=int(arch.get("ledger_lock_ttl_sec", 30)),
        key_prefix=arch.get("key_prefix", "aria:"),
        redis_url=arch.get("redis_url"),
        wait_sec=float(arch.get("ledger_lock_wait_sec", 15)),
        enabled=bool(arch.get("ledger_lock_enabled", True)),
    )