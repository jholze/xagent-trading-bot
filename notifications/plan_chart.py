"""Render portfolio plan vs actual NAV chart (PNG for Telegram)."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Sequence

from logger import log
from services.portfolio_plan import (
    DEFAULT_HORIZON_DAYS,
    plan_series,
    portfolio_plan_config,
)


def render_plan_vs_actual_png(
    *,
    start_capital: float,
    plan_start: date,
    actual_by_day: dict[int, float],
    today_day_index: int,
    daily_return_pct: float = 0.5,
    compound: bool = False,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    title: str | None = None,
    btc_by_day: dict[int, float] | None = None,
) -> str | None:
    """Return path to temp PNG or None."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log("matplotlib not installed — plan chart skipped", "WARNING")
        return None

    h = max(1, int(horizon_days))
    plan = plan_series(
        start_capital,
        daily_return_pct=daily_return_pct,
        compound=compound,
        horizon_days=h,
    )
    xs_plan = list(range(len(plan)))
    # actual series: days with data up to today
    t_today = max(0, min(int(today_day_index), h))
    act_x = sorted(k for k in actual_by_day if 0 <= k <= t_today)
    act_y = [actual_by_day[k] for k in act_x]
    # ensure today point if present only in live
    if t_today not in actual_by_day and act_y:
        pass

    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=110)
    try:
        mode_lbl = "Zinseszins" if compound else "linear"
        ax.plot(
            xs_plan,
            plan,
            color="#3498db",
            linestyle="--",
            linewidth=1.6,
            label=f"Plan {daily_return_pct:g}%/d ({mode_lbl})",
        )
        if act_x:
            ax.plot(
                act_x,
                act_y,
                color="#2ecc71",
                linewidth=2.0,
                marker="o",
                markersize=3,
                label="Portfolio NAV",
            )
        if btc_by_day:
            btc_x = sorted(k for k in btc_by_day if 0 <= k <= t_today)
            btc_y = [btc_by_day[k] for k in btc_x]
            if len(btc_x) >= 2:
                ax.plot(
                    btc_x,
                    btc_y,
                    color="#f39c12",
                    linewidth=1.6,
                    linestyle="-.",
                    marker="s",
                    markersize=3,
                    label="BTC HODL",
                )
        ax.axhline(start_capital, color="#95a5a6", linestyle=":", linewidth=1, alpha=0.8, label="Start")
        ax.axvline(t_today, color="#e67e22", linestyle=":", linewidth=1, alpha=0.7)
        # end marker
        ax.scatter([h], [plan[-1]], color="#3498db", s=28, zorder=5)

        ax.set_xlim(0, h)
        ax.set_xlabel("Tag (Plan-Horizont)", fontsize=9)
        ax.set_ylabel("USDT", fontsize=9)
        ax.set_title(
            title
            or f"Plan vs Portfolio · Start {plan_start.isoformat()} · {h} Tage",
            fontsize=11,
        )
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left", fontsize=8)
        ax.tick_params(labelsize=8)
        fig.tight_layout()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        path = tmp.name
        tmp.close()
        fig.savefig(path, format="png", facecolor="white")
        return path
    except Exception as e:
        log(f"plan chart render failed: {e}", "WARNING")
        return None
    finally:
        plt.close(fig)


def render_plan_chart_for_current(
    *,
    start_capital: float,
    plan_start: date,
    history_points: Sequence[dict],
    nav_today: float,
    today: date | None = None,
    config_raw: dict | None = None,
) -> str | None:
    from services.portfolio_nav_history import history_as_day_nav_map
    from services.portfolio_plan import day_index_for, portfolio_plan_config

    cfg = portfolio_plan_config(config_raw)
    today = today or date.today()
    h = int(cfg["horizon_days"])
    by_day = history_as_day_nav_map(list(history_points), plan_start)
    t = day_index_for(plan_start, today, horizon_days=h)
    by_day[t] = float(nav_today)
    btc_by_day: dict[int, float] | None = None
    try:
        from services.reporting.benchmark import hodl_nav_by_plan_day

        mapped = hodl_nav_by_plan_day(list(history_points), plan_start)
        if len(mapped) >= 2:
            btc_by_day = mapped
    except Exception:
        btc_by_day = None
    return render_plan_vs_actual_png(
        start_capital=start_capital,
        plan_start=plan_start,
        actual_by_day=by_day,
        today_day_index=t,
        daily_return_pct=float(cfg["daily_return_pct"]),
        compound=bool(cfg["compound"]),
        horizon_days=h,
        btc_by_day=btc_by_day,
    )
