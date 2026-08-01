"""Config for realtime exit path (Gate WS)."""

from __future__ import annotations

from typing import Any


def exit_realtime_config(raw: dict | None = None) -> dict[str, Any]:
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    cfg = (raw or {}).get("exit_realtime")
    return dict(cfg) if isinstance(cfg, dict) else {}


def exit_realtime_enabled(raw: dict | None = None) -> bool:
    return bool(exit_realtime_config(raw).get("enabled"))


def exit_realtime_mode(raw: dict | None = None) -> str:
    mode = str(exit_realtime_config(raw).get("mode") or "shadow").strip().lower()
    if mode not in ("shadow", "live", "off"):
        return "shadow"
    return mode


def exit_realtime_sources(raw: dict | None = None) -> frozenset[str]:
    cfg = exit_realtime_config(raw)
    src = cfg.get("sources") or ["trailing_take_profit", "trailing_stop"]
    if not isinstance(src, (list, tuple)):
        src = ["trailing_take_profit", "trailing_stop"]
    return frozenset(str(s) for s in src)
