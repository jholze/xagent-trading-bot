from __future__ import annotations

from dataclasses import dataclass

ROLES = ("owner", "operator", "observer")
ACTIONS = ("read", "trade", "lock", "config_read", "kill")


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
