"""Price-loop liveness for /health, /diag and operator alerts (#305)."""

from __future__ import annotations

import time
from html import escape as html_escape
from typing import Any

# (monotonic, wall-clock seconds). None until the first successful cycle.
last_cycle_completed_at: tuple[float, float] | None = None
consecutive_cycle_failures: int = 0
_cycle_failure_alert_sent: bool = False

_DEFAULT_ALERT_AFTER = 3
_DEFAULT_INTERVAL = 600


def reset_cycle_health_for_tests() -> None:
    global last_cycle_completed_at, consecutive_cycle_failures, _cycle_failure_alert_sent
    last_cycle_completed_at = None
    consecutive_cycle_failures = 0
    _cycle_failure_alert_sent = False


def _alert_after() -> int:
    try:
        from core.config import get_bot_config

        raw = get_bot_config().observability_config.get("cycle_failure_alert_after", _DEFAULT_ALERT_AFTER)
        n = int(raw)
    except Exception:
        n = _DEFAULT_ALERT_AFTER
    return max(1, n)


def _update_interval() -> float:
    try:
        from data_manager import get_config

        return float(get_config().get("update_interval", _DEFAULT_INTERVAL) or _DEFAULT_INTERVAL)
    except Exception:
        return float(_DEFAULT_INTERVAL)


def mark_cycle_success() -> None:
    """Record a completed price cycle; notify once if recovering from an alert."""
    global last_cycle_completed_at, consecutive_cycle_failures, _cycle_failure_alert_sent
    recovered = _cycle_failure_alert_sent
    n_fail = consecutive_cycle_failures
    last_cycle_completed_at = (time.monotonic(), time.time())
    consecutive_cycle_failures = 0
    _cycle_failure_alert_sent = False
    if recovered:
        try:
            from core.operator_notify import notify_operator

            notify_operator(
                f"✅ <b>Price loop recovered</b> after {n_fail} consecutive failures."
            )
        except Exception:
            pass


def mark_cycle_failure(exc: BaseException | str | None = None) -> None:
    """Count a crashed cycle; notify once when the streak hits the threshold."""
    global consecutive_cycle_failures, _cycle_failure_alert_sent
    consecutive_cycle_failures += 1
    threshold = _alert_after()
    if consecutive_cycle_failures < threshold or _cycle_failure_alert_sent:
        return
    _cycle_failure_alert_sent = True
    detail = html_escape(str(exc or "unknown")[:300], quote=False)
    try:
        from core.operator_notify import notify_operator

        notify_operator(
            f"🚨 <b>Price loop</b> — {consecutive_cycle_failures} consecutive cycle failures.\n"
            f"<code>{detail}</code>"
        )
    except Exception:
        pass


def last_cycle_age_sec() -> float | None:
    if last_cycle_completed_at is None:
        return None
    return max(0.0, time.monotonic() - last_cycle_completed_at[0])


def snapshot() -> dict[str, Any]:
    age = last_cycle_age_sec()
    return {
        "last_cycle_completed_at": last_cycle_completed_at,
        "last_cycle_age_sec": None if age is None else int(age),
        "consecutive_failures": consecutive_cycle_failures,
        "alert_sent": _cycle_failure_alert_sent,
    }


def health_payload(update_interval: float | None = None) -> tuple[dict[str, Any], int]:
    """Body + HTTP status for GET /health.

    Startup (no completed cycle yet) stays 200. After the first cycle, 503
    when the last completion is older than ``3 * update_interval``.
    """
    interval = float(update_interval) if update_interval is not None else _update_interval()
    if interval <= 0:
        interval = float(_DEFAULT_INTERVAL)
    age = last_cycle_age_sec()
    body: dict[str, Any] = {
        "status": "OK",
        "last_cycle_age_sec": None if age is None else int(age),
    }
    if age is None:
        return body, 200
    if age > 3 * interval:
        body["status"] = "stale"
        return body, 503
    return body, 200
