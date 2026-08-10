"""Optional Redis client — returns None when unavailable (monolith keeps running)."""

from __future__ import annotations

import os
import time
from typing import Optional

_client = None
_available: Optional[bool] = None
_last_fail_at: float = 0.0
_RETRY_COOLDOWN_SEC = 30.0


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

    After a failure, re-attempts once the cooldown has elapsed so a transient
    Redis restart can self-heal without restarting this process.
    """
    global _client, _available, _last_fail_at
    if _client is not None:
        return _client
    if _available is False:
        if (time.time() - _last_fail_at) < _RETRY_COOLDOWN_SEC:
            return None
        # Cooldown elapsed — allow another connection attempt.
        _available = None
    try:
        import redis  # type: ignore
    except ImportError:
        _available = False
        _last_fail_at = time.time()
        return None
    try:
        conn = redis.from_url(url or redis_url_from_env(), decode_responses=True)
        conn.ping()
        _client = conn
        _available = True
        return _client
    except Exception:
        _client = None
        _available = False
        _last_fail_at = time.time()
        return None


def reset_redis_client():
    """Test helper."""
    global _client, _available, _last_fail_at
    _client = None
    _available = None
    _last_fail_at = 0.0
