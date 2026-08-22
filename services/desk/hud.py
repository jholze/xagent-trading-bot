"""Desk HUD helpers. v0: kill-switch only."""

from __future__ import annotations


def desk_enabled(config_raw: dict | None) -> bool:
    return bool(((config_raw or {}).get("desk") or {}).get("enabled"))
