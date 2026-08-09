"""In-process + optional file state for sniper focus / last decisions."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "focus": [],  # list of {symbol, timeframe, since, usdt}
    "last_decisions": [],  # ring buffer
    "last_cycle_at": None,
    "metrics": {
        "heavies_fired": 0,
        "fund_sells": 0,
        "skips": 0,
        "waits": 0,
        "cash_floor_breaches": 0,
    },
}


def state_path() -> Path | None:
    p = (os.environ.get("DCA_SNIPER_STATE_PATH") or "").strip()
    if p:
        return Path(p)
    return Path("data/dca_sniper_state.json")


def load_state() -> dict[str, Any]:
    global _state
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
    path = state_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            path.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_state() -> dict[str, Any]:
    with _lock:
        return json.loads(json.dumps(_state))


def open_focus_count() -> int:
    with _lock:
        return len(_state.get("focus") or [])


def set_focus(entries: list[dict[str, Any]]) -> None:
    with _lock:
        _state["focus"] = list(entries or [])
        _state["last_cycle_at"] = time.time()
    save_state()


def add_decision(decision: dict[str, Any], *, maxlen: int = 50) -> None:
    with _lock:
        buf = list(_state.get("last_decisions") or [])
        buf.insert(0, decision)
        _state["last_decisions"] = buf[:maxlen]
        m = _state.setdefault("metrics", {})
        act = str(decision.get("action") or "")
        if act == "DCA_HEAVY":
            m["heavies_fired"] = int(m.get("heavies_fired") or 0) + 1
        elif act == "FUND_SELL":
            m["fund_sells"] = int(m.get("fund_sells") or 0) + 1
        elif act == "SKIP":
            m["skips"] = int(m.get("skips") or 0) + 1
        elif act == "WAIT":
            m["waits"] = int(m.get("waits") or 0) + 1
    save_state()
