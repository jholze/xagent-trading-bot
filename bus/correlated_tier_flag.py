"""Redis-backed correlated-tier selloff flag (per group)."""

from __future__ import annotations

import json
import time
from typing import Any

from bus.redis_client import get_redis, resolve_redis_url


def _flag_key(key_prefix: str, group: str) -> str:
    safe = str(group or "default").strip().lower().replace(" ", "_")
    return f"{key_prefix}correlated_tier:{safe}:selloff"


def _arch(config_raw: dict | None) -> dict:
    return dict((config_raw or {}).get("architecture") or {})


def set_correlated_tier_flag(
    group: str,
    payload: dict[str, Any],
    *,
    config_raw: dict | None = None,
    ttl_sec: float | None = None,
) -> bool:
    """Write selloff flag JSON with TTL. Fail-open (returns False on error)."""
    arch = _arch(config_raw)
    key_prefix = str(arch.get("key_prefix", "aria:"))
    client = get_redis(resolve_redis_url(arch.get("redis_url")), key_prefix=key_prefix)
    if not client:
        return False
    if ttl_sec is None:
        try:
            from services.correlated_tier.config import correlated_tier_config

            ct = correlated_tier_config(config_raw)
            ttl_sec = float(ct.get("flag_ttl_sec") or max(15, float(ct.get("eval_interval_sec", 5)) * 3))
        except Exception:
            ttl_sec = 30.0
    ttl = max(5, int(ttl_sec))
    body = dict(payload or {})
    body.setdefault("updated_at", time.time())
    body["group"] = str(group)
    try:
        client.setex(
            _flag_key(key_prefix, group),
            ttl,
            json.dumps(body, separators=(",", ":")),
        )
        return True
    except Exception:
        return False


def get_correlated_tier_flag(
    group: str,
    *,
    config_raw: dict | None = None,
) -> dict[str, Any] | None:
    """Read selloff flag. Fail-open → None."""
    arch = _arch(config_raw)
    key_prefix = str(arch.get("key_prefix", "aria:"))
    client = get_redis(resolve_redis_url(arch.get("redis_url")), key_prefix=key_prefix)
    if not client:
        return None
    try:
        raw = client.get(_flag_key(key_prefix, group))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_correlated_tier_flag(
    group: str,
    *,
    config_raw: dict | None = None,
) -> bool:
    arch = _arch(config_raw)
    key_prefix = str(arch.get("key_prefix", "aria:"))
    client = get_redis(resolve_redis_url(arch.get("redis_url")), key_prefix=key_prefix)
    if not client:
        return False
    try:
        client.delete(_flag_key(key_prefix, group))
        return True
    except Exception:
        return False
