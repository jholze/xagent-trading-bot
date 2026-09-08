"""Optional Redis client — returns None when unavailable (monolith keeps running)."""

from __future__ import annotations

import os
import time
from typing import Optional

REDIS_RETRY_COOLDOWN_SEC = 30

_client = None
_available: Optional[bool] = None
_unavailable_until: float = 0.0


def redis_url_from_env() -> str:
    return (
        os.getenv("REDIS_URL")
        or os.getenv("ARCHITECTURE_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )


def resolve_redis_url(config_url: str | None = None) -> str:
    """Prefer Railway/cloud REDIS_URL over localhost defaults from config.json."""
    env_url = (os.getenv("REDIS_URL") or os.getenv("ARCHITECTURE_REDIS_URL") or "").strip()
    if env_url:
        return env_url
    if config_url:
        return str(config_url)
    return redis_url_from_env()


def get_redis(url: str | None = None, key_prefix: str = "aria:"):
    """Lazy Redis connection; None if redis package missing or server down.

    After a failure, returns None for REDIS_RETRY_COOLDOWN_SEC then retries.
    A successful connect clears the cooldown. Success-path caching is unchanged.
    """
    global _client, _available, _unavailable_until
    if _available is False:
        if time.monotonic() < _unavailable_until:
            return None
        _available = None
        _client = None
    if _client is not None:
        return _client
    try:
        import redis  # type: ignore
    except ImportError:
        _available = False
        _unavailable_until = time.monotonic() + REDIS_RETRY_COOLDOWN_SEC
        return None
    try:
        conn = redis.from_url(url or redis_url_from_env(), decode_responses=True)
        conn.ping()
        _client = conn
        _available = True
        _unavailable_until = 0.0
        return _client
    except Exception:
        _available = False
        _unavailable_until = time.monotonic() + REDIS_RETRY_COOLDOWN_SEC
        return None


def reset_redis_client():
    """Test helper."""
    global _client, _available, _unavailable_until
    _client = None
    _available = None
    _unavailable_until = 0.0
