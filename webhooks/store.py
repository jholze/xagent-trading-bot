"""Persist external signal events — JSONL audit + optional Redis stream."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from bus.redis_client import get_redis
from logger import log
from webhooks.schemas import ExternalSignal

_lock = threading.Lock()
_rate_buckets: dict[str, deque[float]] = {}
_recent_events: list[dict[str, Any]] = []
_MAX_RECENT = 200


def _audit_path() -> Path:
    return Path("logs/signal_webhooks.jsonl")


def _rate_limit_ok(source: str, limit_per_min: int) -> bool:
    if limit_per_min <= 0:
        return True
    now = time.time()
    key = source or "generic"
    with _lock:
        bucket = _rate_buckets.setdefault(key, deque())
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= limit_per_min:
            return False
        bucket.append(now)
        return True


def append_audit(signal: ExternalSignal, *, status: str, detail: str = "") -> None:
    entry = {
        "logged_at": time.time(),
        "status": status,
        "detail": detail,
        **signal.as_dict(),
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    with _lock:
        _recent_events.append(entry)
        if len(_recent_events) > _MAX_RECENT:
            del _recent_events[: len(_recent_events) - _MAX_RECENT]


def publish_redis(signal: ExternalSignal, config_raw: dict | None = None) -> bool:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    arch = (config_raw or {}).get("architecture") or {}
    client = get_redis(arch.get("redis_url"), key_prefix=str(arch.get("key_prefix", "aria:")))
    if not client:
        return False
    stream = f"{str(arch.get('key_prefix', 'aria:'))}events.external_signals"
    payload = {k: json.dumps(v, default=str) if not isinstance(v, str) else v[:4000] for k, v in signal.as_dict().items()}
    try:
        client.xadd(stream, payload, maxlen=1000, approximate=True)
        return True
    except Exception as e:
        log(f"signal_webhook redis publish failed: {e}", "WARNING")
        return False


def ingest(
    signal: ExternalSignal,
    *,
    config_raw: dict | None = None,
    rate_limit_per_min: int = 10,
) -> tuple[bool, str]:
    if not _rate_limit_ok(signal.source, rate_limit_per_min):
        append_audit(signal, status="rejected", detail="rate_limit")
        return False, "rate_limit"

    append_audit(signal, status="accepted")
    publish_redis(signal, config_raw=config_raw)
    return True, "accepted"


def recent_events(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        return list(_recent_events[-limit:])


def reset_for_tests() -> None:
    with _lock:
        _rate_buckets.clear()
        _recent_events.clear()