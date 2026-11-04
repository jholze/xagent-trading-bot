"""Telegram /plan and /performance — portfolio vs 0.5%/day plan (365d)."""

from __future__ import annotations

from pathlib import Path

from notifications.plan_chart import render_plan_chart_for_current
from services.portfolio_nav_history import (
    capture_current_nav_snapshot,
    load_nav_history,
)
from services.portfolio_plan import (
    build_report_metrics,
    portfolio_plan_config,
    resolve_plan_start_date,
)
from telegram_notifier import send_telegram_message, send_telegram_photo


def _signed(n: float, decimals: int = 0) -> str:
    if decimals <= 0:
        body = f"{abs(n):,.0f}"
    else:
        body = f"{abs(n):,.{decimals}f}"
    return f"+{body}" if n >= 0 else f"-{body}"


def format_plan_report_html(
    *,
    gap,
    plan_start,
    mode: str = "",
) -> str:
    from services.portfolio_plan import plan_daily_step_usd, plan_total_return_pct

    mode_bit = f" · <i>{mode}</i>" if mode else ""
    compound_l = "Zinseszins" if gap.compound else "linear auf Startkapital"
    step = plan_daily_step_usd(
        gap.start_capital,
        gap.day_index,
        daily_return_pct=gap.daily_return_pct,
        compound=gap.compound,
        horizon_days=gap.horizon_days,
    )
    total_ret = plan_total_return_pct(
        gap.start_capital,
        daily_return_pct=gap.daily_return_pct,
        compound=gap.compound,
        horizon_days=gap.horizon_days,
    )
    formula = (
        f"S×(1+{gap.daily_return_pct/100:g})^t"
        if gap.compound
        else f"S×(1+{gap.daily_return_pct/100:g}×t)"
    )
    step_note = (
        f"+{gap.daily_return_pct:g}% auf den Plan-Stand "
        f"(nächster Tag ≈ ${_signed(step, decimals=0)})"
        if gap.compound
        else f"+{gap.daily_return_pct:g}% vom Startkapital "
        f"(= ${_signed(gap.start_capital * gap.daily_return_pct / 100.0)}/Tag)"
    )
    lines = [
        f"<b>📈 Plan vs. Portfolio ({gap.horizon_days} Tage)</b>{mode_bit}",
        (
            f"Start: <b>${gap.start_capital:,.0f}</b> · "
            f"Tag <b>{gap.day_index}</b> / {gap.horizon_days} · "
            f"Ziel {gap.daily_return_pct:g}%/Tag ({compound_l})"
        ),
        f"Formel: <code>{formula}</code>",
        f"Plan-Start: <code>{plan_start.isoformat()}</code>",
        "",
        f"NAV jetzt:     <b>${gap.nav_actual:,.0f}</b>",
        f"Plan heute:    <b>${gap.nav_plan:,.0f}</b>",
        (
            f"Δ vs Plan:     <b>${_signed(gap.delta_usd)}</b> "
            f"(<code>{gap.delta_pct:+.1f}%</code>)"
        ),
        "",
        (
            f"Plan-Ende t={gap.horizon_days}: <b>${gap.plan_end:,.0f}</b> "
            f"(<code>+{total_ret:.0f}%</code> vs. Start)"
        ),
        f"Restlaufzeit:    <b>{gap.days_remaining}</b> Tage",
        "",
        f"<i>Tagesziel: {step_note}</i>",
    ]
    return "\n".join(lines)


def format_epoch_line(epoch: dict | None, pre_history: list[dict] | None) -> str:
    """One line naming the ledger epoch and where the older numbers are."""
    if not epoch or not epoch.get("started_at"):
        return ""
    try:
        from services.ledger_epoch_view import epoch_start

        start = epoch_start(epoch)
        from core.time_utils import display_tz

        when = start.astimezone(display_tz()).strftime("%Y-%m-%d %H:%M") if start else "?"
    except Exception:
        when = str(epoch.get("started_at"))[:16]
    line = f"<i>Ledger-Epoch <code>{epoch.get('epoch_id') or '?'}</code> seit {when}"
    pre = [p for p in (pre_history or []) if isinstance(p, dict) and p.get("nav") is not None]
    if pre:
        first = str(pre[0].get("date") or "?")[:10]
        last = str(pre[-1].get("date") or "?")[:10]
        try:
            last_nav = f"${float(pre[-1]['nav']):,.0f}"
        except (TypeError, ValueError):
            last_nav = "?"
        line += f" · vorher {first}…{last}, NAV zuletzt {last_nav} (in portfolio_nav_daily)"
    return line + "</i>"


