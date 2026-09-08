"""Hermes fold-geometry and inconclusive-verdict health (#308)."""

from __future__ import annotations

from typing import Any

from hermes.validation import inspect_fold_geometry
from logger import log

geometry_ok: bool = True
geometry_detail: str = ""
_geometry_alert_sent: bool = False
_inconclusive_alert_sent: bool = False


def reset_hermes_health_for_tests() -> None:
    global geometry_ok, geometry_detail, _geometry_alert_sent, _inconclusive_alert_sent
    geometry_ok = True
    geometry_detail = ""
    _geometry_alert_sent = False
    _inconclusive_alert_sent = False


def check_fold_geometry(hermes_cfg: dict | None) -> bool:
    """Validate walk-forward windows; notify once and refuse cycles if invalid."""
    global geometry_ok, geometry_detail, _geometry_alert_sent
    geo = inspect_fold_geometry(hermes_cfg or {})
    geometry_ok = geo.ok
    geometry_detail = geo.message
    if geo.ok:
        _geometry_alert_sent = False
        return True
    log(geo.message, "ERROR")
    if not _geometry_alert_sent:
        _geometry_alert_sent = True
        try:
            from core.operator_notify import notify_operator

            notify_operator(f"🚨 <b>Hermes fold geometry</b>\n{geo.message}")
        except Exception:
            pass
    return False


def inconclusive_stats(
    hermes_cfg: dict | None = None,
    last_verdict: str | None = None,
) -> dict[str, Any]:
    health = (hermes_cfg or {}).get("health") or {}
    try:
        window = max(1, int(health.get("inconclusive_window", 20)))
    except (TypeError, ValueError):
        window = 20
    try:
        alert_pct = float(health.get("inconclusive_alert_pct", 50))
    except (TypeError, ValueError):
        alert_pct = 50.0

    from hermes.memory import store

    recent = store.recent_experiments(window)
    n = len(recent)
    inc = sum(1 for e in recent if str(e.get("verdict") or "").lower() == "inconclusive")
    window_pct = (100.0 * inc / n) if n else 0.0
    if last_verdict is None:
        last = str((recent[-1].get("verdict") if recent else "") or "")
    else:
        last = str(last_verdict or "")
    last_cycle_pct = 100.0 if last.lower() == "inconclusive" else 0.0
    return {
        "window": window,
        "window_n": n,
        "window_inconclusive": inc,
        "window_pct": window_pct,
        "last_cycle_verdict": last,
        "last_cycle_pct": last_cycle_pct,
        "alert_pct": alert_pct,
        "alert": bool(n > 0 and window_pct > alert_pct),
    }


def update_inconclusive_health(
    hermes_cfg: dict | None,
    last_verdict: str | None = None,
) -> dict[str, Any]:
    """Refresh inconclusive share; notify once per episode above the threshold."""
    global _inconclusive_alert_sent
    stats = inconclusive_stats(hermes_cfg, last_verdict=last_verdict)
    if stats["alert"]:
        if not _inconclusive_alert_sent:
            _inconclusive_alert_sent = True
            try:
                from core.operator_notify import notify_operator

                notify_operator(
                    "⚠️ <b>Hermes inconclusive share</b>\n"
                    f"Last cycle: {stats['last_cycle_pct']:.0f}% inconclusive.\n"
                    f"Last {stats['window']}: {stats['window_inconclusive']}/"
                    f"{stats['window_n']} ({stats['window_pct']:.0f}%) "
                    f"&gt; {stats['alert_pct']:.0f}% threshold.\n"
                    "0-trade windows are not strategy evidence."
                )
            except Exception:
                pass
    elif _inconclusive_alert_sent:
        _inconclusive_alert_sent = False
        try:
            from core.operator_notify import notify_operator

            notify_operator(
                "✅ <b>Hermes inconclusive share recovered</b>\n"
                f"Last {stats['window']}: {stats['window_inconclusive']}/"
                f"{stats['window_n']} ({stats['window_pct']:.0f}%) "
                f"under {stats['alert_pct']:.0f}%."
            )
        except Exception:
            pass
    return stats


def format_status_lines(hermes_cfg: dict | None) -> list[str]:
    geo = inspect_fold_geometry(hermes_cfg or {})
    if geo.ok:
        geo_line = f"Geometry: OK ({geo.message})"
    else:
        geo_line = f"Geometry: INVALID — {geo.message}"
    stats = inconclusive_stats(hermes_cfg)
    inc_line = (
        f"Inconclusive: last cycle {stats['last_cycle_pct']:.0f}% | "
        f"last {stats['window']}: {stats['window_inconclusive']}/{stats['window_n']} "
        f"({stats['window_pct']:.0f}%)"
    )
    if stats["alert"]:
        inc_line += " ALERT"
    return [geo_line, inc_line]


def format_inconclusive_summary(hermes_cfg: dict | None) -> str:
    stats = inconclusive_stats(hermes_cfg)
    return (
        f"Inconclusive last cycle {stats['last_cycle_pct']:.0f}% | "
        f"last {stats['window']}: {stats['window_inconclusive']}/{stats['window_n']} "
        f"({stats['window_pct']:.0f}%)."
    )
