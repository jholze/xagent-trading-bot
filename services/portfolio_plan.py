"""Portfolio plan curve vs actual NAV (0.5%/day on start capital, 365d horizon)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence


DEFAULT_DAILY_RETURN_PCT = 0.5
DEFAULT_HORIZON_DAYS = 365
DEFAULT_COMPOUND = True  # 0.5%/Tag Zinseszins: S*(1+r)^t


def portfolio_plan_config(config_raw: dict | None = None) -> dict[str, Any]:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    raw = dict((config_raw or {}).get("portfolio_plan") or {})
    if "compound" in raw:
        compound = bool(raw.get("compound"))
    else:
        compound = DEFAULT_COMPOUND
    return {
        "enabled": bool(raw.get("enabled", True)),
        "daily_return_pct": float(raw.get("daily_return_pct", DEFAULT_DAILY_RETURN_PCT) or DEFAULT_DAILY_RETURN_PCT),
        "compound": compound,
        "horizon_days": int(raw.get("horizon_days", DEFAULT_HORIZON_DAYS) or DEFAULT_HORIZON_DAYS),
        "plan_start_date": (raw.get("plan_start_date") or None),
        "chart_default_days": int(raw.get("chart_default_days", DEFAULT_HORIZON_DAYS) or DEFAULT_HORIZON_DAYS),
    }


def daily_rate(cfg: dict | None = None) -> float:
    c = cfg or portfolio_plan_config()
    return float(c["daily_return_pct"]) / 100.0


def plan_nav_at_day(
    start_capital: float,
    day_index: int,
    *,
    daily_return_pct: float = DEFAULT_DAILY_RETURN_PCT,
    compound: bool = DEFAULT_COMPOUND,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> float:
    """Plan NAV at day t (0 = start).

    - compound (default): S * (1+r)^t  — Zinseszins 0.5%/Tag
    - linear: S * (1 + r*t)            — 0.5% vom Startkapital pro Tag

    After horizon, clamp t to horizon (flat at end value).
    """
    s = float(start_capital)
    if s <= 0:
        return 0.0
    r = float(daily_return_pct) / 100.0
    h = max(0, int(horizon_days))
    t = max(0, int(day_index))
    if h > 0:
        t = min(t, h)
    if compound:
        return s * ((1.0 + r) ** t)
    return s * (1.0 + r * t)


def plan_daily_step_usd(
    start_capital: float,
    day_index: int,
    *,
    daily_return_pct: float = DEFAULT_DAILY_RETURN_PCT,
    compound: bool = DEFAULT_COMPOUND,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> float:
    """USD increase from day t → t+1 on the plan curve (0 after horizon)."""
    h = max(0, int(horizon_days))
    t = max(0, int(day_index))
    if h > 0 and t >= h:
        return 0.0
    now = plan_nav_at_day(
        start_capital,
        t,
        daily_return_pct=daily_return_pct,
        compound=compound,
        horizon_days=h,
    )
    nxt = plan_nav_at_day(
        start_capital,
        t + 1,
        daily_return_pct=daily_return_pct,
        compound=compound,
        horizon_days=h,
    )
    return float(nxt - now)


def plan_total_return_pct(
    start_capital: float,
    *,
    daily_return_pct: float = DEFAULT_DAILY_RETURN_PCT,
    compound: bool = DEFAULT_COMPOUND,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> float:
    """Total % gain over full horizon vs start capital."""
    s = float(start_capital)
    if s <= 0:
        return 0.0
    end = plan_nav_at_day(
        start_capital,
        int(horizon_days),
        daily_return_pct=daily_return_pct,
        compound=compound,
        horizon_days=horizon_days,
    )
    return (end / s - 1.0) * 100.0


def plan_series(
    start_capital: float,
    *,
    daily_return_pct: float = DEFAULT_DAILY_RETURN_PCT,
    compound: bool = DEFAULT_COMPOUND,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> list[float]:
    """Plan values for days 0..horizon inclusive (horizon+1 points)."""
    h = max(0, int(horizon_days))
    return [
        plan_nav_at_day(
            start_capital,
            t,
            daily_return_pct=daily_return_pct,
            compound=compound,
            horizon_days=h,
        )
        for t in range(h + 1)
    ]


def parse_plan_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()[:10]
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def day_index_for(plan_start: date, on_day: date, *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> int:
    delta = (on_day - plan_start).days
    t = max(0, int(delta))
    h = max(0, int(horizon_days))
    if h > 0:
        t = min(t, h)
    return t


@dataclass(frozen=True)
class PlanGap:
    start_capital: float
    day_index: int
    horizon_days: int
    nav_actual: float
    nav_plan: float
    plan_end: float
    delta_usd: float
    delta_pct: float
    days_remaining: int
    daily_return_pct: float
    compound: bool


def compute_gap(
    start_capital: float,
    nav_actual: float,
    day_index: int,
    *,
    daily_return_pct: float = DEFAULT_DAILY_RETURN_PCT,
    compound: bool = DEFAULT_COMPOUND,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> PlanGap:
    h = max(0, int(horizon_days))
    t = max(0, min(int(day_index), h if h else int(day_index)))
    plan = plan_nav_at_day(
        start_capital,
        t,
        daily_return_pct=daily_return_pct,
        compound=compound,
        horizon_days=h,
    )
    end = plan_nav_at_day(
        start_capital,
        h,
        daily_return_pct=daily_return_pct,
        compound=compound,
        horizon_days=h,
    )
    delta = float(nav_actual) - plan
    pct = (delta / plan * 100.0) if plan else 0.0
    return PlanGap(
        start_capital=float(start_capital),
        day_index=t,
        horizon_days=h,
        nav_actual=float(nav_actual),
        nav_plan=plan,
        plan_end=end,
        delta_usd=delta,
        delta_pct=pct,
        days_remaining=max(0, h - t),
        daily_return_pct=float(daily_return_pct),
        compound=bool(compound),
    )


def resolve_plan_start_date(
    *,
    config_raw: dict | None = None,
    history_points: Sequence[dict] | None = None,
    fallback: date | None = None,
) -> date:
    cfg = portfolio_plan_config(config_raw)
    configured = parse_plan_date(cfg.get("plan_start_date"))
    if configured:
        return configured
    if history_points:
        dates = []
        for p in history_points:
            d = parse_plan_date(p.get("date"))
            if d:
                dates.append(d)
        if dates:
            return min(dates)
    if fallback:
        return fallback
    try:
        from core.time_utils import now_display

        return now_display().date()
    except Exception:
        return date.today()


def build_report_metrics(
    *,
    start_capital: float,
    nav_actual: float,
    plan_start: date,
    today: date | None = None,
    config_raw: dict | None = None,
) -> PlanGap:
    cfg = portfolio_plan_config(config_raw)
    today = today or date.today()
    t = day_index_for(plan_start, today, horizon_days=int(cfg["horizon_days"]))
    return compute_gap(
        start_capital,
        nav_actual,
        t,
        daily_return_pct=float(cfg["daily_return_pct"]),
        compound=bool(cfg["compound"]),
        horizon_days=int(cfg["horizon_days"]),
    )
