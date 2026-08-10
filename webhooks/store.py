"""Persist external signal events — JSONL audit + optional Redis stream."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from bus.redis_client import get_redis
from logger import log
from webhooks.schemas import ExternalSignal

_lock = threading.Lock()
_rate_buckets: dict[str, deque[float]] = {}
_recent_events: list[dict[str, Any]] = []
_MAX_RECENT = 200
# Mirror logger.py decisions.jsonl defaults (50 MiB, keep 3 archives).
_AUDIT_MAX_BYTES = 52_428_800
_AUDIT_ROTATE_KEEP = 3


def _audit_path() -> Path:
    return Path("logs/signal_webhooks.jsonl")


def _maybe_rotate_audit() -> None:
    """Size-based rotation for signal_webhooks.jsonl (same approach as logger decisions)."""
    path = _audit_path()
    max_bytes = _AUDIT_MAX_BYTES
    keep = max(1, _AUDIT_ROTATE_KEEP)
    if max_bytes <= 0 or not path.is_file():
        return
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = path.parent
    archive = log_dir / f"signal_webhooks.{stamp}.jsonl"
    try:
        os.replace(path, archive)
    except OSError as e:
        log(f"signal_webhook audit rotate failed: {e}", "WARNING")
        return

    try:
        archives = sorted(
            f
            for f in os.listdir(log_dir)
            if f.startswith("signal_webhooks.")
            and f.endswith(".jsonl")
            and f != "signal_webhooks.jsonl"
        )
    except OSError:
        return
    while len(archives) > keep:
        try:
            os.remove(log_dir / archives.pop(0))
        except OSError:
            break


def _rate_limit_ok(client_ip: str, limit_per_min: int) -> bool:
    """Rate-limit by remote client IP (not attacker-controlled source string)."""
    if limit_per_min <= 0:
        return True
    now = time.time()
    key = (client_ip or "").strip() or "unknown"
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
    _maybe_rotate_audit()
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
    client_ip: str | None = None,
) -> tuple[bool, str]:
    if not _rate_limit_ok(client_ip or "unknown", rate_limit_per_min):
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