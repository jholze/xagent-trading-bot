"""Hot-path profile cache — fail-open, no Weaviate."""

from __future__ import annotations

import threading
import time
from typing import Any

from intelligence.memory.models import CoinProfile
from intelligence.memory.store import MemoryStore, memory_enabled, resolve_memory_scope

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, CoinProfile | None]] = {}
_TTL = 60.0


def invalidate_cache(symbol: str | None = None) -> None:
    with _LOCK:
        if symbol is None:
            _CACHE.clear()
        else:
            keys = [k for k in _CACHE if k.endswith(f"|{symbol}")]
            for k in keys:
                _CACHE.pop(k, None)


def get_coin_profile(
    symbol: str,
    *,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
    store: MemoryStore | None = None,
    config: dict | None = None,
) -> CoinProfile | None:
    if not memory_enabled(config):
        return None
    ledger_scope = resolve_memory_scope(ledger_scope)

    key = f"{tenant_id}|{ledger_scope}|{symbol}"
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] <= _TTL:
            return hit[1]

    store = store or MemoryStore()
    prof = store.get_profile(symbol, ledger_scope=ledger_scope, tenant_id=tenant_id)
    with _LOCK:
        _CACHE[key] = (now, prof)
    return prof


def get_size_bias(
    symbol: str,
    *,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
    config: dict | None = None,
) -> float:
    """Fail-open 1.0 when missing/disabled."""
    prof = get_coin_profile(
        symbol, ledger_scope=ledger_scope, tenant_id=tenant_id, config=config
    )
    if not prof:
        return 1.0
    return float(prof.size_bias)


def get_entry_bias(symbol: str, **kwargs) -> str:
    prof = get_coin_profile(symbol, **kwargs)
    if not prof:
        return "neutral"
    return prof.entry_bias or "neutral"
