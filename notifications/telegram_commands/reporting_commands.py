"""Read-only Telegram reporting: /fills and /attribution (#307)."""

from __future__ import annotations

import html

from notifications.telegram_commands.utils import safe_int
from notifications.telegram_i18n import signed_money, t
from telegram_notifier import send_telegram_message

_DEFAULT_DAYS = 7


def _parse_days(text: str, *names: str) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split()
    cmd = parts[0].lower()
    if cmd not in names:
        return None
    if len(parts) == 1:
        return _DEFAULT_DAYS
    n = safe_int(parts[1])
    if n is None:
        return _DEFAULT_DAYS
    return max(1, min(365, n))


def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _fmt_bps(value: object) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def format_fills_html(summary: dict) -> str:
    days = int(summary.get("days") or _DEFAULT_DAYS)
    lines = [t("fills_title", days=days), ""]
    by_side = summary.get("by_side") or {}
    if by_side:
        for side in ("buy", "sell", "short", "cover"):
            row = by_side.get(side)
            if not row:
                continue
            lines.append(
                t(
                    "fills_side_row",
                    side=_esc(side.upper()),
                    n=int(row.get("n") or 0),
                    median=_fmt_bps(row.get("median_bps")),
                    p90=_fmt_bps(row.get("p90_bps")),
                )
            )
        extra = [k for k in by_side if k not in ("buy", "sell", "short", "cover")]
        for side in sorted(extra):
            row = by_side[side]
            lines.append(
                t(
                    "fills_side_row",
                    side=_esc(str(side).upper()),
                    n=int(row.get("n") or 0),
                    median=_fmt_bps(row.get("median_bps")),
                    p90=_fmt_bps(row.get("p90_bps")),
                )
            )
    by_venue = summary.get("by_venue") or {}
    if by_venue:
        lines.append("")
        lines.append(t("fills_venue_header"))
        for name, row in sorted(by_venue.items(), key=lambda kv: str(kv[0])):
            lines.append(
                t(
                    "fills_side_row",
                    side=_esc(name),
                    n=int(row.get("n") or 0),
                    median=_fmt_bps(row.get("median_bps")),
                    p90=_fmt_bps(row.get("p90_bps")),
                )
            )
    lines.append("")
    drag = summary.get("fee_drag_pct")
    if drag is None:
        lines.append(t("fills_fee_drag_na"))
    else:
        lines.append(
            t(
                "fills_fee_drag",
                pct=f"{float(drag):.1f}%",
                fees=f"${float(summary.get('total_fees') or 0):,.2f}",
                pnl=signed_money(float(summary.get("gross_realized_pnl") or 0), decimals=2),
            )
        )
    return "\n".join(lines).rstrip()


def format_attribution_html(summary: dict) -> str:
    days = int(summary.get("days") or _DEFAULT_DAYS)
    lines = [t("attribution_title", days=days), ""]
    lines.append(t("attribution_source_header"))
    for row in summary.get("by_source") or []:
        lines.append(
            t(
                "attribution_row",
                name=_esc(row.get("name") or "?"),
                pnl=signed_money(float(row.get("pnl") or 0), decimals=2),
                n=int(row.get("n") or 0),
                wr=f"{float(row.get('win_rate') or 0):.0f}%",
            )
        )
    lines.append("")
    lines.append(t("attribution_exit_header"))
    for row in summary.get("by_exit_source") or []:
        lines.append(
            t(
                "attribution_row",
                name=_esc(row.get("name") or "?"),
                pnl=signed_money(float(row.get("pnl") or 0), decimals=2),
                n=int(row.get("n") or 0),
                wr=f"{float(row.get('win_rate') or 0):.0f}%",
            )
        )
    return "\n".join(lines).rstrip()


def _handle_fills(days: int) -> bool:
    try:
        from services.reporting.fills import fill_quality_summary, list_filled_orders

        orders = list_filled_orders(days)
        summary = fill_quality_summary(orders, days)
        if not summary.get("n_fills"):
            send_telegram_message(t("fills_no_data"))
            return True
        send_telegram_message(format_fills_html(summary))
    except Exception:
        send_telegram_message(t("fills_no_data"))
    return True


def _handle_attribution(days: int) -> bool:
    try:
        from services.reporting.attribution import attribution_summary, list_closed_trades

        trades = list_closed_trades(days)
        summary = attribution_summary(trades, days)
        if summary.get("empty"):
            send_telegram_message(t("attribution_no_data"))
            return True
        send_telegram_message(format_attribution_html(summary))
    except Exception:
        send_telegram_message(t("attribution_no_data"))
    return True


def handle(text: str) -> bool:
    days = _parse_days(text, "/fills")
    if days is not None:
        return _handle_fills(days)
    days = _parse_days(text, "/attribution")
    if days is not None:
        return _handle_attribution(days)
    return False
