"""Read-only NAV / drawdown pack for the MCP ``xagent_nav`` tool.

Sources (all reads, no writes):

- ``portfolio_nav_daily``: one point per tenant, ledger scope and day, written
  by the bot each cycle (``nav``, ``cash``, ``positions_mtm``,
  ``initial_capital``, ``as_of``).
- ``trade_history`` ``<tenant>:demo`` and ``<tenant>:live``: ``peak_equity``
  (the high-water mark the risk throttle uses; live dry-run tenants keep it
  in ``:live``).
- Open lots (same loader as ``xagent_lots``) for the position and lock counts.
- Ledger epoch (``scripts/ledger_epoch.py``): ``ledger_epoch`` marker in the
  trade_history docs, else the newest ``ledger_epochs`` doc of the tenant.
  Peak and drawdown count only from the current epoch start; the values from
  before the epoch are returned separately under ``pre_epoch``.

Only whitelisted numeric/date fields are returned. Nothing here writes to
Mongo, the ledger or a file.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

NAV_COLLECTION = "portfolio_nav_daily"
PEAK_SCOPES = ("demo", "live")
MAX_CURVE_POINTS = 400
_POINT_FIELDS = ("date", "nav", "cash", "positions_mtm", "initial_capital", "as_of", "btc_close")


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _round(value: float | None, nd: int = 2) -> float | None:
    return None if value is None else round(value, nd)


def _parse_day(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _clean_point(p: dict) -> dict:
    out: dict[str, Any] = {}
    for key in _POINT_FIELDS:
        val = p.get(key)
        if key in ("date", "as_of"):
            out[key] = None if val is None else str(val)
        else:
            out[key] = _num(val)
    return out


def load_nav_points_readonly(tenant_id: str, *, db=None) -> list[dict]:
    """All NAV points of one tenant (every ledger scope), oldest first. find() only."""
    if db is None:
        from storage.mongo_client import get_database

        db = get_database()
    cur = db[NAV_COLLECTION].find(
        {"tenant_id": tenant_id},
        {"_id": 0, "ledger_scope": 1, **{k: 1 for k in _POINT_FIELDS}},
    )
    return [dict(p) for p in cur if isinstance(p, dict)]


def load_peak_equity_readonly(tenant_id: str, scope: str) -> dict:
    """``peak_equity`` / ``peak_equity_at`` of ``trade_history`` <tenant>:<scope>. Read only."""
    from data_manager import _mongo_ledger_store, get_config

    try:
        cfg = get_config()
    except Exception:
        cfg = None
    doc = _mongo_ledger_store(cfg).load_trade_history(scope, tenant_id=tenant_id) or {}
    marker = doc.get("ledger_epoch") if isinstance(doc.get("ledger_epoch"), dict) else {}
    return {
        "peak_equity": doc.get("peak_equity"),
        "peak_equity_at": doc.get("peak_equity_at"),
        "ledger_epoch": {k: marker.get(k) for k in ("epoch_id", "started_at") if marker.get(k) is not None},
    }


def load_epoch_readonly(tenant_id: str, *, db=None) -> dict:
    """Newest ``ledger_epochs`` marker of one tenant (epoch_id, started_at). find() only."""
    if db is None:
        from storage.mongo_client import get_database

        db = get_database()
    cur = db["ledger_epochs"].find(
        {"tenant_id": tenant_id}, {"_id": 0, "epoch_id": 1, "started_at": 1}
    )
    best: dict = {}
    for doc in cur:
        if isinstance(doc, dict) and str(doc.get("started_at") or "") > str(best.get("started_at") or ""):
            best = {"epoch_id": doc.get("epoch_id"), "started_at": str(doc.get("started_at"))}
    return best


def _parse_ts(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        ts = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _in_epoch(point: dict, start: datetime | None) -> bool:
    """A daily point belongs to the epoch when it was last written at/after the start.

    The bot overwrites today's point every cycle, so the reset day's point
    carries post-reset NAV once ``as_of`` is past the start. Without ``as_of``
    fall back to the day (strictly after the start day).
    """
    if start is None:
        return True
    ts = _parse_ts(point.get("as_of"))
    if ts is not None:
        return ts >= start
    day = _parse_day(point.get("date"))
    return day is not None and day > start.date()


def load_lots_readonly(tenant_id: str) -> list[dict]:
    from services.desk.snapshot import _load_lots

    return _load_lots(tenant_id)


def _lot_locked(lot: dict) -> bool:
    try:
        from strategies.position_lock import get_lock, lock_is_active

        return bool(lock_is_active(get_lock(lot)))
    except Exception:
        # Degraded path: only count locks that are enabled and carry no expiry.
        lock = lot.get("lock") if isinstance(lot, dict) else None
        return isinstance(lock, dict) and bool(lock.get("enabled", True)) and not lock.get("until")


def _pick_scope(points: list[dict]) -> str | None:
    """Ledger scope of the newest point (by as_of, then date)."""
    best = None
    best_key = ("", "")
    for p in points:
        if not isinstance(p, dict):
            continue
        as_of = p.get("as_of")
        key = ("" if as_of is None else str(as_of), str(p.get("date") or ""))
        if best is None or key > best_key:
            best, best_key = p, key
    return None if best is None else str(best.get("ledger_scope") or "")


def curve_max_drawdown(navs: Iterable[float], seed_peak: float | None = None) -> tuple[float, float]:
    """(max drawdown %, running peak) over a NAV sequence; peak seeded by ``seed_peak``."""
    peak = seed_peak if seed_peak and seed_peak > 0 else None
    max_dd = 0.0
    for nav in navs:
        if nav is None:
            continue
        peak = nav if peak is None else max(peak, nav)
        if peak > 0:
            max_dd = max(max_dd, (peak - nav) / peak * 100.0)
    return max_dd, (peak or 0.0)


def nav_pack(
    tenant_id: str,
    *,
    curve: bool = False,
    curve_from: str | None = None,
    curve_to: str | None = None,
    points_fn: Callable[[str], list[dict]] | None = None,
    peak_fn: Callable[[str, str], dict] | None = None,
    lots_fn: Callable[[str], list[dict]] | None = None,
    epoch_fn: Callable[[str], dict] | None = None,
) -> dict:
    points_fn = points_fn or load_nav_points_readonly
    peak_fn = peak_fn or load_peak_equity_readonly
    lots_fn = lots_fn or load_lots_readonly
    if epoch_fn is None:
        epoch_fn = load_epoch_readonly if points_fn is load_nav_points_readonly else (lambda _t: {})
    errors: list[str] = []

    d_from = _parse_day(curve_from)
    d_to = _parse_day(curve_to)
    if (curve_from and d_from is None) or (curve_to and d_to is None):
        return {"ok": False, "error": "bad_date", "hint": "use YYYY-MM-DD"}

    try:
        raw_points = points_fn(tenant_id) or []
    except Exception:
        raw_points = []
        errors.append("nav_history_unavailable")
    scope = _pick_scope(raw_points)
    points = [
        _clean_point(p)
        for p in raw_points
        if isinstance(p, dict) and str(p.get("ledger_scope") or "") == (scope or "")
    ]
    points = [p for p in points if p["date"] and p["nav"] is not None]
    points.sort(key=lambda p: p["date"])
    latest = points[-1] if points else None

    peaks: dict[str, dict] = {}
    markers: list[dict] = []
    for sc in PEAK_SCOPES:
        try:
            got = peak_fn(tenant_id, sc) or {}
        except Exception:
            errors.append(f"peak_{sc}_unavailable")
            got = {}
        if isinstance(got.get("ledger_epoch"), dict) and got["ledger_epoch"].get("started_at"):
            markers.append(got["ledger_epoch"])
        peaks[sc] = {
            "peak_equity": _round(_num(got.get("peak_equity"))),
            "peak_equity_at": None if got.get("peak_equity_at") is None else str(got.get("peak_equity_at")),
        }

    try:
        got_epoch = epoch_fn(tenant_id) or {}
    except Exception:
        got_epoch = {}
        errors.append("ledger_epoch_unavailable")
    if isinstance(got_epoch, dict) and got_epoch.get("started_at"):
        markers.append(got_epoch)
    epoch = max(markers, key=lambda m: str(m.get("started_at"))) if markers else {}
    epoch_start = _parse_ts(epoch.get("started_at"))
    if epoch and epoch_start is None:
        errors.append("ledger_epoch_bad_started_at")

    cur_pts = [p for p in points if _in_epoch(p, epoch_start)]
    pre_pts = [p for p in points if not _in_epoch(p, epoch_start)]

    initial = None
    for p in reversed(cur_pts or points):
        if p["initial_capital"]:
            initial = p["initial_capital"]
            break

    def _epoch_peak(sc: str) -> float | None:
        # A trade_history HWM set before the epoch start is history, not this epoch.
        val = peaks[sc]["peak_equity"]
        if val is None or epoch_start is None:
            return val
        at = _parse_ts(peaks[sc]["peak_equity_at"])
        return val if at is not None and at >= epoch_start else None

    navs = [p["nav"] for p in cur_pts]
    curve_dd, curve_peak = curve_max_drawdown(navs, seed_peak=initial)
    candidates = {
        "nav_curve": curve_peak or None,
        "initial_capital": initial,
        "trade_history_demo": _epoch_peak("demo"),
        "trade_history_live": _epoch_peak("live"),
    }
    valid = {k: v for k, v in candidates.items() if v is not None and v > 0}
    peak_nav = max(valid.values()) if valid else None
    peak_source = max(valid, key=valid.get) if valid else None

    nav = latest["nav"] if latest else None
    current_dd = None
    if nav is not None and peak_nav:
        current_dd = max(0.0, (peak_nav - nav) / peak_nav * 100.0)
    max_dd = max(x for x in (curve_dd, current_dd or 0.0))
    ret = None
    if nav is not None and initial:
        ret = (nav - initial) / initial * 100.0

    pre_epoch = None
    if epoch_start is not None:
        pre_init = next((p["initial_capital"] for p in reversed(pre_pts) if p["initial_capital"]), None)
        pre_dd, pre_peak = curve_max_drawdown([p["nav"] for p in pre_pts], seed_peak=pre_init)
        pre_epoch = {
            "peak_nav": _round(pre_peak or None),
            "max_drawdown_pct": _round(pre_dd) if pre_pts else None,
            "initial_capital": _round(pre_init),
            "last_nav": _round(pre_pts[-1]["nav"]) if pre_pts else None,
            "nav_points": len(pre_pts),
            "first_date": pre_pts[0]["date"] if pre_pts else None,
            "last_date": pre_pts[-1]["date"] if pre_pts else None,
            "trade_history_peaks_before_epoch": {
                sc: peaks[sc]["peak_equity"]
                for sc in PEAK_SCOPES
                if peaks[sc]["peak_equity"] is not None and _epoch_peak(sc) is None
            },
        }

    lots: list[dict] = []
    try:
        lots = [lot for lot in (lots_fn(tenant_id) or []) if isinstance(lot, dict)]
    except Exception:
        errors.append("lots_unavailable")

    out: dict[str, Any] = {
        "ok": True,
        "tenant_id": tenant_id,
        "ledger_scope": scope,
        "as_of": latest["as_of"] if latest else None,
        "date": latest["date"] if latest else None,
        "nav": _round(nav),
        "cash": _round(latest["cash"]) if latest else None,
        "positions_mtm": _round(latest["positions_mtm"]) if latest else None,
        "initial_capital": _round(initial),
        "return_pct": _round(ret),
        "peak_nav": _round(peak_nav),
        "peak_source": peak_source,
        "peak_candidates": {k: _round(v) for k, v in candidates.items()},
        "peak_equity_by_scope": peaks,
        "current_drawdown_pct": _round(current_dd),
        "max_drawdown_pct": _round(max_dd),
        "max_drawdown_curve_pct": _round(curve_dd),
        "ledger_epoch": (
            {"epoch_id": epoch.get("epoch_id"), "started_at": epoch.get("started_at")} if epoch else None
        ),
        "nav_points": len(cur_pts),
        "first_date": cur_pts[0]["date"] if cur_pts else None,
        "open_positions": len(lots),
        "locked_lots": sum(1 for lot in lots if _lot_locked(lot)),
        "notes": (
            "read-only. nav/cash = newest portfolio_nav_daily point (bot writes it each cycle). "
            "peak_nav = max(nav curve, initial_capital, trade_history peak_equity :demo and :live), "
            "all counted only from the current ledger epoch start (pre_epoch holds the older values). "
            "max_drawdown_pct = max(daily-curve drawdown seeded at initial_capital, current drawdown vs peak_nav). "
            "peak_equity_by_scope.live is the HWM the risk throttle uses on live dry-run (shadow) tenants."
        ),
    }
    if scope and peak_source and peak_source.startswith("trade_history_") and not peak_source.endswith(scope):
        # HWM comes from the other ledger scope than nav/cash (e.g. a paper tenant with a :live peak).
        out["peak_scope_mismatch"] = True
    if pre_epoch is not None:
        out["pre_epoch"] = pre_epoch
    if not points:
        errors.append("no_nav_points")
    if curve:
        sel = [
            p
            for p in points
            if (d_from is None or p["date"][:10] >= d_from.isoformat())
            and (d_to is None or p["date"][:10] <= d_to.isoformat())
        ]
        truncated = len(sel) > MAX_CURVE_POINTS
        sel = sel[-MAX_CURVE_POINTS:]
        def _seed(pts: list[dict]) -> float | None:
            ini = next((q["initial_capital"] for q in pts if q["initial_capital"]), None)
            return ini if ini and ini > 0 else None

        # Running peak restarts at the epoch start (seeded by the epoch's initial capital).
        running = _seed(pre_pts) if pre_pts else initial if initial and initial > 0 else None
        in_cur = False
        for p in points:
            if sel and p["date"] >= sel[0]["date"]:
                break
            if epoch_start is not None and not in_cur and _in_epoch(p, epoch_start):
                in_cur, running = True, initial if initial and initial > 0 else None
            running = p["nav"] if running is None else max(running, p["nav"])
        rows = []
        for p in sel:
            if epoch_start is not None and not in_cur and _in_epoch(p, epoch_start):
                in_cur, running = True, initial if initial and initial > 0 else None
            running = p["nav"] if running is None else max(running, p["nav"])
            dd = (running - p["nav"]) / running * 100.0 if running and running > 0 else 0.0
            rows.append(
                {
                    "date": p["date"],
                    "nav": _round(p["nav"]),
                    "cash": _round(p["cash"]),
                    "positions_mtm": _round(p["positions_mtm"]),
                    "drawdown_pct": _round(dd),
                    **({"epoch": "current" if _in_epoch(p, epoch_start) else "pre"} if epoch_start else {}),
                }
            )
        out["curve"] = rows
        out["curve_truncated"] = truncated
    if errors:
        out["errors"] = errors
    return out
