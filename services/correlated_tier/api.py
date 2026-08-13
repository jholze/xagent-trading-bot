"""Public API: is this symbol's correlated group currently in selloff?"""

from __future__ import annotations

import time
from typing import Any

# Short in-process cache so one decision cycle does not hammer Redis.
_CACHE: dict[str, tuple[float, bool]] = {}
_CACHE_TTL_SEC = 5.0


def _cache_get(group: str) -> bool | None:
    row = _CACHE.get(group)
    if not row:
        return None
    exp, val = row
    if time.time() > exp:
        _CACHE.pop(group, None)
        return None
    return val


def _cache_set(group: str, active: bool) -> None:
    _CACHE[group] = (time.time() + _CACHE_TTL_SEC, bool(active))


def reset_correlated_tier_cache_for_tests() -> None:
    _CACHE.clear()


def correlated_tier_selloff_active(
    symbol: str,
    config_raw: dict | None = None,
) -> bool:
    """True when the symbol's resolved group has an active Redis selloff flag.

    Fail-open to False on any Redis/parse/resolution error (matches get_redis).
    """
    try:
        if config_raw is None:
            try:
                from core.config import get_bot_config

                config_raw = get_bot_config().raw
            except Exception:
                config_raw = {}

        from services.correlated_tier.config import correlated_tier_enabled

        if not correlated_tier_enabled(config_raw):
            return False

        from strategies.correlated_tier_overlay import resolve_correlated_group

        group = resolve_correlated_group(symbol, config_raw)
        if not group:
            return False

        cached = _cache_get(group)
        if cached is not None:
            return cached

        from bus.correlated_tier_flag import get_correlated_tier_flag

        payload: dict[str, Any] | None = get_correlated_tier_flag(
            group, config_raw=config_raw
        )
        active = bool(payload and payload.get("active"))
        _cache_set(group, active)
        return active
    except Exception:
        return False
