"""Path-stats refresh (cron / memory cycle). Writes only memory_path_stats.

Fail-open, ledger-safe. Kill: MEMORY_PATH_STATS=0 / path_stats.enabled=false.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from intelligence.memory.path_stats import (
    compute_path_stats_for_ohlcv,
    path_stats_enabled,
    upsert_path_summaries,
)
from logger import log

# Module-level throttle (process-local; fine for hermes single worker)
_LAST_REFRESH_AT: float = 0.0
_LAST_RESULT: dict[str, Any] = {}


def _norm_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper().replace("-", "/")
    if not s:
        return ""
    if "/" not in s and s.endswith("USDT") and len(s) > 4:
        s = f"{s[:-4]}/USDT"
    return s


def _symbols_from_positions(scope: str, limit: int) -> list[str]:
    from strategies.positions import is_open_position, load_positions, parse_position_key, positions

    load_positions(scope)
    out: list[str] = []
    for key, pos in list(positions.items()):
        if not is_open_position(pos):
            continue
        sym, _tf = parse_position_key(key)
        if not sym:
            sym = str(pos.get("symbol") or "").replace("_", "/")
        sym = _norm_symbol(sym)
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def _order_ts(o: dict) -> str:
    for k in (
        "filled_at",
        "timestamp",
        "ts",
        "created_at",
        "updated_at",
        "time",
        "closed_at",
    ):
        v = o.get(k)
        if v:
            return str(v)
    return ""


def _symbols_from_trades(scope: str, *, max_orders: int, limit: int) -> list[str]:
    from data_manager import load_orders

    doc = load_orders(scope) or {}
    orders = list(doc.get("orders") or [])
    orders.sort(key=_order_ts, reverse=True)
    out: list[str] = []
    scanned = 0
    for o in orders:
        if scanned >= max_orders:
            break
        status = str(o.get("status") or "").lower()
        if status and status not in ("filled", "closed", "complete", "completed"):
            if not (o.get("amount") or o.get("filled") or o.get("qty")):
                continue
        scanned += 1
        sym = _norm_symbol(str(o.get("symbol") or o.get("pair") or ""))
        if not sym:
            continue
        if sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def _symbols_from_watchlist(limit: int) -> list[str]:
    try:
        from data_manager import load_watchlist

        wl = load_watchlist()
        if isinstance(wl, list):
            coins = wl
        elif isinstance(wl, dict):
            coins = wl.get("coins") or wl.get("watchlist") or []
        else:
            coins = []
        out: list[str] = []
        for c in coins:
            if isinstance(c, str):
                sym = _norm_symbol(c)
            elif isinstance(c, dict):
                sym = _norm_symbol(str(c.get("symbol") or c.get("pair") or ""))
            else:
                continue
            if sym and sym not in out:
                out.append(sym)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _merge_universe(
    *,
    open_syms: list[str],
    trade_syms: list[str],
    watch_syms: list[str],
    limit: int,
) -> tuple[list[str], dict[str, str]]:
    out: list[str] = []
    source: dict[str, str] = {}
    for s in open_syms:
        if s not in source:
            source[s] = "open"
            out.append(s)
        if len(out) >= limit:
            return out, source
    for s in trade_syms:
        if s not in source:
            source[s] = "trade"
            out.append(s)
        if len(out) >= limit:
            return out, source
    for s in watch_syms:
        if s not in source:
            source[s] = "watch"
            out.append(s)
        if len(out) >= limit:
            return out, source
    return out, source


def _df_to_rows(df) -> list[list[float]]:
    rows: list[list[float]] = []
    cols = {str(c).lower(): c for c in getattr(df, "columns", [])}
    if "high" in cols and "low" in cols and "close" in cols:
        o_c = cols.get("open", cols["close"])
        h_c, l_c, c_c = cols["high"], cols["low"], cols["close"]
        v_c = cols.get("volume")
        for i, (_, r) in enumerate(df.iterrows()):
            rows.append(
                [
                    i,
                    float(r[o_c]),
                    float(r[h_c]),
                    float(r[l_c]),
                    float(r[c_c]),
                    float(r[v_c]) if v_c is not None else 0.0,
                ]
            )
    else:
        for i, (_, r) in enumerate(df.iterrows()):
            vals = list(r.values)
            if len(vals) < 4:
                continue
            rows.append(
                [
                    i,
                    float(vals[0]),
                    float(vals[1]),
                    float(vals[2]),
                    float(vals[3]),
                    float(vals[4]) if len(vals) > 4 else 0.0,
                ]
            )
    return rows


def refresh_path_stats(
    *,
    config: dict | None = None,
    limit: int | None = None,
    write: bool = True,
    include_trades: bool = True,
    include_watchlist: bool = True,
    trade_orders: int | None = None,
) -> dict[str, Any]:
    """Compute path stats for open+trade(+watch) universe. Optional Mongo write."""
    global _LAST_REFRESH_AT, _LAST_RESULT

    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
    except Exception:
        config = config or {}

    ps = ((config or {}).get("memory") or {}).get("path_stats") or {}
    enabled = path_stats_enabled(config)
    limit = int(limit if limit is not None else ps.get("cycle_limit") or ps.get("limit") or 40)
    limit = max(1, min(200, limit))
    timeframe = str(ps.get("timeframe") or "1h")
    ohlcv_limit = int(ps.get("ohlcv_limit") or 500)
    bands_pct = ps.get("bands_pct") or [5, 8, 10, 12, 15, 20]
    bands = [float(x) / 100.0 for x in bands_pct]
    trough_lb = int(ps.get("trough_lookback") or 48)
    forward = int(ps.get("forward_bars") or 24)
    trade_orders = int(
        trade_orders
        if trade_orders is not None
        else ps.get("trade_orders_scan") or 500
    )
    if include_trades is True and "include_trade_symbols" in ps:
        include_trades = bool(ps.get("include_trade_symbols", True))

    try:
        from data_manager import resolve_ledger_scope

        scope = resolve_ledger_scope() or "demo"
    except Exception:
        scope = "demo"

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "timeframe": timeframe,
        "enabled": enabled,
        "write": bool(write) and enabled,
        "summary": {
            "ok": 0,
            "thin": 0,
            "errors": 0,
            "bands": 0,
            "writes": 0,
            "n_merged": 0,
            "skipped": False,
            "reason": "",
        },
    }

    if not enabled:
        report["summary"]["skipped"] = True
        report["summary"]["reason"] = "disabled"
        _LAST_RESULT = report
        return report

    open_syms = _symbols_from_positions(scope, limit)
    trade_syms: list[str] = []
    if include_trades:
        try:
            trade_syms = _symbols_from_trades(
                scope, max_orders=max(1, trade_orders), limit=limit
            )
        except Exception as e:
            log(f"path_stats trades universe: {e}", "DEBUG")
    watch_syms: list[str] = []
    if include_watchlist:
        watch_syms = _symbols_from_watchlist(limit)

    symbols, _src = _merge_universe(
        open_syms=open_syms,
        trade_syms=trade_syms,
        watch_syms=watch_syms,
        limit=limit,
    )
    report["summary"]["n_merged"] = len(symbols)
    report["summary"]["n_open"] = len(open_syms)
    report["summary"]["n_trade_syms"] = len(trade_syms)
    report["summary"]["n_watch"] = len(watch_syms)

    if not symbols:
        report["summary"]["skipped"] = True
        report["summary"]["reason"] = "empty_universe"
        _LAST_REFRESH_AT = time.time()
        _LAST_RESULT = report
        return report

    try:
        from services.market_service import MarketService

        market = MarketService(config_raw=config)
    except Exception as e:
        report["summary"]["errors"] = 1
        report["summary"]["reason"] = f"market_service:{e}"[:160]
        log(f"path_stats refresh market: {e}", "WARNING")
        _LAST_RESULT = report
        return report

    all_summaries = []
    for sym in symbols:
        try:
            df = market.fetch_ohlcv(sym, timeframe, limit=ohlcv_limit)
            if df is None or getattr(df, "empty", True):
                report["summary"]["errors"] += 1
                continue
            rows = _df_to_rows(df)
            summaries = compute_path_stats_for_ohlcv(
                sym,
                timeframe,
                rows,
                ledger_scope=scope,
                bands=bands,
                trough_lookback=trough_lb,
                forward_bars=forward,
            )
            all_summaries.extend(summaries)
            for s in summaries:
                report["summary"]["bands"] += 1
                if s.sample_quality == "ok":
                    report["summary"]["ok"] += 1
                else:
                    report["summary"]["thin"] += 1
        except Exception as e:
            report["summary"]["errors"] += 1
            log(f"path_stats refresh {sym}: {e}", "DEBUG")

    if write and enabled and all_summaries:
        w = upsert_path_summaries(all_summaries, config=config, force=False)
        report["summary"]["writes"] = w

    _LAST_REFRESH_AT = time.time()
    _LAST_RESULT = report
    log(
        f"path_stats refresh done merged={len(symbols)} "
        f"ok={report['summary']['ok']} thin={report['summary']['thin']} "
        f"writes={report['summary']['writes']}",
        "INFO",
    )
    return report


def maybe_refresh_path_stats(
    *,
    config: dict | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Throttle-aware refresh for hermes/memory cycle. Fail-open."""
    global _LAST_REFRESH_AT

    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
    except Exception:
        config = config or {}

    ps = ((config or {}).get("memory") or {}).get("path_stats") or {}
    if not path_stats_enabled(config):
        return {"skipped": True, "reason": "disabled"}
    if not bool(ps.get("refresh_in_memory_cycle", True)):
        return {"skipped": True, "reason": "refresh_in_memory_cycle=false"}

    interval_h = float(ps.get("refresh_interval_hours") or 12)
    interval_s = max(300.0, interval_h * 3600.0)
    now = time.time()
    if not force and _LAST_REFRESH_AT and (now - _LAST_REFRESH_AT) < interval_s:
        return {
            "skipped": True,
            "reason": "throttled",
            "seconds_since": round(now - _LAST_REFRESH_AT, 1),
            "interval_hours": interval_h,
            "last": _LAST_RESULT.get("summary") if _LAST_RESULT else {},
        }

    try:
        return refresh_path_stats(config=config, write=True)
    except Exception as e:
        log(f"path_stats maybe_refresh: {e}", "WARNING")
        return {"skipped": True, "reason": f"error:{e}"[:160]}
