"""Rolling 5-minute vs 60-minute exclusive-baseline liquidation cascade detector."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from logger import log

from services.exit_realtime.liq_stream import ParsedLiq, SIDE_LONG, SIDE_SHORT

SIDES = (SIDE_LONG, SIDE_SHORT)


@dataclass(frozen=True)
class SideSnapshot:
    side: str
    window_usd: Decimal
    baseline_mean_usd: Decimal
    ratio: float
    fire: bool
    soft: bool
    cold_start: bool


def _as_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


class CascadeDetector:
    """Pure window math. Timer / execute live in cascade_state + execute_cascade_exit."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.window_ms = int(float(cfg.get("window_sec") or 300) * 1000)
        self.baseline_ms = int(float(cfg.get("baseline_sec") or 3600) * 1000)
        self.min_samples_ms = int(float(cfg.get("min_baseline_samples_sec") or 1800) * 1000)
        self.multiplier = float(cfg.get("multiplier") or 3.0)
        self.min_notional = _as_decimal(cfg.get("min_notional_usd") or 100000)
        self.log_soft = bool(cfg.get("log_soft_signal", True))
        self._events: deque[ParsedLiq] = deque()
        self._first_ts: int | None = None

    def ingest(self, events: Iterable[ParsedLiq]) -> None:
        for ev in events:
            if ev is None:
                continue
            self._events.append(ev)
            if self._first_ts is None or ev.ts_ms < self._first_ts:
                self._first_ts = ev.ts_ms
        self._prune(self._newest_ts())

    def _newest_ts(self) -> int:
        if not self._events:
            return 0
        return max(ev.ts_ms for ev in self._events)

    def _prune(self, now_ms: int) -> None:
        if now_ms <= 0:
            return
        cutoff = now_ms - self.window_ms - self.baseline_ms
        while self._events and self._events[0].ts_ms < cutoff:
            self._events.popleft()

    def _sum_side(
        self,
        side: str,
        *,
        start_ms: int,
        end_ms: int,
        now_ms: int,
        start_inclusive: bool,
    ) -> Decimal:
        total = Decimal("0")
        for ev in self._events:
            if ev.side != side:
                continue
            if ev.ts_ms > now_ms:
                continue
            if start_inclusive:
                hit = start_ms <= ev.ts_ms <= end_ms
            else:
                hit = start_ms < ev.ts_ms <= end_ms
            if hit:
                total += ev.usd
        return total

    def evaluate(self, now_ms: int) -> dict[str, SideSnapshot]:
        self._prune(now_ms)
        window_start = now_ms - self.window_ms
        baseline_start = window_start - self.baseline_ms
        n_slots = max(1.0, self.baseline_ms / self.window_ms)

        first = self._first_ts
        available_ms = 0 if first is None else max(0, window_start - first)
        cold_start = first is None or available_ms < self.min_samples_ms

        out: dict[str, SideSnapshot] = {}
        for side in SIDES:
            window = self._sum_side(
                side,
                start_ms=window_start,
                end_ms=now_ms,
                now_ms=now_ms,
                start_inclusive=False,
            )
            exclusive = self._sum_side(
                side,
                start_ms=baseline_start,
                end_ms=window_start,
                now_ms=now_ms,
                start_inclusive=True,
            )
            mean = exclusive / Decimal(str(n_slots))
            if mean > 0:
                ratio = float(window / mean)
            elif window > 0:
                ratio = float("inf")
            else:
                ratio = 0.0
            over_rel = window >= (Decimal(str(self.multiplier)) * mean)
            over_floor = window >= self.min_notional
            fire = (not cold_start) and over_rel and over_floor
            half_floor = self.min_notional * Decimal("0.5")
            soft = (not fire) and (ratio >= 2.0 or window >= half_floor)
            snap = SideSnapshot(
                side=side,
                window_usd=window,
                baseline_mean_usd=mean,
                ratio=ratio,
                fire=fire,
                soft=soft,
                cold_start=cold_start,
            )
            out[side] = snap
            if self.log_soft and soft:
                log(
                    f"liq_cascade soft side={side} window_usd={window} "
                    f"baseline_mean={mean} ratio={ratio:.3g} cold_start={cold_start}",
                    "INFO",
                )
        return out
