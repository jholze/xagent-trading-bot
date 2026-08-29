"""In-process + optional Redis store for Santiment sidecar snapshots."""

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

REDIS_KEY = "aria:santiment:latest"


def reset_for_tests() -> None:
    global _LATEST, _HISTORY
    with _LOCK:
        _LATEST = None
        _HISTORY = []


def _redis_set(snapshot: dict[str, Any]) -> bool:
    try:
        from bus.redis_client import get_redis

        r = get_redis()
        if not r:
            return False
        r.set(REDIS_KEY, json.dumps(snapshot), ex=max(300, int(snapshot.get("ttl_sec") or 1800) * 2))
        return True
    except Exception as e:
        log(f"santiment redis set failed: {e}", "WARNING")
        return False


def _redis_get() -> dict[str, Any] | None:
    try:
        from bus.redis_client import get_redis

        r = get_redis()
        if not r:
            return None
        raw = r.get(REDIS_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def store_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate lightly and store; returns applied meta."""
    global _LATEST, _HISTORY
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be object")
    if snapshot.get("source") not in (None, "santiment"):
        raise ValueError("invalid source")
    regime = str(snapshot.get("regime") or "NEUTRAL").upper()
    snapshot = dict(snapshot)
    snapshot["regime"] = regime
    snapshot.setdefault("source", "santiment")
    snapshot.setdefault("as_of", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    snapshot.setdefault("size_mult", 1.0)
    snapshot.setdefault("sensor_policy", "active")
    snapshot.setdefault("confidence", 0.5)
    snapshot.setdefault("features", {})
    snapshot.setdefault("symbols", {})
    snapshot["stored_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _LOCK:
        prev = _LATEST
        _LATEST = snapshot
        _HISTORY.append(snapshot)
        if len(_HISTORY) > _MAX_HISTORY:
            del _HISTORY[: len(_HISTORY) - _MAX_HISTORY]

    redis_ok = _redis_set(snapshot)
    return {
        "prev_regime": (prev or {}).get("regime"),
        "regime": snapshot.get("regime"),
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
    ttl = int(snapshot.get("ttl_sec") or 1800)
    age = (now - ts).total_seconds()
    return age <= ttl * 2


def status_line() -> str:
    snap = get_latest_snapshot()
    if not snap:
        return "Santiment: —"
    fresh = snapshot_is_fresh(snap)
    age_note = "ok" if fresh else "stale"
    meta = snap.get("meta") if isinstance(snap.get("meta"), dict) else {}
    lag = meta.get("data_lag_days_max")
    lag_s = f"lag={lag}d" if lag is not None else "lag=?"
    n_ok = len(meta.get("metrics_ok") or [])
    n_fail = len(meta.get("metrics_failed") or [])
    lev = "lev" if meta.get("leverage_fresh") else (
        "lev_research" if meta.get("research_only") else "lev—"
    )
    return (
        f"Santiment: {snap.get('regime')} size={snap.get('size_mult')} "
        f"{lag_s} ok={n_ok} fail={n_fail} {lev} "
        f"({age_note}, {snap.get('as_of', '?')})"
    )
