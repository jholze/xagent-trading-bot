"""WQE-R9: lightweight counters for ops (in-process; fail-open)."""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {
    "wqe_scored_total": 0,
    "wqe_soft_dropped_total": 0,
    "wqe_buy_blocked_total": 0,
    "wqe_ai_ok": 0,
    "wqe_ai_error": 0,
}
_BLOCK_REASONS: dict[str, int] = {}


def note_scored(n: int = 1) -> None:
    with _LOCK:
        _COUNTERS["wqe_scored_total"] = int(_COUNTERS.get("wqe_scored_total") or 0) + int(n)


def note_soft_dropped(n: int = 1) -> None:
    with _LOCK:
        _COUNTERS["wqe_soft_dropped_total"] = int(_COUNTERS.get("wqe_soft_dropped_total") or 0) + int(n)


def note_buy_blocked(reason: str = "") -> None:
    with _LOCK:
        _COUNTERS["wqe_buy_blocked_total"] = int(_COUNTERS.get("wqe_buy_blocked_total") or 0) + 1
        key = (reason or "unknown")[:64]
        _BLOCK_REASONS[key] = int(_BLOCK_REASONS.get(key) or 0) + 1


def note_ai(ok: bool) -> None:
    with _LOCK:
        if ok:
            _COUNTERS["wqe_ai_ok"] = int(_COUNTERS.get("wqe_ai_ok") or 0) + 1
        else:
            _COUNTERS["wqe_ai_error"] = int(_COUNTERS.get("wqe_ai_error") or 0) + 1


def snapshot() -> dict[str, Any]:
    with _LOCK:
        out = dict(_COUNTERS)
        out["wqe_buy_blocked_by_reason"] = dict(_BLOCK_REASONS)
    try:
        from services.watchlist_quality.store import score_age_seconds

        out["wqe_score_age_seconds"] = score_age_seconds()
    except Exception:
        out["wqe_score_age_seconds"] = None
    return out


def reset_for_tests() -> None:
    with _LOCK:
        for k in list(_COUNTERS):
            _COUNTERS[k] = 0
        _BLOCK_REASONS.clear()
