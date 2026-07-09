"""Normalized external signal payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_TYPES = frozenset({
    "volume_spike",
    "price_breakout",
    "news_alert",
    "generic",
})


def normalize_symbol(raw: str | None) -> str:
    sym = str(raw or "").strip().upper()
    if not sym:
        return ""
    sym = sym.replace("-", "").replace("_", "")
    if sym.endswith("USDT") and "/" not in sym:
        base = sym[:-4]
        if base:
            return f"{base}/USDT"
    if "/" not in sym:
        return f"{sym}/USDT"
    return sym


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExternalSignal:
    source: str
    symbol: str
    event_type: str = "generic"
    strength: float = 0.5
    timestamp: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)

    def reason(self) -> str:
        return f"webhook:{self.source}:{self.event_type}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "strength": float(self.strength),
            "timestamp": self.timestamp,
            "raw": self.raw,
        }


def clamp_strength(value: Any, default: float = 0.5) -> float:
    try:
        strength = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, strength))


def normalize_event_type(raw: str | None) -> str:
    et = str(raw or "generic").strip().lower().replace("-", "_").replace(" ", "_")
    if et in EVENT_TYPES:
        return et
    aliases = {
        "vol_spike": "volume_spike",
        "volume": "volume_spike",
        "breakout": "price_breakout",
        "price_alert": "price_breakout",
        "news": "news_alert",
    }
    return aliases.get(et, "generic")