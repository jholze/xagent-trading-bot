"""DCA sniper config + env kill switches."""

from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def dca_sniper_enabled(config: dict | None = None) -> bool:
    env = _env_bool("DCA_SNIPER_ENABLED")
    if env is not None:
        return env
    raw = config
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    sec = (raw or {}).get("dca_sniper") if isinstance(raw, dict) else {}
    if isinstance(sec, dict) and "enabled" in sec:
        return bool(sec.get("enabled"))
    return False


def dca_sniper_config(config: dict | None = None) -> dict[str, Any]:
    raw = config
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    sec = dict((raw or {}).get("dca_sniper") or {}) if isinstance(raw, dict) else {}
    notify = sec.get("notify_only")
    if notify is None:
        notify = False  # staging sharp default: execute
    # env can force notify-only (shadow) without code change
    env_notify = _env_bool("DCA_SNIPER_NOTIFY_ONLY")
    if env_notify is not None:
        notify = env_notify
    return {
        "enabled": dca_sniper_enabled(raw if isinstance(raw, dict) else None),
        "mode": str(sec.get("mode") or "live"),
        "notify_only": bool(notify),
        "max_focus_slots": int(sec.get("max_focus_slots") or 2),
        "poll_interval_sec": float(sec.get("poll_interval_sec") or 180),
        "poll_fallback_sec": float(sec.get("poll_fallback_sec") or 60),
        "ws_enabled": bool(sec.get("ws_enabled", True)),
        "heavy_min_score": float(sec.get("heavy_min_score") or 6.5),
        "min_meaningful_usdt": float(sec.get("min_meaningful_usdt") or 200),
        "min_cash_after_focus": float(sec.get("min_cash_after_focus") or 150),
        "soft_claim_enabled": bool(sec.get("soft_claim_enabled", True)),
        "soft_claim_max_pct_equity": float(sec.get("soft_claim_max_pct_equity") or 3.0),
        "fund_from_winner_enabled": bool(sec.get("fund_from_winner_enabled", True)),
        "max_fund_sells_per_cycle": int(sec.get("max_fund_sells_per_cycle") or 1),
        "exclude_grid": bool(sec.get("exclude_grid", True)),
        "disable_cycle_dca_when_enabled": bool(
            sec.get("disable_cycle_dca_when_enabled", True)
        ),
        "be_buffer_pct": float(sec.get("be_buffer_pct") or 2.0),
        "timeout_days": float(sec.get("timeout_days") or 14),
        "profile_f": dict(
            sec.get("profile_f")
            or {
                "major": 0.55,
                "volatile": 0.75,
                "meme": 0.8,
                "default": 0.65,
            }
        ),
        "max_single_add_usdt": float(sec.get("max_single_add_usdt") or 2500),
        "max_bag_pct_equity": float(sec.get("max_bag_pct_equity") or 5.0),
        "small_dca_usdt": float(sec.get("small_dca_usdt") or 500),
        "deep_analysis_cooldown_sec": float(sec.get("deep_analysis_cooldown_sec") or 300),
        "in_process_tick": bool(sec.get("in_process_tick", True)),
        "require_reclaim_for_dca": bool(sec.get("require_reclaim_for_dca", True)),
        "require_reclaim_for_heavy": bool(sec.get("require_reclaim_for_heavy", True)),
        "prefer_small_before_heavy": bool(sec.get("prefer_small_before_heavy", True)),
        "heavy_only_on_reclaim": bool(sec.get("heavy_only_on_reclaim", True)),
        "max_dd_pct_for_heavy": float(sec.get("max_dd_pct_for_heavy") or 55),
        "min_dd_pct_for_dca": float(sec.get("min_dd_pct_for_dca") or 12),
        "max_dd_pct_for_dca": float(sec.get("max_dd_pct_for_dca") or 55),
    }


def internal_token() -> str:
    return (
        (os.environ.get("DCA_SNIPER_TOKEN") or "").strip()
        or (os.environ.get("EXIT_WS_INTERNAL_TOKEN") or "").strip()
        or (os.environ.get("GAINER_SIGNAL_TOKEN") or "").strip()
    )


def bot_base_url() -> str:
    explicit = (
        os.environ.get("DCA_SNIPER_BOT_URL") or os.environ.get("BOT_INTERNAL_URL") or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("PORT") or "5000").strip()
    return f"http://127.0.0.1:{port}"
