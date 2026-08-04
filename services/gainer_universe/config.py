"""Config for gainer universe — fail-closed when disabled."""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    # off | shadow (scan+log only) | trade_expand (inject into trade universe)
    "mode": "off",
    "quote": "USDT",
    "poll_sec": 60,
    "daily_refresh_sec": 900,
    "universe_top_by_volume": 250,
    "min_volume_usdt_24h": 500_000,
    "blacklist_suffixes": ["3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR"],
    "blacklist_bases": [],
    # stock tokens allowed by default (empty name keywords)
    "blacklist_name_keywords": [],
    "live_top_n": 50,
    "daily_history_days": 10,
    "daily_top_max": 80,
    "min_day_ret_pct": 3.0,
    "daily_min_volume": 300_000,
    "prev_top_ttl_hours": 36,
    "enable_continuation": True,
    "streak_min_days_in_top20": 2,
    "streak_lookback_days": 3,
    "continuation_max_chase_pct_today": 15.0,
    "expand_inject_max": 40,
    "trade_max_with_expand": 80,
    "scan_workers": 8,
    # Issue #162 — live heat into trade + DE priority + prev-day chase guard
    "live_heat_trade": True,
    "live_heat_min_pct": 8.0,
    "live_heat_max_pct": 35.0,
    "live_heat_ttl_hours": 10.0,
    "scan_prefer_gainer": True,
    "chase_guard_enabled": True,
    "chase_max_gain_from_prev_close_pct": 18.0,
    "chase_guard_sources": ["gate_prev_top"],
    # WS board identify (shadow logs only — no auto-buy)
    "ws_board": {
        "enabled": False,
        "mode": "shadow",
        "max_watch": 40,
        "log_top_n": 15,
        "log_interval_sec": 30,
        "min_pct_to_rank": 5.0,
    },
}


def gainer_universe_config(config: dict | None = None) -> dict[str, Any]:
    raw: dict = {}
    if isinstance(config, dict):
        sec = config.get("gainer_universe")
        if isinstance(sec, dict):
            raw = sec
    out = {**_DEFAULTS, **raw}
    out["enabled"] = bool(out.get("enabled", False))
    mode = str(out.get("mode") or "off").strip().lower()
    if mode not in ("off", "shadow", "trade_expand"):
        mode = "off"
    out["mode"] = mode
    # normalize lists
    for key in ("blacklist_suffixes", "blacklist_bases", "blacklist_name_keywords"):
        v = out.get(key) or []
        if not isinstance(v, list):
            v = []
        out[key] = [str(x) for x in v]
    return out


def gainer_universe_enabled(config: dict | None = None) -> bool:
    cfg = gainer_universe_config(config)
    return bool(cfg.get("enabled")) and cfg.get("mode") != "off"


def gainer_trade_expand_enabled(config: dict | None = None) -> bool:
    cfg = gainer_universe_config(config)
    return bool(cfg.get("enabled")) and cfg.get("mode") == "trade_expand"
