"""Memory gates for 15m entry sensor (pure, fail-open).

Blocks or shrinks sensor BUY when coin facts / profile say the name is toxic —
proactive BDX/LAB-class protection, no Grok on hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SensorMemoryVerdict:
    allow: bool
    reason: str = ""
    size_mult: float = 1.0


def _flag(flags: Any, name: str) -> bool:
    return bool(getattr(flags, name, False))


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        return float(v if v is not None else default)
    except (TypeError, ValueError):
        return float(default)


def apply_sensor_memory_entry_policy(
    *,
    flags: Any = None,
    entry_bias: str = "neutral",
    cfg: dict | None = None,
) -> SensorMemoryVerdict:
    """Decide if a sensor BUY should fire given memory.

    order: hard blocks → soft_block → size-downs (structure / flow).
    """
    cfg = dict(cfg or {})
    if not cfg.get("memory_enabled", True):
        return SensorMemoryVerdict(allow=True)

    bias = str(entry_bias or "neutral").lower()
    if bias == "soft_block" and cfg.get("memory_honor_soft_block", True):
        return SensorMemoryVerdict(
            allow=False,
            reason="memory: profile soft_block (prior gross loss / weak history)",
        )

    if flags is None:
        return SensorMemoryVerdict(allow=True)

    if _flag(flags, "hard_negative") and cfg.get("memory_block_hard_negative", True):
        return SensorMemoryVerdict(
            allow=False,
            reason="memory: hard_negative (hack/exploit/sec) — no sensor entry",
        )

    if _flag(flags, "structure_risk") and cfg.get("memory_block_structure_risk", True):
        return SensorMemoryVerdict(
            allow=False,
            reason="memory: structure_risk (weak vs market) — no sensor chase",
        )

    if _flag(flags, "unlock") and cfg.get("memory_block_unlock", True):
        return SensorMemoryVerdict(
            allow=False,
            reason="memory: unlock/supply overhang — no sensor entry",
        )

    # Soft size-down (still allow) — flow-only pumps
    mult = 1.0
    reasons: list[str] = []
    if _flag(flags, "flow_only") and cfg.get("memory_size_down_flow_only", True):
        mult *= _f(cfg, "memory_flow_only_size_mult", 0.5)
        reasons.append("flow_only_size")
    if _flag(flags, "profit_taking") and cfg.get("memory_size_down_profit_taking", True):
        mult *= _f(cfg, "memory_profit_taking_size_mult", 0.7)
        reasons.append("profit_taking_size")

    mult = max(0.25, min(1.0, mult))
    if mult < 0.999:
        return SensorMemoryVerdict(
            allow=True,
            reason="memory: " + "+".join(reasons),
            size_mult=mult,
        )
    return SensorMemoryVerdict(allow=True)
