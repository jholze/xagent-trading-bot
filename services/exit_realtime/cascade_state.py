"""In-memory per-side cascade timers. Does not read or write oracle state."""

from __future__ import annotations

from typing import Any


class CascadeState:
    """One timer per side. Starts only after a real fill. No BUY/SHORT gate."""

    def __init__(self, cooldown_sec: float = 600.0) -> None:
        self.cooldown_sec = float(cooldown_sec or 600.0)
        self.last_fill_mono: dict[str, float | None] = {"long": None, "short": None}
        self.saw_under: dict[str, bool] = {"long": True, "short": True}
        self.wave_open: dict[str, bool] = {"long": False, "short": False}

    def reset(self) -> None:
        self.last_fill_mono = {"long": None, "short": None}
        self.saw_under = {"long": True, "short": True}
        self.wave_open = {"long": False, "short": False}

    def can_arm(self, side: str, now_mono: float) -> bool:
        last = self.last_fill_mono.get(side)
        if last is None:
            return True
        if now_mono - last < self.cooldown_sec:
            return False
        return bool(self.saw_under.get(side))

    def should_execute(self, side: str, *, is_fire: bool, now_mono: float) -> bool:
        """Rising-edge of a fire wave, only when the side is armed."""
        if not is_fire:
            self.saw_under[side] = True
            self.wave_open[side] = False
            return False
        if not self.can_arm(side, now_mono):
            return False
        if self.wave_open.get(side):
            return False
        self.wave_open[side] = True
        return True

    def note_fill(self, side: str, now_mono: float) -> None:
        self.last_fill_mono[side] = float(now_mono)
        self.saw_under[side] = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "cooldown_sec": self.cooldown_sec,
            "last_fill_mono": dict(self.last_fill_mono),
            "saw_under": dict(self.saw_under),
            "wave_open": dict(self.wave_open),
        }
