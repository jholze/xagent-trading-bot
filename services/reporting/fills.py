"""Fill quality: slippage in bps and fee drag, computed on read (#307).

Sign convention for ``slippage_bps``: positive = worse than requested.
BUY/COVER: exec > req is worse. SELL/SHORT: exec < req is worse.

No ledger field is written; callers pass stored ``request.price`` /
``execution.price`` / ``execution.fee`` / ``execution.venue``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable

from core.models import is_executed_status
from services.reporting import clamp_days

_WORSE_IF_HIGHER = frozenset({"buy", "cover"})
_WORSE_IF_LOWER = frozenset({"sell", "short"})


def _as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _side_token(order: dict) -> str:
    raw = order.get("side") or order.get("type") or ""
    inner = getattr(raw, "value", raw)
    return str(inner or "").strip().lower()


def _event_ts(order: dict) -> datetime | None:
    try:
        from storage.order_ledger_v2 import order_event_ts_naive

        return order_event_ts_naive(order)
    except Exception:
        pass
    ts = order.get("timestamps") or {}
    raw = ts.get("filled") or ts.get("created") or ts.get("updated") or order.get("timestamp")
    if not raw:
        return None
    try:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    except Exception:
        return None


def _window_now() -> datetime:
    try:
        from core.time_utils import now_display

        n = now_display()
        return n.replace(tzinfo=None) if n.tzinfo is not None else n
    except Exception:
        return datetime.now()


def _in_window(order: dict, cutoff: datetime, now: datetime) -> bool:
    ts = _event_ts(order)
    if ts is None:
        return True
    return cutoff <= ts <= now + timedelta(seconds=5)


def _is_filled(order: dict) -> bool:
    if not isinstance(order, dict):
        return False
    status = order.get("status")
    if status is None:
        exe = order.get("execution") or {}
        return _as_float(exe.get("price")) > 0
    return is_executed_status(status)


def slippage_bps(order: dict) -> float | None:
    """Positive = worse than requested. ``None`` when prices are missing."""
    if not isinstance(order, dict):
        return None
    req = order.get("request") or {}
    exe = order.get("execution") or {}
    req_p = _as_float(req.get("price"))
    exe_p = _as_float(exe.get("price"))
    if req_p <= 0 or exe_p <= 0:
        return None
    raw = (exe_p - req_p) / req_p * 10_000.0
    side = _side_token(order)
    if side in _WORSE_IF_LOWER:
        raw = -raw
    return raw


def venue_key(order: dict) -> str:
    exe = (order.get("execution") or {}) if isinstance(order, dict) else {}
    venue = exe.get("venue")
    if isinstance(venue, dict):
        for key in ("exchange", "name", "id", "capture"):
            val = venue.get(key)
            if val:
                return str(val)
        return "unknown"
    if venue:
        return str(venue)
    return "unknown"


def _fee_usdt(order: dict) -> float:
    exe = order.get("execution") or {}
    fee = exe.get("fee")
    if isinstance(fee, dict):
        fee = fee.get("usdt", fee.get("cost", fee.get("fee")))
    return max(0.0, _as_float(fee))


def _order_pnl(order: dict) -> float:
    raw = order.get("pnl")
    if isinstance(raw, dict):
        raw = raw.get("usdt", raw.get("realized", raw.get("pnl")))
    return _as_float(raw)


def percentile(values: Iterable[float], p: float) -> float | None:
    """Linear-interpolation percentile. ``p`` in 0..100."""
    xs = [float(v) for v in values]
    if not xs:
        return None
    xs.sort()
    if len(xs) == 1:
        return xs[0]
    p = min(100.0, max(0.0, float(p)))
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    w = k - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def _group_slippage(samples: list[tuple[str, float]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for key, bps in samples:
        buckets.setdefault(key, []).append(bps)
    out: dict[str, dict[str, Any]] = {}
    for key, xs in buckets.items():
        med = float(median(xs)) if xs else None
        p90 = percentile(xs, 90.0)
        out[key] = {
            "n": len(xs),
            "median_bps": round(med, 4) if med is not None else None,
            "p90_bps": round(p90, 4) if p90 is not None else None,
        }
    return out


def fill_quality_summary(orders: Iterable[dict], days: int = 7) -> dict[str, Any]:
    """Median/p90 slippage by side and venue, plus fee drag vs gross realized P&L."""
    days = clamp_days(days)
    now = _window_now()
    cutoff = now - timedelta(days=days)
    filled: list[dict] = []
    for order in orders or []:
        if not _is_filled(order):
            continue
        if not _in_window(order, cutoff, now):
            continue
        filled.append(order)

    side_samples: list[tuple[str, float]] = []
    venue_samples: list[tuple[str, float]] = []
    total_fees = 0.0
    gross_realized = 0.0
    for order in filled:
        bps = slippage_bps(order)
        if bps is not None:
            side = _side_token(order) or "unknown"
            side_samples.append((side, bps))
            venue_samples.append((venue_key(order), bps))
        total_fees += _fee_usdt(order)
        if _side_token(order) in ("sell", "cover"):
            gross_realized += _order_pnl(order)

    fee_drag_pct = None
    denom = abs(gross_realized)
    if denom > 1e-12:
        fee_drag_pct = round(100.0 * total_fees / denom, 4)
    elif total_fees > 0:
        fee_drag_pct = None

    return {
        "days": days,
        "n_fills": len(filled),
        "n_slippage": len(side_samples),
        "by_side": _group_slippage(side_samples),
        "by_venue": _group_slippage(venue_samples),
        "total_fees": round(total_fees, 6),
        "gross_realized_pnl": round(gross_realized, 6),
        "fee_drag_pct": fee_drag_pct,
    }


def _filled_from_v2(days: int) -> list[dict] | None:
    """Return filled orders from v2, or None when v2 is unavailable."""
    try:
        from storage.order_ledger_v2 import (
            get_order_ledger_v2,
            order_ledger_v2_reads_enabled,
        )

        if not order_ledger_v2_reads_enabled():
            return None
        store = get_order_ledger_v2()
        if store is None:
            return None
    except Exception:
        return None

    try:
        from core.tenant_context import resolve_tenant_id
        from data_manager import resolve_ledger_scope

        tid = resolve_tenant_id()
        scope = resolve_ledger_scope()
        now = _window_now()
        out: list[dict] = []
        seen: set[str] = set()
        for i in range(int(days) + 1):
            day = (now.date() - timedelta(days=i)).strftime("%Y-%m-%d")
            try:
                rows = store.query_day(
                    tid, scope, day, filled_only=True, limit=10_000,
                )
            except Exception:
                continue
            for row in rows or []:
                oid = str(row.get("id") or "")
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                out.append(row)
        return out
    except Exception:
        return None


def _filled_from_blob() -> list[dict]:
    from data_manager import load_orders, resolve_ledger_scope

    blob = load_orders(resolve_ledger_scope()) or {}
    rows = blob.get("orders") or []
    return [o for o in rows if isinstance(o, dict) and _is_filled(o)]


def list_filled_orders(days: int = 7) -> list[dict]:
    """Filled orders in the lookback window.

    Prefer order-ledger v2 when reads are enabled; always fall back to (and
    union with) the orders document so blob-only fills are not dropped.
    """
    days = clamp_days(days)
    now = _window_now()
    cutoff = now - timedelta(days=days)
    v2_rows = _filled_from_v2(days)
    try:
        blob_rows = _filled_from_blob()
    except Exception:
        blob_rows = []
    if v2_rows is None:
        rows = blob_rows
    else:
        seen = {str(o.get("id") or "") for o in v2_rows if o.get("id")}
        rows = list(v2_rows)
        for order in blob_rows:
            oid = str(order.get("id") or "")
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            rows.append(order)
    return [o for o in rows if _in_window(o, cutoff, now)]
