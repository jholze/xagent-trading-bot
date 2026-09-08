"""In-process + optional Mongo snapshot for bot hot path (no external APIs)."""

from __future__ import annotations

import threading
import time
from typing import Any

from logger import log

_LOCK = threading.Lock()
_SNAP: dict[str, Any] = {}
_SNAP_AT = 0.0
_TTL = 120.0  # seconds — Hermes refreshes more often

COL_SNAP = "memory_macro_snapshot"


def publish_macro_snapshot(snap: dict[str, Any]) -> None:
    """Hermes writes; bot reads. Also best-effort Mongo for multi-process."""
    global _SNAP, _SNAP_AT
    payload = dict(snap)
    payload["published_at"] = time.time()
    with _LOCK:
        _SNAP = payload
        _SNAP_AT = time.time()
    try:
        from storage.mongo_client import get_database

        doc = {"_id": "global", **payload}
        get_database()[COL_SNAP].replace_one({"_id": "global"}, doc, upsert=True)
    except Exception as e:
        log(f"macro snapshot mongo write skipped: {e}", "DEBUG")


def get_macro_snapshot(*, max_age_sec: float | None = None) -> dict[str, Any]:
    """Fail-open empty dict when stale/missing."""
    global _SNAP, _SNAP_AT
    ttl = float(max_age_sec if max_age_sec is not None else _TTL)
    with _LOCK:
        age = time.time() - _SNAP_AT
        if _SNAP and age <= ttl:
            return dict(_SNAP)
    # try mongo
    try:
        from storage.mongo_client import get_database

        doc = get_database()[COL_SNAP].find_one({"_id": "global"})
        if doc:
            pub = float(doc.get("published_at") or 0)
            if pub and (time.time() - pub) <= max(ttl, 600):
                clean = {k: v for k, v in doc.items() if k != "_id"}
                with _LOCK:
                    _SNAP = clean
                    _SNAP_AT = time.time()
                return dict(clean)
    except Exception:
        pass
    return {}


def get_risk_multipliers(config: dict | None = None) -> dict[str, Any]:
    """Hot-path multipliers from snapshot — never raises, defaults 1.0."""
    try:
        from intelligence.macro.config import calendar_risk_config

        cr = calendar_risk_config(config)
    except Exception:
        cr = {}
    snap = get_macro_snapshot()
    if not snap:
        return {
            "calendar_mult": 1.0,
            "session_mult": 1.0,
            "pm_mult": 1.0,
            "fakeout_risk": 0.0,
            "calendar_risk": "",
            "session_risk": "",
            "pm_risk": "",
            "block_new_entries": False,
            "measured": False,
        }
    regime = snap.get("regime") or {}
    cal = snap.get("calendar") or {}
    session_mult = float(regime.get("session_mult") or snap.get("session_mult") or 1.0)
    calendar_mult = float(regime.get("calendar_mult") or snap.get("calendar_mult") or 1.0)
    pm_mult = float(snap.get("pm_mult") or 1.0)
    # clamp
    session_mult = max(0.0, min(1.5, session_mult))
    calendar_mult = max(0.0, min(1.5, calendar_mult))
    pm_mult = max(0.0, min(1.5, pm_mult))

    block = bool(cr.get("block_new_entries", False)) and bool(
        cal.get("in_pre_window") and cal.get("high_impact")
    )

    return {
        "calendar_mult": calendar_mult,
        "session_mult": session_mult,
        "pm_mult": pm_mult,
        "fakeout_risk": float(regime.get("fakeout_risk") or snap.get("fakeout_risk") or 0.0),
        "calendar_risk": str(cal.get("summary") or "")[:100],
        "session_risk": str(regime.get("regime") or "")[:80],
        "pm_risk": str((snap.get("pm") or {}).get("summary") or "")[:80],
        "block_new_entries": block,
        "next_event": cal.get("next_event"),
        "hours_to_event": cal.get("hours_to_event"),
        "measured": True if "measured" not in snap else bool(snap.get("measured")),
    }
