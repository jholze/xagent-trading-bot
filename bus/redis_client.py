"""Optional Redis client — returns None when unavailable (monolith keeps running)."""

from __future__ import annotations

import os
from typing import Optional

_client = None
_available: Optional[bool] = None


def redis_url_from_env() -> str:
    return (
        os.getenv("REDIS_URL")
        or os.getenv("ARCHITECTURE_REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )


def get_redis(url: str | None = None, key_prefix: str = "aria:"):
    """Lazy Redis connection; None if redis package missing or server down."""
    global _client, _available
    if _available is False:
        return None
    if _client is not None:
        return _client
    try:
        import redis  # type: ignore
    except ImportError:
        _available = False
        return None
    try:
        conn = redis.from_url(url or redis_url_from_env(), decode_responses=True)
        conn.ping()
        _client = conn
        _available = True
        return _client
    except Exception:
        _available = False
        return None


def reset_redis_client():
    """Test helper."""
    global _client, _available
    _client = None
    _available = None