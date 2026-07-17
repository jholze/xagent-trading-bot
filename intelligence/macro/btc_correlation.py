"""Impact score + BTC historical reaction around macro events (MC-4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from intelligence.macro.calendar import MacroEvent, parse_iso, surprise_score


@dataclass
class BtcCorrSummary:
    event_code: str
    sample_n: int
    avg_abs_ret: float
    median_ret: float
    down_hit_rate: float
    window_hours: float
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ret(bars: list[dict], idx: int, ahead: int) -> float | None:
    if idx < 0 or idx + ahead >= len(bars):
        return None
    try:
        c0 = float(bars[idx].get("close") or bars[idx].get("c") or 0)
        c1 = float(bars[idx + ahead].get("close") or bars[idx + ahead].get("c") or 0)
    except (TypeError, ValueError):
        return None
    if c0 <= 0:
        return None
    return (c1 - c0) / c0


def compute_btc_correlation(
    event_code: str,
    release_times: list[str | datetime],
    bars: list[dict],
    *,
    window_hours: float = 4.0,
    bar_hours: float = 1.0,
    min_samples: int = 3,
) -> BtcCorrSummary:
    """Join release times to OHLCV bars; stats on forward returns.

    `bars` sorted ascending by timestamp; each row needs close + timestamp.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not bars or not release_times:
        return BtcCorrSummary(event_code, 0, 0.0, 0.0, 0.0, window_hours, now)

    # index bars by time
    parsed: list[tuple[datetime, dict]] = []
    for b in bars:
        ts = parse_iso(b.get("timestamp") or b.get("ts") or b.get("time"))
        if ts:
            parsed.append((ts, b))
    parsed.sort(key=lambda x: x[0])
    if not parsed:
        return BtcCorrSummary(event_code, 0, 0.0, 0.0, 0.0, window_hours, now)

    ahead = max(1, int(round(window_hours / max(bar_hours, 0.25))))
    rets: list[float] = []
    for rel in release_times:
        rdt = parse_iso(rel)
        if rdt is None:
            continue
        # nearest bar at or before release
        idx = None
        for i, (ts, _) in enumerate(parsed):
            if ts <= rdt:
                idx = i
            else:
                break
        if idx is None:
            continue
        r = _ret([p[1] for p in parsed], idx, ahead)
        if r is not None:
            rets.append(r)

    n = len(rets)
    if n < min_samples and n == 0:
        return BtcCorrSummary(event_code, 0, 0.0, 0.0, 0.0, window_hours, now)

    abs_avg = sum(abs(x) for x in rets) / n if n else 0.0
    sorted_r = sorted(rets)
    med = sorted_r[n // 2] if n else 0.0
    down = sum(1 for x in rets if x < 0) / n if n else 0.0
    return BtcCorrSummary(
        event_code=event_code,
        sample_n=n,
        avg_abs_ret=round(abs_avg, 6),
        median_ret=round(med, 6),
        down_hit_rate=round(down, 4),
        window_hours=window_hours,
        as_of=now,
    )


def impact_score(
    event: MacroEvent,
    corr: BtcCorrSummary | None = None,
    *,
    min_samples: int = 8,
) -> float:
    """Heuristic impact in [-1, 1] for MarketEvent."""
    base = {
        "FOMC": 0.55,
        "NFP": 0.5,
        "CPI": 0.6,
        "PPI": 0.35,
        "CLAIMS": 0.25,
    }.get(event.event_code, 0.3)
    if event.importance == "medium":
        base *= 0.7
    elif event.importance == "low":
        base *= 0.4

    # surprise tilts sign slightly (risk-off if inflation hot / strong NFP simplified)
    sur = surprise_score(event.actual, event.consensus)
    signed = base
    if event.event_code == "CPI" and sur > 0.05:
        signed = base  # hotter CPI → risk-off magnitude
    elif event.event_code == "CPI" and sur < -0.05:
        signed = base * 0.85

    if corr and corr.sample_n >= min_samples:
        # scale by historical abs move (e.g. 2% avg → boost)
        scale = min(1.5, 1.0 + corr.avg_abs_ret * 20.0)
        signed = min(1.0, signed * scale)
        if corr.down_hit_rate >= 0.55:
            return -abs(signed)
    # default high-impact macro as risk-off-ish negative for size
    return -abs(signed)
