"""Economic calendar normalize + pre/post event windows (MC-2/3)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


EVENT_CODES = frozenset({"FOMC", "NFP", "CPI", "PPI", "CLAIMS"})


@dataclass
class MacroEvent:
    event_code: str
    scheduled_at: str  # ISO UTC
    title: str = ""
    country: str = "US"
    importance: str = "high"  # high | medium | low
    actual: float | None = None
    consensus: float | None = None
    source: str = "fixture"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_iso(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            s = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s[:32] if "T" in s else s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_event_code(raw: str) -> str:
    u = str(raw or "").upper().strip()
    aliases = {
        "NONFARM": "NFP",
        "NONFARM_PAYROLLS": "NFP",
        "PAYROLLS": "NFP",
        "FED": "FOMC",
        "FOMC_RATE": "FOMC",
        "INTEREST_RATE": "FOMC",
        "INFLATION": "CPI",
        "CORE_CPI": "CPI",
    }
    if u in EVENT_CODES:
        return u
    return aliases.get(u, u if u in EVENT_CODES else u)


def active_windows(
    event: MacroEvent,
    now: datetime | None = None,
    *,
    pre_windows_min: list[int] | None = None,
    post_windows_min: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Return active pre/post window buckets for an event at `now`."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sched = parse_iso(event.scheduled_at)
    if sched is None:
        return []
    pre = list(pre_windows_min if pre_windows_min is not None else [1440, 240, 60, 15])
    post = list(post_windows_min if post_windows_min is not None else [5, 60])
    delta_min = (sched - now).total_seconds() / 60.0
    out: list[dict[str, Any]] = []

    # pre: event is in the future; window T-X active when 0 < delta <= X
    # (closest larger window first for labeling)
    if delta_min > 0:
        for w in sorted(pre):
            if delta_min <= w:
                out.append(
                    {
                        "kind": "pre",
                        "window_min": w,
                        "minutes_to_event": round(delta_min, 2),
                        "event_code": event.event_code,
                        "scheduled_at": event.scheduled_at,
                        "bucket": f"pre_{w}",
                    }
                )
                break  # only tightest matching pre-window
    # post: event in the past; 0 >= delta > -post_w
    elif delta_min <= 0:
        age = -delta_min
        for w in sorted(post):
            if age <= w:
                out.append(
                    {
                        "kind": "post",
                        "window_min": w,
                        "minutes_since_event": round(age, 2),
                        "event_code": event.event_code,
                        "scheduled_at": event.scheduled_at,
                        "bucket": f"post_{w}",
                    }
                )
                break
        # print just happened (within 5 min)
        if age <= 5:
            out.append(
                {
                    "kind": "print",
                    "window_min": 5,
                    "minutes_since_event": round(age, 2),
                    "event_code": event.event_code,
                    "scheduled_at": event.scheduled_at,
                    "bucket": "print",
                    "actual": event.actual,
                    "consensus": event.consensus,
                }
            )
    return out


def surprise_score(actual: float | None, consensus: float | None) -> float:
    if actual is None or consensus is None:
        return 0.0
    try:
        a, c = float(actual), float(consensus)
    except (TypeError, ValueError):
        return 0.0
    if c == 0:
        return 0.0
    # relative surprise clamped
    rel = (a - c) / max(abs(c), 1e-6)
    return max(-1.0, min(1.0, rel))


def load_fixture_events(path: str | None = None) -> list[MacroEvent]:
    """Load calendar fixtures for tests / offline."""
    import json
    from pathlib import Path

    if path:
        p = Path(path)
    else:
        # packaged data (not under tests/ — Docker includes this)
        p = Path(__file__).resolve().parent / "data" / "calendar_events.json"
        if not p.is_file():
            p = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "macro" / "calendar_events.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for row in data.get("events") or []:
        code = normalize_event_code(str(row.get("event_code") or row.get("code") or ""))
        if code not in EVENT_CODES and code not in ("FOMC", "NFP", "CPI"):
            # still allow if in allowlist-like
            if not code:
                continue
        out.append(
            MacroEvent(
                event_code=code if code in EVENT_CODES else code,
                scheduled_at=str(row.get("scheduled_at") or ""),
                title=str(row.get("title") or code),
                country=str(row.get("country") or "US"),
                importance=str(row.get("importance") or "high"),
                actual=row.get("actual"),
                consensus=row.get("consensus"),
                source=str(row.get("source") or "fixture"),
                metadata=dict(row.get("metadata") or {}),
            )
        )
    return out


def fetch_fred_calendar_stub() -> list[MacroEvent]:
    """Optional live FRED — fail-open empty without key / network.

    Full FRED release calendar is limited; v1 uses fixtures + optional AV.
    """
    import os

    if not (os.environ.get("FRED_API_KEY") or "").strip():
        return []
    # Live FRED series pull is heavy; return empty and rely on fixtures/AV in cycle
    # unless caller injects events. Keeps performance predictable.
    return []


def fetch_alpha_vantage_calendar_stub() -> list[MacroEvent]:
    import os

    if not (os.environ.get("ALPHA_VANTAGE_API_KEY") or "").strip():
        return []
    return []
