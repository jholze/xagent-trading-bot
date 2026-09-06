"""Distributed ledger lock — thread lock + optional Redis SET NX."""

from __future__ import annotations

import threading
import time
import uuid

from bus.redis_client import get_redis, resolve_redis_url
from logger import log
from storage.errors import LedgerLockUnavailable

_meta_lock = threading.Lock()
_thread_locks: dict[tuple[str, str], threading.Lock] = {}
_hold_depth = threading.local()


def _thread_lock_for(tenant_id: str, scope: str) -> threading.Lock:
    """Process-local mutex for one (tenant, scope). Created once; never deleted."""
    key = (tenant_id, scope)
    with _meta_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


def _depth_map() -> dict[tuple[str, str], int]:
    depths = getattr(_hold_depth, "map", None)
    if depths is None:
        depths = {}
        _hold_depth.map = depths
    return depths


class LedgerLock:
    def __init__(
        self,
        scope: str,
        *,
        tenant_id: str | None = None,
        ttl_sec: int = 60,
        key_prefix: str = "aria:",
        redis_url: str | None = None,
        wait_sec: float = 15.0,
        enabled: bool = True,
        fail_closed: bool = True,
    ):
        from core.tenant_context import resolve_tenant_id

        self.scope = scope or "paper"
        self.tenant_id = resolve_tenant_id(tenant_id)
        self.ttl_sec = max(5, int(ttl_sec))
        self.key_prefix = key_prefix
        self.redis_url = redis_url
        self.wait_sec = max(0.0, float(wait_sec))
        self.enabled = enabled
        self.fail_closed = fail_closed
        self._redis_key = f"{key_prefix}lock:ledger:{self.tenant_id}:{self.scope}"
        self._token = uuid.uuid4().hex
        self._redis = None
        self._held_redis = False
        self._held_at: float | None = None
        self._nested = False

    def _abort_acquire(
        self,
        reason: str,
        *,
        waited_sec: float = 0.0,
        cause: BaseException | None = None,
    ):
        if self.fail_closed:
            _depth_map()[(self.tenant_id, self.scope)] = 0
            self._thread_lock.release()
            raise LedgerLockUnavailable(
                scope=self.scope,
                tenant_id=self.tenant_id,
                reason=reason,
                waited_sec=waited_sec,
                cause=cause,
            )
        log(f"Ledger lock fail_open ({self.scope}) reason={reason}", "WARNING")
        return self

    def __enter__(self):
        key = (self.tenant_id, self.scope)
        depths = _depth_map()
        if depths.get(key, 0) > 0:
            depths[key] += 1
            self._nested = True
            return self
        self._nested = False
        self._thread_lock = _thread_lock_for(self.tenant_id, self.scope)
        self._thread_lock.acquire()
        depths[key] = 1
        if not self.enabled:
            return self
        self._redis = get_redis(self.redis_url, key_prefix=self.key_prefix)
        if not self._redis:
            return self._abort_acquire("redis_unavailable", waited_sec=0.0)
        deadline = time.time() + self.wait_sec
        started = time.time()
        while True:
            try:
                if self._redis.set(self._redis_key, self._token, nx=True, ex=self.ttl_sec):
                    self._held_redis = True
                    self._held_at = time.monotonic()
                    return self
            except Exception as e:
                log(f"Ledger redis lock error ({self.scope}): {e}", "WARNING")
                return self._abort_acquire(
                    "redis_error",
                    waited_sec=max(0.0, time.time() - started),
                    cause=e,
                )
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        log(f"Ledger lock timeout ({self.scope})", "WARNING")
        return self._abort_acquire(
            "timeout", waited_sec=max(0.0, time.time() - started)
        )

    def __exit__(self, exc_type, exc, tb):
        key = (self.tenant_id, self.scope)
        depths = _depth_map()
        if self._nested:
            depths[key] = max(0, depths.get(key, 1) - 1)
            return False
        if self._held_redis and self._redis:
            try:
                current = self._redis.get(self._redis_key)
                if current == self._token:
                    self._redis.delete(self._redis_key)
                else:
                    held = 0.0
                    if self._held_at is not None:
                        held = time.monotonic() - self._held_at
                    log(
                        f"ledger lock lost while held ({self.scope}) hold_sec={held:.3f}",
                        "WARNING",
                    )
            except Exception:
                pass
        depths[key] = 0
        self._thread_lock.release()
        return False


def ledger_lock(scope: str | None = None, *, cfg=None, tenant_id: str | None = None):
    from core.config import get_bot_config
    from data_manager import resolve_ledger_scope

    cfg = cfg or get_bot_config()
    arch = cfg.architecture_config
    scope = scope or resolve_ledger_scope(cfg.trading_mode)
    return LedgerLock(
        scope,
        tenant_id=tenant_id,
        ttl_sec=int(arch.get("ledger_lock_ttl_sec", 60)),
        key_prefix=arch.get("key_prefix", "aria:"),
        redis_url=resolve_redis_url(arch.get("redis_url")),
        wait_sec=float(arch.get("ledger_lock_wait_sec", 15)),
        enabled=bool(arch.get("ledger_lock_enabled", True)),
        fail_closed=bool(arch.get("ledger_lock_fail_closed", True)),
    )
