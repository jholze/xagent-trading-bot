"""Impact observability for Market Oracle × Santiment fusion.

- Detect fusion state changes → optional Telegram (rate-limited)
- Count buy blocks / size cuts per cycle
- Cycle footer line for summaries /health
- Lightweight JSONL event log
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from logger import LOG_DIR, log

EVENTS_LOG = os.path.join(LOG_DIR, "market_policy_events.jsonl")

_LOCK = threading.Lock()
_last_notified: dict[str, Any] | None = None
_last_notify_ts: float = 0.0
_cycle: dict[str, int] = {
    "buy_blocks": 0,
    "size_cuts": 0,
    "size_cut_logged": 0,  # 0/1 — log at most one size_cut event per cycle
}


def _obs_cfg() -> dict:
    try:
        from core.config import get_bot_config

        return dict(get_bot_config().raw.get("observability") or {})
    except Exception:
        return {}


def state_change_notify_enabled() -> bool:
    cfg = _obs_cfg()
    if "market_context_state_notify" in cfg:
        return bool(cfg.get("market_context_state_notify"))
    # default on for Railway / when startup notify pattern exists
    return True


def min_notify_interval_sec() -> float:
    try:
        return float(_obs_cfg().get("market_context_notify_min_sec", 120))
    except Exception:
        return 120.0


def reset_cycle_counters() -> None:
    with _LOCK:
        _cycle["buy_blocks"] = 0
        _cycle["size_cuts"] = 0
        _cycle["size_cut_logged"] = 0


def note_buy_blocked(*, regime: str | None, source: str | None, rationale: str = "") -> None:
    with _LOCK:
        _cycle["buy_blocks"] += 1
    _append_event(
        {
            "event": "buy_blocked_global",
            "regime": regime,
            "source": source,
            "rationale": (rationale or "")[:200],
        }
    )


def note_size_cut(*, mult: float, regime: str | None = None) -> None:
    if mult >= 0.999:
        return
    with _LOCK:
        _cycle["size_cuts"] += 1
        first = _cycle["size_cut_logged"] == 0
        if first:
            _cycle["size_cut_logged"] = 1
    if first:
        _append_event(
            {
                "event": "size_cut_global",
                "size_mult": round(float(mult), 4),
                "regime": regime,
            }
        )


def cycle_counters() -> dict[str, int]:
    with _LOCK:
        return dict(_cycle)


def format_fusion_line(bias: dict[str, Any] | None = None) -> str:
    """One-line fusion status for cycle summary / health."""
    if bias is None:
        try:
            from services.market_policy_fusion import get_global_market_bias

            bias = get_global_market_bias()
        except Exception:
            return "Fusion: —"
    if not bias or not bias.get("active"):
        return "Fusion: off"
    reg = bias.get("regime") or "?"
    try:
        sm = float(bias.get("size_mult") if bias.get("size_mult") is not None else 1.0)
    except Exception:
        sm = 1.0
    sensor = bias.get("sensor_policy") or "active"
    src = bias.get("source") or ",".join(bias.get("sources") or []) or "?"
    warm = " warmup" if bias.get("warmup_active") else ""
    block = " block" if bias.get("block_buys") else ""
    ctr = cycle_counters()
    extra = ""
    if ctr.get("buy_blocks") or ctr.get("size_cuts"):
        extra = f" · blocks={ctr.get('buy_blocks', 0)} cuts={ctr.get('size_cuts', 0)}"
    return (
        f"Fusion: {reg} ×{sm:.2f} sensor={sensor} [{src}]{warm}{block}{extra}"
    )


def _append_event(record: dict[str, Any]) -> None:
    try:
        from services.observability_store import append_jsonl

        rec = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **record,
        }
        append_jsonl(EVENTS_LOG, rec)
    except Exception as e:
        log(f"market_policy event log failed: {e}", "DEBUG")


def maybe_notify_state_change(bias: dict[str, Any] | None = None) -> bool:
    """If fusion regime/size/sensor changed, Telegram once (rate-limited)."""
    if not state_change_notify_enabled():
        return False
    if bias is None:
        try:
            from services.market_policy_fusion import get_global_market_bias

            bias = get_global_market_bias()
        except Exception:
            return False

    snap = {
        "active": bool(bias.get("active")),
        "regime": str(bias.get("regime") or ""),
        "size_mult": round(float(bias.get("size_mult") if bias.get("size_mult") is not None else 1.0), 3),
        "sensor_policy": str(bias.get("sensor_policy") or "active"),
        "source": str(bias.get("source") or ""),
        "block_buys": bool(bias.get("block_buys")),
        "warmup_active": bool(bias.get("warmup_active")),
    }

    global _last_notified, _last_notify_ts
    with _LOCK:
        prev = _last_notified
        if prev is not None and (
            prev.get("regime") == snap["regime"]
            and prev.get("size_mult") == snap["size_mult"]
            and prev.get("sensor_policy") == snap["sensor_policy"]
            and prev.get("block_buys") == snap["block_buys"]
            and prev.get("active") == snap["active"]
        ):
            return False
        now = time.time()
        if prev is not None and (now - _last_notify_ts) < min_notify_interval_sec():
            # still update memory so we don't spam after interval with stale flip
            _last_notified = snap
            return False
        _last_notified = snap
        _last_notify_ts = now

    _append_event({"event": "fusion_state_change", **snap, "rationale": (bias.get("rationale") or "")[:200]})

    # Epic #72 C8: optional RAG index of fusion snapshots (default off)
    try:
        from intelligence.memory.rag_index import index_fusion_snapshot

        index_fusion_snapshot(bias)
    except Exception:
        pass

    prev_s = "—"
    if prev:
        prev_s = (
            f"{prev.get('regime') or 'off'} ×{prev.get('size_mult', 1):.2f} "
            f"sensor={prev.get('sensor_policy')}"
        )
    new_s = (
        f"{snap['regime'] or 'off'} ×{snap['size_mult']:.2f} "
        f"sensor={snap['sensor_policy']}"
    )
    if snap.get("warmup_active"):
        new_s += " warmup"
    if snap.get("block_buys"):
        new_s += " block"

    msg = (
        f"<b>Market context</b>\n"
        f"{prev_s}\n→ <b>{new_s}</b>\n"
        f"<i>{(bias.get('source') or '')}</i>\n"
        f"{(bias.get('rationale') or '')[:180]}"
    )
    try:
        from telegram_notifier import send_telegram_message

        send_telegram_message(msg, priority=None)
        log(f"market context state notify: {prev_s} → {new_s}", "INFO")
        return True
    except Exception as e:
        log(f"market context notify failed: {e}", "WARNING")
        return False


def observe_cycle_start(config_raw: dict | None = None) -> str:
    """Reset counters, sample bias, maybe notify; return fusion line."""
    reset_cycle_counters()
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias(config_raw)
    except Exception:
        bias = {}
    maybe_notify_state_change(bias)
    return format_fusion_line(bias)