def build_plan_payload() -> tuple[str, str | None, object]:
    """Return (html_report, chart_path_or_none, gap)."""
    from core.config import get_bot_config
    from core.portfolio_baseline import initial_capital
    from notifications.terminal_dashboard import _portfolio_snapshot

    cfg = get_bot_config()
    plan_cfg = portfolio_plan_config(cfg.raw)
    if not plan_cfg.get("enabled", True):
        return "📈 Portfolio-Plan ist deaktiviert (<code>portfolio_plan.enabled</code>).", None, None

    # Refresh today's snapshot for chart endpoint
    capture_current_nav_snapshot()

    snap = _portfolio_snapshot()
    nav = float(snap.get("total_value") or 0)
    init = float(snap.get("initial_capital") or initial_capital() or 0)
    all_history = load_nav_history()

    # Current ledger epoch (reset marker, else newest ledger_epochs doc): plan
    # start, day counter, BTC HODL baseline and KPI windows all start there.
    from services.ledger_epoch_view import (
        epoch_start,
        epoch_start_date,
        load_current_epoch,
        split_points,
    )

    try:
        epoch = load_current_epoch()
    except Exception:
        epoch = {}
    ep_start = epoch_start(epoch)
    history, pre_history = split_points(all_history, ep_start)
    if ep_start is not None:
        try:
            ep_cap = float(epoch.get("initial_capital") or 0)
        except (TypeError, ValueError):
            ep_cap = 0.0
        if ep_cap > 0:
            init = ep_cap
    plan_start = resolve_plan_start_date(
        config_raw=cfg.raw,
        history_points=history,
        epoch_start=epoch_start_date(epoch),
    )
    try:
        from core.time_utils import now_display

        today = now_display().date()
    except Exception:
        from datetime import date

        today = date.today()

    gap = build_report_metrics(
        start_capital=init,
        nav_actual=nav,
        plan_start=plan_start,
        today=today,
        config_raw=cfg.raw,
    )
    mode = ""
    try:
        from services.trading_service import TradingService

        mode = TradingService().mode_label()
    except Exception:
        mode = str(cfg.trading_mode or "")

    html = format_plan_report_html(gap=gap, plan_start=plan_start, mode=mode)
    epoch_line = format_epoch_line(epoch, pre_history)
    if epoch_line:
        html = f"{html}\n{epoch_line}"
    try:
        from services.reporting.metrics import format_live_metrics_block

        block = format_live_metrics_block(
            days=7, since=ep_start, start_equity=init if ep_start is not None else None
        )
        if block:
            html = f"{html}\n\n{block}"
        elif ep_start is not None:
            html = f"{html}\n\n<i>Kennzahlen: noch keine geschlossenen Trades seit Epoch-Start.</i>"
    except Exception:
        pass
    try:
        from services.reporting.benchmark import format_btc_benchmark_line

        bench = format_btc_benchmark_line(
            history, start_nav=init if ep_start is not None else None
        )
        if bench:
            html = f"{html}\n{bench}"
    except Exception:
        pass
    chart_path = render_plan_chart_for_current(
        start_capital=init,
        plan_start=plan_start,
        history_points=history,
        nav_today=nav,
        today=today,
        config_raw=cfg.raw,
    )
    return html, chart_path, gap


def send_plan_report() -> None:
    html, chart_path, _gap = build_plan_payload()
    if chart_path:
        try:
            # Caption limited to 1024 — send full text first if long
            if len(html) > 900:
                send_telegram_message(html)
                send_telegram_photo("📈 Plan vs Portfolio", chart_path)
            else:
                send_telegram_photo(html, chart_path)
        finally:
            try:
                Path(chart_path).unlink(missing_ok=True)
            except Exception:
                pass
    else:
        send_telegram_message(html)


def handle(text: str) -> bool:
    raw = (text or "").strip().lower()
    if raw in ("/plan", "/performance", "/plan_vs", "/zielplan"):
        send_plan_report()
        return True
    if raw.startswith("/plan ") or raw.startswith("/performance "):
        send_plan_report()
        return True
    return False
