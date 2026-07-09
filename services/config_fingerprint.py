"""Hash of rule-relevant config for prod vs staging comparison."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_RULE_PATHS = (
    ("exit_sensor",),
    ("volatile_altcoin", "trailing_take_profit"),
    ("volatile_altcoin", "sell_rotation"),
    ("stable_altcoin", "trailing_take_profit"),
    ("stable_altcoin", "sell_rotation"),
    ("entry_guard",),
    ("entry_sensor_15m",),
    ("architecture", "sell_rotation"),
)


def _dig(cfg: dict, path: tuple[str, ...]) -> Any:
    cur: Any = cfg
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_rule_snapshot(config_raw: dict | None) -> dict:
    raw = config_raw or {}
    snap: dict[str, Any] = {}
    for path in _RULE_PATHS:
        val = _dig(raw, path)
        if val is not None:
            snap[".".join(path)] = val
    return snap


def config_fingerprint(config_raw: dict | None) -> str:
    snap = extract_rule_snapshot(config_raw)
    payload = json.dumps(snap, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]