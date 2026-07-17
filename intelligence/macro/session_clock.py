"""Asia / London / NY session clock — pure UTC math (MC-1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from typing import Any


# Default UTC windows (approximate cash crypto-relevant sessions; DST policy: fixed UTC)
DEFAULT_WINDOWS = {
    "asia": ("00:00", "08:00"),
    "london": ("07:00", "16:00"),
    "ny": ("13:30", "20:00"),
}


def _parse_hhmm(s: str) -> time:
    parts = str(s).strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(h, m, tzinfo=timezone.utc)


def _minutes_of_day(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.hour * 60 + dt.minute


def _window_open(now_min: int, start: time, end: time) -> bool:
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if s <= e:
        return s <= now_min < e
    # overnight wrap
    return now_min >= s or now_min < e


def _minutes_since_open(now_min: int, start: time, end: time) -> int | None:
    if not _window_open(now_min, start, end):
        return None
    s = start.hour * 60 + start.minute
    if start.hour * 60 + start.minute <= end.hour * 60 + end.minute:
        return now_min - s
    if now_min >= s:
        return now_min - s
    return (24 * 60 - s) + now_min


@dataclass
class SessionStatus:
    as_of: str
    asia_open: bool
    london_open: bool
    ny_open: bool
    active: list[str] = field(default_factory=list)
    overlap_london_ny: bool = False
    minutes_since_asia_open: int | None = None
    minutes_since_london_open: int | None = None
    minutes_since_ny_open: int | None = None
    low_volume: bool = False
    fakeout_risk: float = 0.0
    volume_proxy: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def session_status(
    now: datetime | None = None,
    *,
    windows: dict[str, tuple[str, str] | list[str]] | None = None,
    volume_proxy: float | None = None,
    volume_baseline: float | None = None,
    low_volume_pctile: float = 30.0,
    fakeout_size_hint: float = 0.5,
) -> SessionStatus:
    """Compute live session flags. Pure — no I/O."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    win = dict(DEFAULT_WINDOWS)
    if windows:
        for k, v in windows.items():
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                win[k] = (str(v[0]), str(v[1]))

    asia_s, asia_e = _parse_hhmm(win["asia"][0]), _parse_hhmm(win["asia"][1])
    lon_s, lon_e = _parse_hhmm(win["london"][0]), _parse_hhmm(win["london"][1])
    ny_s, ny_e = _parse_hhmm(win["ny"][0]), _parse_hhmm(win["ny"][1])
    now_min = _minutes_of_day(now)

    asia = _window_open(now_min, asia_s, asia_e)
    london = _window_open(now_min, lon_s, lon_e)
    ny = _window_open(now_min, ny_s, ny_e)
    active = [n for n, o in (("asia", asia), ("london", london), ("ny", ny)) if o]

    low_vol = False
    if volume_proxy is not None and volume_baseline is not None and volume_baseline > 0:
        # treat as low if proxy below baseline * (pctile/100) style threshold
        thr = volume_baseline * (float(low_volume_pctile) / 100.0)
        # if baseline is median volume, low when proxy < thr is wrong —
        # interpret: low when proxy < baseline * (low_volume_pctile/50) simplified:
        # user config: low_volume_pctile 30 means below 30% of baseline
        low_vol = float(volume_proxy) < float(volume_baseline) * (float(low_volume_pctile) / 100.0)

    fakeout = 0.0
    if asia and low_vol:
        fakeout = max(0.0, min(1.0, float(fakeout_size_hint) + 0.2))
    elif asia:
        fakeout = 0.25
    elif low_vol:
        fakeout = 0.15

    return SessionStatus(
        as_of=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        asia_open=asia,
        london_open=london,
        ny_open=ny,
        active=active,
        overlap_london_ny=london and ny,
        minutes_since_asia_open=_minutes_since_open(now_min, asia_s, asia_e),
        minutes_since_london_open=_minutes_since_open(now_min, lon_s, lon_e),
        minutes_since_ny_open=_minutes_since_open(now_min, ny_s, ny_e),
        low_volume=low_vol,
        fakeout_risk=round(fakeout, 3),
        volume_proxy=volume_proxy,
    )
