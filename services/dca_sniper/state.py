"""Sniper focus / decisions — Redis preferred, file fallback."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "focus": [],
    "last_decisions": [],
    "last_cycle_at": None,
    "metrics": {
        "heavies_fired": 0,
        "fund_sells": 0,
        "skips": 0,
        "waits": 0,
        "cash_floor_breaches": 0,
        "wakes": 0,
        "deep_passes": 0,
        "deep_thin": 0,
        "deep_rich": 0,
        "deep_rag_hits": 0,
        "deep_with_facts": 0,
        "policy_skips": 0,
    },
}


def state_path() -> Path | None:
    p = (os.environ.get("DCA_SNIPER_STATE_PATH") or "").strip()
    if p:
        return Path(p)
    return Path("data/dca_sniper_state.json")


def load_state() -> dict[str, Any]:
    global _state
    # Redis first
    try:
        from services.dca_sniper.redis_bus import load_state_redis

        remote = load_state_redis()
        if isinstance(remote, dict) and remote:
            with _lock:
                _state.update(remote)
            return dict(_state)
    except Exception:
        pass
    path = state_path()
    if path and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                with _lock:
                    _state.update(raw)
        except Exception:
            pass
    return dict(_state)


def save_state() -> None:
    with _lock:
        snapshot = json.loads(json.dumps(_state, default=str))
    try:
        from services.dca_sniper.redis_bus import save_state_redis

        if save_state_redis(snapshot):
            # still mirror to file if path set
            pass
    except Exception:
        pass
    path = state_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_state() -> dict[str, Any]:
    with _lock:
        return json.loads(json.dumps(_state, default=str))


def open_focus_count() -> int:
    with _lock:
        return len(_state.get("focus") or [])


def focus_symbols() -> list[str]:
    with _lock:
        out = []
        for f in _state.get("focus") or []:
            if isinstance(f, dict) and f.get("symbol"):
                out.append(str(f["symbol"]))
        return out


def set_focus(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _state["focus"] = list(entries or [])
        _state["last_cycle_at"] = time.time()
    save_state()
    try:
        from services.dca_sniper.redis_bus import set_watch_symbols

        syms = [str(e.get("symbol")) for e in (entries or []) if e.get("symbol")]
        set_watch_symbols(syms)
    except Exception:
        pass


def record_deep_quality(quality: dict[str, Any] | None, *, policy_skip: bool = False) -> None:
    """Accumulate deep-pass quality metrics (call per analyzed candidate)."""
    q = quality if isinstance(quality, dict) else {}
    flags = q.get("flags") if isinstance(q.get("flags"), dict) else {}
    with _lock:
        m = _state.setdefault("metrics", {})
        m["deep_passes"] = int(m.get("deep_passes") or 0) + 1
        if q.get("thin"):
            m["deep_thin"] = int(m.get("deep_thin") or 0) + 1
        if q.get("rich"):
            m["deep_rich"] = int(m.get("deep_rich") or 0) + 1
        if flags.get("has_rag"):
            m["deep_rag_hits"] = int(m.get("deep_rag_hits") or 0) + 1
        if flags.get("has_facts"):
            m["deep_with_facts"] = int(m.get("deep_with_facts") or 0) + 1
        if policy_skip:
            m["policy_skips"] = int(m.get("policy_skips") or 0) + 1
    # do not save every candidate — batch save from add_decision / set_focus


def add_decision(decision: dict[str, Any], *, maxlen: int = 50) -> None:
    with _lock:
        buf = list(_state.get("last_decisions") or [])
        buf.insert(0, decision)
        _state["last_decisions"] = buf[:maxlen]
        m = _state.setdefault("metrics", {})
        act = str(decision.get("action") or "")
        if act in ("DCA_HEAVY", "DCA_SMALL") or act.startswith("DCA_"):
            if "HEAVY" in act:
                m["heavies_fired"] = int(m.get("heavies_fired") or 0) + 1
            else:
                m["smalls_fired"] = int(m.get("smalls_fired") or 0) + 1
        elif act == "FUND_SELL":
            m["fund_sells"] = int(m.get("fund_sells") or 0) + 1
        elif act == "SKIP":
            m["skips"] = int(m.get("skips") or 0) + 1
        elif act == "WAIT":
            m["waits"] = int(m.get("waits") or 0) + 1
        elif act == "WAKE":
            m["wakes"] = int(m.get("wakes") or 0) + 1
        # optional quality attached on decision
        q = decision.get("quality") if isinstance(decision.get("quality"), dict) else None
        if q is not None:
            flags = q.get("flags") if isinstance(q.get("flags"), dict) else {}
            m["deep_passes"] = int(m.get("deep_passes") or 0) + 1
            if q.get("thin"):
                m["deep_thin"] = int(m.get("deep_thin") or 0) + 1
            if q.get("rich"):
                m["deep_rich"] = int(m.get("deep_rich") or 0) + 1
            if flags.get("has_rag"):
                m["deep_rag_hits"] = int(m.get("deep_rag_hits") or 0) + 1
            if flags.get("has_facts"):
                m["deep_with_facts"] = int(m.get("deep_with_facts") or 0) + 1
        if decision.get("policy_skip"):
            m["policy_skips"] = int(m.get("policy_skips") or 0) + 1
    save_state()
    try:
        from services.dca_sniper.redis_bus import publish_event

        publish_event({"type": "decision", "decision": decision})
    except Exception:
        pass
