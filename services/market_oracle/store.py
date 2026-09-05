"""In-process + Redis store for market oracle snapshots."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from logger import log

_LOCK = threading.Lock()
_LATEST: dict[str, Any] | None = None
_HISTORY: list[dict[str, Any]] = []
_MAX_HISTORY = 50
_PROCESS_START = datetime.now(timezone.utc)

# Naming debt: tests isolate via OHLCV_CACHE_KEY_PREFIX (#319/#323). A shared
# REDIS_KEY_PREFIX would be cleaner; not renamed here to avoid an ohlcv_cache sweep.
_DEFAULT_KEY_PREFIX = "aria:"
_ENV_KEY_PREFIX = "OHLCV_CACHE_KEY_PREFIX"


def _key_prefix() -> str:
    return (os.environ.get(_ENV_KEY_PREFIX) or "").strip() or _DEFAULT_KEY_PREFIX


def _redis_key() -> str:
    return f"{_key_prefix()}market_oracle:latest"


def reset_for_tests() -> None:
    global _LATEST, _HISTORY, _PROCESS_START
    with _LOCK:
        _LATEST = None
        _HISTORY = []
    _PROCESS_START = datetime.now(timezone.utc)


def process_uptime_sec() -> float:
    return (datetime.now(timezone.utc) - _PROCESS_START).total_seconds()


def _redis_set(snapshot: dict[str, Any]) -> bool:
    try:
        from bus.redis_client import get_redis

        r = get_redis()
        if not r:
            return False
        r.set(
            _redis_key(),
            json.dumps(snapshot),
            ex=max(300, int(snapshot.get("ttl_sec") or 900) * 2),
        )
        return True
    except Exception as e:
        log(f"market_oracle redis set failed: {e}", "WARNING")
        return False


def _redis_get() -> dict[str, Any] | None:
    try:
        from bus.redis_client import get_redis

        r = get_redis()
        if not r:
            return None
        raw = r.get(_redis_key())
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def store_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    global _LATEST, _HISTORY
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be object")
    src = snapshot.get("source")
    if src not in (None, "market_oracle", "oracle"):
        raise ValueError("invalid source")
    snap = dict(snapshot)
    state = str(snap.get("state") or snap.get("regime") or "NEUTRAL").upper()
    snap["state"] = state
    snap["regime"] = state
    snap.setdefault("source", "market_oracle")
    snap.setdefault("as_of", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    snap.setdefault("size_mult", 1.0)
    snap.setdefault("sensor_policy", "active")
    snap.setdefault("features", {})
    snap["stored_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _LOCK:
        prev = _LATEST
        _LATEST = snap
        _HISTORY.append(snap)
        if len(_HISTORY) > _MAX_HISTORY:
            del _HISTORY[: len(_HISTORY) - _MAX_HISTORY]

    redis_ok = _redis_set(snap)
    return {
        "prev_state": (prev or {}).get("state") or (prev or {}).get("regime"),
        "state": snap.get("state"),
        "redis": redis_ok,
    }


def get_latest_snapshot(*, allow_redis: bool = True) -> dict[str, Any] | None:
    with _LOCK:
        if _LATEST is not None:
            return dict(_LATEST)
    if allow_redis:
        return _redis_get()
    return None


def snapshot_is_fresh(snapshot: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not snapshot:
        return False
    now = now or datetime.now(timezone.utc)
    as_of = snapshot.get("as_of") or snapshot.get("stored_at")
    if not as_of:
        return False
    try:
        ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    ttl = int(snapshot.get("ttl_sec") or 900)
    return (now - ts).total_seconds() <= ttl * 2


def status_line() -> str:
    snap = get_latest_snapshot()
    if not snap:
        return "Oracle: —"
    fresh = snapshot_is_fresh(snap)
    age = "ok" if fresh else "stale"
    st = snap.get("state") or snap.get("regime")
    return f"Oracle: {st} size={snap.get('size_mult')} ({age}, {snap.get('as_of', '?')})"
