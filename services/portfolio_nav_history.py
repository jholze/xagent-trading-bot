"""Daily portfolio NAV history for plan-vs-actual charts."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from logger import log

_LOCK = threading.RLock()
_BOT_ROOT = Path(__file__).resolve().parents[1]
_COLLECTION = "portfolio_nav_daily"
_UNSET = object()
_BTC_USDT = "BTC/USDT"


def _display_today() -> date:
    try:
        from core.time_utils import now_display

        return now_display().date()
    except Exception:
        return date.today()


def _tenant_scope() -> tuple[str, str]:
    try:
        from core.tenant_context import resolve_tenant_id
        from data_manager import resolve_ledger_scope

        return str(resolve_tenant_id() or "default"), str(resolve_ledger_scope() or "demo")
    except Exception:
        return "default", "demo"


def _json_path(tenant_id: str, scope: str) -> Path:
    safe_t = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id) or "default"
    safe_s = "".join(c if c.isalnum() or c in "-_" else "_" for c in scope) or "demo"
    return _BOT_ROOT / "data" / f"portfolio_nav_daily.{safe_t}.{safe_s}.json"


def _use_mongo() -> bool:
    try:
        from data_manager import resolve_ledger_backend

        return resolve_ledger_backend() == "mongo"
    except Exception:
        return False


def _load_json(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        points = data.get("points") if isinstance(data, dict) else data
        return list(points) if isinstance(points, list) else []
    except Exception as e:
        log(f"nav history json load failed: {e}", "WARNING")
        return []


def _save_json(path: Path, points: list[dict]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from data_manager import atomic_write_json

        atomic_write_json(
            str(path),
            {"points": points, "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        return True
    except Exception as e:
        log(f"nav history json save failed: {e}", "WARNING")
        return False


def _load_mongo(tenant_id: str, scope: str) -> list[dict]:
    try:
        from storage.mongo_client import get_database

        db = get_database()
        cur = db[_COLLECTION].find(
            {"tenant_id": tenant_id, "ledger_scope": scope},
            {"_id": 0},
        ).sort("date", 1)
        return list(cur)
    except Exception as e:
        log(f"nav history mongo load failed: {e}", "DEBUG")
        return []


def _upsert_mongo(point: dict) -> bool:
    try:
        from storage.mongo_client import get_database

        db = get_database()
        db[_COLLECTION].update_one(
            {
                "tenant_id": point["tenant_id"],
                "ledger_scope": point["ledger_scope"],
                "date": point["date"],
            },
            {"$set": point},
            upsert=True,
        )
        return True
    except Exception as e:
        log(f"nav history mongo upsert failed: {e}", "WARNING")
        return False


def load_nav_history(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    tid, sc = _tenant_scope()
    tenant_id = tenant_id or tid
    scope = scope or sc
    with _LOCK:
        if _use_mongo():
            points = _load_mongo(tenant_id, scope)
            if not points:
                points = _load_json(_json_path(tenant_id, scope))
        else:
            points = _load_json(_json_path(tenant_id, scope))
    points = sorted(points, key=lambda p: str(p.get("date") or ""))
    if limit is not None and limit > 0:
        points = points[-int(limit) :]
    return points


def _coerce_btc_close(value: Any) -> float | None:
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price != price:  # NaN
        return None
    return price


def _lookup_btc_close() -> float | None:
    """BTC/USDT last from existing price sources. Never raises, never blocks the NAV write.

    Prefers the in-memory TTL cache / Gate ticker snapshot (#304). ``get_prices_batch``
    is a fallback outside pytest so a cold cache can still fill the row; a miss
    or any error becomes ``None`` (JSON ``null``).
    """
    try:
        from price_fetcher import peek_cached_price

        peeked = _coerce_btc_close(peek_cached_price(_BTC_USDT))
        if peeked is not None:
            return peeked
    except Exception:
        pass
    try:
        import os

        if os.environ.get("PYTEST_RUNNING") == "1":
            return None
    except Exception:
        pass
    try:
        from price_fetcher import get_prices_batch

        prices = get_prices_batch([_BTC_USDT]) or {}
        return _coerce_btc_close(prices.get(_BTC_USDT) or prices.get("BTC_USDT"))
    except Exception:
        return None


def record_nav_snapshot(
    *,
    nav: float,
    cash: float = 0.0,
    positions_mtm: float = 0.0,
    initial_capital: float = 0.0,
    on_date: date | None = None,
    tenant_id: str | None = None,
    scope: str | None = None,
    btc_close: Any = _UNSET,
) -> dict:
    """Upsert today's (or given day's) NAV point for the active tenant/scope.

    ``btc_close`` is stored when a BTC/USDT price is available; otherwise ``null``.
    A missing price never raises and never skips the NAV write.
    """
    tid, sc = _tenant_scope()
    tenant_id = tenant_id or tid
    scope = scope or sc
    d = on_date or _display_today()
    explicit_btc = btc_close is not _UNSET
    if explicit_btc:
        btc_value = _coerce_btc_close(btc_close)
    else:
        try:
            btc_value = _lookup_btc_close()
        except Exception:
            btc_value = None
    point = {
        "tenant_id": tenant_id,
        "ledger_scope": scope,
        "date": d.isoformat(),
        "nav": float(nav),
        "cash": float(cash),
        "positions_mtm": float(positions_mtm),
        "initial_capital": float(initial_capital),
        "btc_close": btc_value,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    with _LOCK:
        path = _json_path(tenant_id, scope)
        points = _load_json(path)
        if not explicit_btc and point.get("btc_close") is None:
            for p in points:
                if p.get("date") == point["date"]:
                    kept = _coerce_btc_close(p.get("btc_close"))
                    if kept is not None:
                        point["btc_close"] = kept
                    break
        replaced = False
        for i, p in enumerate(points):
            if p.get("date") == point["date"]:
                points[i] = point
                replaced = True
                break
        if not replaced:
            points.append(point)
        points.sort(key=lambda p: str(p.get("date") or ""))
        # keep a bit more than one year
        if len(points) > 400:
            points = points[-400:]
        _save_json(path, points)
        if _use_mongo():
            _upsert_mongo(point)
    return point


def _trade_history_io():
    """Same store `_equity_drawdown_pct` reads: live dry-run vs active scope."""
    from data_manager import (
        get_config,
        is_live_dry_run,
        load_live_trade_history,
        load_trade_history,
        save_live_trade_history,
        save_trade_history,
    )

    cfg = get_config()
    if is_live_dry_run(cfg):
        return load_live_trade_history, save_live_trade_history
    return load_trade_history, save_trade_history


def persist_peak_equity(nav: float) -> dict:
    """High-water mark in the active-scope trade-history document."""
    load_fn, save_fn = _trade_history_io()
    history = load_fn() or {}
    if not isinstance(history, dict):
        history = {}
    try:
        nav_f = float(nav)
    except (TypeError, ValueError):
        nav_f = 0.0
    try:
        existing = float(history.get("peak_equity") or 0)
    except (TypeError, ValueError):
        existing = 0.0
    peak = max(existing, nav_f)
    history["peak_equity"] = peak
    if peak > existing + 1e-9 or not history.get("peak_equity_at"):
        history["peak_equity_at"] = datetime.now(timezone.utc).isoformat()
    save_fn(history)
    return history


def latest_fresh_nav(*, max_age_sec: float) -> float | None:
    """Most recent snapshot NAV if `as_of` is younger than max_age_sec."""
    try:
        age_limit = float(max_age_sec)
    except (TypeError, ValueError):
        return None
    if age_limit <= 0:
        return None
    points = load_nav_history(limit=1)
    if not points:
        return None
    point = points[-1]
    try:
        nav = float(point.get("nav") or 0)
    except (TypeError, ValueError):
        return None
    if nav <= 0:
        return None
    raw_ts = point.get("as_of")
    if not raw_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None
    if age < 0 or age > age_limit:
        return None
    return nav


def capture_current_nav_snapshot() -> dict | None:
    """Compute live NAV, persist daily point + peak_equity. Returns point or None."""
    try:
        from core.portfolio_baseline import initial_capital
        from notifications.terminal_dashboard import _portfolio_snapshot

        snap = _portfolio_snapshot()
        total = float(snap.get("total_value") or 0)
        cash = float(snap.get("balance") or 0)
        pos_mv = float(snap.get("positions_market_value") or 0)
        init = float(
            snap.get("initial_capital")
            or initial_capital()
            or 0
        )
        point = record_nav_snapshot(
            nav=total,
            cash=cash,
            positions_mtm=pos_mv,
            initial_capital=init,
        )
        try:
            persist_peak_equity(total)
        except Exception as e:
            log(f"persist_peak_equity failed: {e}", "WARNING")
        return point
    except Exception as e:
        log(f"capture_current_nav_snapshot failed: {e}", "WARNING")
        return None


def history_as_day_nav_map(
    points: list[dict],
    plan_start: date,
) -> dict[int, float]:
    """Map day_index -> nav for points on/after plan_start."""
    out: dict[int, float] = {}
    for p in points:
        d = None
        try:
            d = date.fromisoformat(str(p.get("date") or "")[:10])
        except Exception:
            continue
        if d < plan_start:
            continue
        t = (d - plan_start).days
        if t < 0:
            continue
        try:
            out[t] = float(p.get("nav") or 0)
        except Exception:
            continue
    return out
