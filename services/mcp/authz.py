from __future__ import annotations

import threading
import time
from dataclasses import dataclass

ROLES = ("owner", "operator", "observer")
ACTIONS = ("read", "trade", "lock", "config_read", "kill")
DEFAULT_TENANTS = ("default", "henry", "ctexp")
DEFAULT_WRITE_RATE_PER_MIN = 20

_WRITE_HITS: dict[str, list[float]] = {}
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    tenants: tuple[str, ...]
    caps: tuple[str, ...]
    status: str = "active"


def authorize(
    actor: Actor | None,
    action: str,
    tenant_id: str,
    *,
    enabled: bool = True,
    writes_enabled: bool = True,
) -> tuple[bool, str]:
    if not enabled:
        return False, "mcp_disabled"
    if actor is None or actor.status != "active":
        return False, "unauthorized"
    tid = str(tenant_id or "").strip()
    if "*" not in actor.tenants and tid not in actor.tenants:
        return False, "tenant_forbidden"
    if action not in actor.caps:
        return False, "forbidden"
    if action in ("trade", "lock") and not writes_enabled:
        return False, "writes_disabled"
    return True, ""


def _mcp_section(config_raw: dict | None) -> dict | None:
    if not isinstance(config_raw, dict):
        return None
    mcp = config_raw.get("mcp")
    return mcp if isinstance(mcp, dict) else None


def mcp_enabled(config_raw: dict | None) -> bool:
    mcp = _mcp_section(config_raw)
    if mcp is None:
        return False
    return bool(mcp.get("enabled"))


def mcp_writes_enabled(config_raw: dict | None) -> bool:
    mcp = _mcp_section(config_raw)
    if mcp is None:
        return False
    return bool(mcp.get("enabled")) and bool(mcp.get("allow_writes"))


def mcp_allow_live(config_raw: dict | None) -> bool:
    mcp = _mcp_section(config_raw)
    if mcp is None:
        return False
    return bool(mcp.get("allow_live"))


def mcp_allowed_tenants(config_raw: dict | None) -> list[str]:
    mcp = _mcp_section(config_raw)
    raw = (mcp or {}).get("tenants") if mcp is not None else None
    if not isinstance(raw, (list, tuple, set)):
        return list(DEFAULT_TENANTS)
    out = [str(t).strip() for t in raw if str(t).strip()]
    return out or list(DEFAULT_TENANTS)


def mcp_tenant_allowed(tenant_id: str, config_raw: dict | None) -> bool:
    tid = str(tenant_id or "").strip()
    if not tid:
        return False
    return tid in mcp_allowed_tenants(config_raw)


def mcp_write_rate_per_min(config_raw: dict | None = None) -> int:
    mcp = _mcp_section(config_raw)
    raw = (mcp or {}).get("write_rate_per_min") if mcp is not None else None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_WRITE_RATE_PER_MIN
    if n < 1:
        return DEFAULT_WRITE_RATE_PER_MIN
    return min(n, 120)


def mcp_live_writes_blocked(config_raw: dict | None) -> bool:
    if mcp_allow_live(config_raw):
        return False
    try:
        from core.simulated_trading import is_real_live_trading

        return bool(is_real_live_trading(config_raw or {}))
    except Exception:
        return False


def reset_write_rate() -> None:
    with _WRITE_LOCK:
        _WRITE_HITS.clear()


def check_write_rate(
    actor_id: str,
    *,
    per_min: int = DEFAULT_WRITE_RATE_PER_MIN,
    now: float | None = None,
) -> tuple[bool, str]:
    key = str(actor_id or "").strip() or "_"
    try:
        cap = int(per_min)
    except (TypeError, ValueError):
        cap = DEFAULT_WRITE_RATE_PER_MIN
    if cap < 1:
        return True, ""
    t = float(now if now is not None else time.time())
    cutoff = t - 60.0
    with _WRITE_LOCK:
        hits = [x for x in _WRITE_HITS.get(key, []) if x >= cutoff]
        if len(hits) >= cap:
            _WRITE_HITS[key] = hits
            return False, "rate_limited"
        hits.append(t)
        _WRITE_HITS[key] = hits
    return True, ""
