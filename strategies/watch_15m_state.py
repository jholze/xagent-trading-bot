"""Persisted watch list for 15m entry sensor (not separate positions)."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from data_manager import atomic_write_json, get_data_file

_WATCH_FILE = "watch_15m_state.json"
_lock = threading.RLock()
_cache: dict | None = None


def _state_path() -> str:
    return get_data_file(_WATCH_FILE)


def _default_state() -> dict:
    return {"coins": {}, "last_reject_at": {}}


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        path = _state_path()
        if not os.path.exists(path):
            _cache = _default_state()
            return _cache
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = _default_state()
        if "coins" not in data:
            data["coins"] = {}
        if "last_reject_at" not in data:
            data["last_reject_at"] = {}
        _cache = data
        return _cache


def _save(data: dict) -> None:
    global _cache
    with _lock:
        _cache = data
        atomic_write_json(_state_path(), data)


def reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def set_watch(
    symbol: str,
    timeframe: str,
    *,
    reason: str = "",
    ttl_hours: float = 24.0,
) -> None:
    data = _load()
    now = datetime.now()
    data["coins"][symbol] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "reason": reason,
        "watched_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
    }
    _save(data)


def clear_watch(symbol: str) -> None:
    data = _load()
    data["coins"].pop(symbol, None)
    _save(data)


def is_watched(symbol: str) -> bool:
    entry = _load()["coins"].get(symbol)
    if not entry:
        return False
    try:
        expires = datetime.fromisoformat(str(entry["expires_at"]).replace("Z", ""))
        if datetime.now() >= expires:
            clear_watch(symbol)
            return False
    except Exception:
        pass
    return True


def get_watch_entry(symbol: str) -> dict | None:
    if not is_watched(symbol):
        return None
    return dict(_load()["coins"].get(symbol) or {})


def list_watched() -> list[dict]:
    prune_ttl()
    return [dict(v) for v in _load()["coins"].values()]


def prune_ttl(now: datetime | None = None) -> int:
    now = now or datetime.now()
    data = _load()
    removed = 0
    for sym in list(data["coins"].keys()):
        entry = data["coins"][sym]
        try:
            expires = datetime.fromisoformat(str(entry.get("expires_at", "")).replace("Z", ""))
            if now >= expires:
                data["coins"].pop(sym, None)
                removed += 1
        except Exception:
            data["coins"].pop(sym, None)
            removed += 1
    if removed:
        _save(data)
    return removed


def record_sensor_reject(symbol: str, when: datetime | None = None) -> None:
    data = _load()
    data.setdefault("last_reject_at", {})[symbol] = (when or datetime.now()).isoformat()
    _save(data)


def hours_since_sensor_reject(symbol: str, now: datetime | None = None) -> float | None:
    raw = _load().get("last_reject_at", {}).get(symbol)
    if not raw:
        return None
    now = now or datetime.now()
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", ""))
    except Exception:
        return None
    return (now - ts).total_seconds() / 3600.0


def max_watched_reached(max_coins: int) -> bool:
    if max_coins <= 0:
        return False
    prune_ttl()
    return len(_load()["coins"]) >= max_coins