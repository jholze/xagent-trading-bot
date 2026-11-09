"""In-process + Redis store for market oracle snapshots."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from bus.redis_keys import redis_key_prefix
from logger import log

_LOCK = threading.Lock()
_LATEST: dict[str, Any] | None = None
_HISTORY: list[dict[str, Any]] = []
_MAX_HISTORY = 50
_PROCESS_START = datetime.now(timezone.utc)


def _key_prefix() -> str:
    return redis_key_prefix()


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


# Order-path read (#765). Under 200 ms even when the socket hits the cap,
# so a timeout cannot sit on p99. No in-process snapshot is consulted.
ORDER_PATH_REDIS_TIMEOUT_SEC = 0.19

_order_client = None
_order_client_lock = threading.Lock()


class OracleReadTimeout(TimeoutError):
    """The order-path Redis read did not finish inside its budget."""


def reset_order_path_client() -> None:
    global _order_client
    with _order_client_lock:
        client = _order_client
        _order_client = None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _order_path_client():
    global _order_client
    import redis
    from bus.redis_client import redis_url_from_env

    with _order_client_lock:
        if _order_client is None:
            _order_client = redis.Redis.from_url(
                redis_url_from_env(),
                socket_timeout=ORDER_PATH_REDIS_TIMEOUT_SEC,
                socket_connect_timeout=ORDER_PATH_REDIS_TIMEOUT_SEC,
                decode_responses=True,
            )
        return _order_client


def get_order_path_redis_snapshot() -> dict[str, Any] | None:
    """Redis GET of the oracle snapshot. Does not read the in-process cache.

    A socket timeout raises ``OracleReadTimeout``. A missing key (Redis
    answered) returns None. A connection failure returns None so a process
    with no Redis still imports; it is not a timeout and it is not a cache hit.
    """
    import redis

    try:
        raw = _order_path_client().get(_redis_key())
    except (redis.TimeoutError, TimeoutError) as exc:
        raise OracleReadTimeout(str(exc)) from exc
    except Exception:
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


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
