"""BTC buy-and-hold benchmark vs bot NAV (#307).

Rows without a usable ``btc_close`` are skipped. Start capital is the NAV of
the first remaining row. Alpha is bot return % minus BTC return % (percentage
points).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable


def _as_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0 or n != n:
        return None
    return n


def _row_date(point: dict) -> date | None:
    try:
        return date.fromisoformat(str(point.get("date") or "")[:10])
    except Exception:
        return None


def btc_rows(points: Iterable[dict] | None) -> list[tuple[date, float, float]]:
    """``(date, nav, btc_close)`` for rows with both a positive NAV and btc_close."""
    rows: list[tuple[date, float, float]] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        d = _row_date(point)
        nav = _as_positive_float(point.get("nav"))
        btc = _as_positive_float(point.get("btc_close"))
        if d is None or nav is None or btc is None:
            continue
        rows.append((d, nav, btc))
    rows.sort(key=lambda r: r[0])
    return rows


def benchmark_from_nav_rows(points: Iterable[dict] | None) -> dict[str, Any] | None:
    """BTC HODL series and alpha from NAV history.

    Returns ``None`` when no row has ``btc_close``. With one row, returns and
    alpha are 0. Callers that need a comparison require ``n >= 2``.
    """
    rows = btc_rows(points)
    if not rows:
        return None
    start_d, start_nav, start_btc = rows[0]
    series: list[dict[str, Any]] = []
    for d, nav, btc in rows:
        btc_hodl_nav = start_nav * (btc / start_btc)
        bot_pct = (nav / start_nav - 1.0) * 100.0
        btc_pct = (btc / start_btc - 1.0) * 100.0
        series.append(
            {
                "date": d.isoformat(),
                "nav": nav,
                "btc_close": btc,
                "btc_hodl_nav": btc_hodl_nav,
                "bot_return_pct": bot_pct,
                "btc_return_pct": btc_pct,
                "alpha_pp": bot_pct - btc_pct,
            }
        )
    last = series[-1]
    return {
        "start_date": start_d.isoformat(),
        "start_nav": start_nav,
        "start_btc": start_btc,
        "n": len(series),
        "points": series,
        "bot_return_pct": last["bot_return_pct"],
        "btc_return_pct": last["btc_return_pct"],
        "alpha_pp": last["alpha_pp"],
        "btc_hodl_nav": last["btc_hodl_nav"],
    }


def hodl_nav_by_plan_day(
    points: Iterable[dict] | None,
    plan_start: date,
) -> dict[int, float]:
    """Map plan day-index → BTC HODL NAV for rows on/after *plan_start*."""
    bench = benchmark_from_nav_rows(points)
    if not bench:
        return {}
    out: dict[int, float] = {}
    for p in bench["points"]:
        try:
            d = date.fromisoformat(str(p["date"])[:10])
        except Exception:
            continue
        if d < plan_start:
            continue
        out[(d - plan_start).days] = float(p["btc_hodl_nav"])
    return out


def format_btc_benchmark_line(points: Iterable[dict] | None) -> str:
    """Additive /plan line. Empty unless at least two rows have ``btc_close``."""
    bench = benchmark_from_nav_rows(points)
    if not bench or int(bench.get("n") or 0) < 2:
        return ""
    btc = f"{float(bench['btc_return_pct']):+.1f}%"
    bot = f"{float(bench['bot_return_pct']):+.1f}%"
    alpha = f"{float(bench['alpha_pp']):+.1f}"
    start = str(bench["start_date"])
    try:
        from notifications.telegram_i18n import t

        line = t("plan_btc_hodl", date=start, btc=btc, bot=bot, alpha=alpha)
        if line and line != "plan_btc_hodl":
            return line
    except Exception:
        pass
    return f"BTC HODL seit {start}: {btc} · Bot: {bot} · Alpha: {alpha} pp"
