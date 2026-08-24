from __future__ import annotations


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
