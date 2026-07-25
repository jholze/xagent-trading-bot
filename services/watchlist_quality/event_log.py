"""Durable WQE event log for shadow/soft soak data (always-on by default).

Writes ``logs/wqe_events.jsonl`` (demo: same path under process CWD) with one
JSON object per line. Fail-open. Independent of observability.json_logs so
soak data is collected even when global JSON logs are off.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from logger import LOG_DIR, log

WQE_EVENTS_LOG = os.path.join(LOG_DIR, "wqe_events.jsonl")


def _enabled(config: dict | None = None) -> bool:
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        wq = (config or {}).get("watchlist_quality") or {}
        # default True when mode != off so shadow always produces data
        if "event_log" in wq:
            return bool(wq.get("event_log"))
        mode = str(wq.get("mode") or "off").lower()
        return mode in ("shadow", "soft", "enforce")
    except Exception:
        return True


def log_wqe_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    config: dict | None = None,
    level: str = "INFO",
) -> None:
    """Append structured event; also human line via logger.log."""
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": str(event_type or "wqe"),
        **(payload or {}),
    }
    # Human-readable summary line (always, for Railway logs)
    try:
        flat = " ".join(
            f"{k}={rec[k]}"
            for k in (
                "type",
                "mode",
                "symbol",
                "reason",
                "n_in",
                "scored",
                "soft_n",
                "quality_score",
                "quality_shadow_ai",
                "stance",
                "behavior_change",
            )
            if k in rec and rec[k] is not None
        )
        log(f"wqe_event {flat}", level)
    except Exception:
        pass

    if not _enabled(config):
        return
    try:
        from services.observability_store import append_jsonl, maybe_rotate_jsonl

        append_jsonl(WQE_EVENTS_LOG, rec)
        maybe_rotate_jsonl(WQE_EVENTS_LOG, max_bytes=8_000_000, keep_lines=5_000)
    except Exception as e:
        log(f"wqe event_log write failed: {e}", "DEBUG")


def log_sync_summary(summary: dict[str, Any], *, config: dict | None = None) -> None:
    """One row for full score sync + optional per-coin compact rows (top demotes/boosts)."""
    payload = {
        "mode": summary.get("mode"),
        "n_in": summary.get("n_in"),
        "scored": summary.get("scored"),
        "score_p50": summary.get("score_p50"),
        "score_p90": summary.get("score_p90"),
        "score_ai_p50": summary.get("score_ai_p50"),
        "n_T1_hint": summary.get("n_T1_hint"),
        "n_T2_hint": summary.get("n_T2_hint"),
        "n_T3_hint": summary.get("n_T3_hint"),
        "memory_soft_block": summary.get("memory_soft_block"),
        "memory_prefer": summary.get("memory_prefer"),
        "vol_low": summary.get("vol_low"),
        "ai_ok": summary.get("ai_ok"),
        "ai_error": summary.get("ai_error"),
        "soft_n": summary.get("soft_n"),
        "soft_vol_floor": summary.get("soft_vol_floor"),
        "behavior_change": summary.get("behavior_change"),
        "tenant_id": summary.get("tenant_id"),
        "updated_at": summary.get("updated_at"),
    }
    log_wqe_event("wqe_sync", payload, config=config)

    # Compact per-coin rows for analysis (all coins — soak needs full distribution)
    for c in summary.get("coins") or []:
        if not isinstance(c, dict):
            continue
        ai = c.get("ai") if isinstance(c.get("ai"), dict) else {}
        mem = c.get("memory") if isinstance(c.get("memory"), dict) else {}
        metrics = c.get("metrics") if isinstance(c.get("metrics"), dict) else {}
        log_wqe_event(
            "wqe_coin",
            {
                "mode": summary.get("mode"),
                "symbol": c.get("symbol"),
                "quality_score": c.get("quality_score"),
                "quality_shadow_ai": c.get("quality_shadow_ai"),
                "tier_hint": c.get("tier_hint") or c.get("tier"),
                "flags": c.get("flags"),
                "entry_bias": mem.get("entry_bias"),
                "hard_exclude_new_add": mem.get("hard_exclude_new_add"),
                "stance": ai.get("stance"),
                "adjust": ai.get("adjust"),
                "confidence": ai.get("confidence"),
                "ai_source": ai.get("source"),
                "ai_rationale": (ai.get("rationale") or "")[:200],
                "quote_vol_24h": metrics.get("quote_vol_24h"),
                "source": metrics.get("source"),
                "cmc_rank": metrics.get("cmc_rank"),
            },
            config=config,
        )


def log_buy_block(
    symbol: str,
    reason: str,
    *,
    source: str = "",
    mode: str = "",
    quality_score: Any = None,
    config: dict | None = None,
) -> None:
    log_wqe_event(
        "wqe_buy_block",
        {
            "symbol": symbol,
            "reason": reason,
            "source": source,
            "mode": mode,
            "quality_score": quality_score,
        },
        config=config,
    )


def log_soft_apply(
    *,
    mode: str,
    n_in: int,
    n_out: int,
    open_n: int,
    tenant_id: str = "default",
    config: dict | None = None,
) -> None:
    log_wqe_event(
        "wqe_soft_apply",
        {
            "mode": mode,
            "n_in": n_in,
            "n_out": n_out,
            "dropped": max(0, n_in - n_out),
            "open_n": open_n,
            "tenant_id": tenant_id,
        },
        config=config,
    )


def log_path() -> str:
    return WQE_EVENTS_LOG
