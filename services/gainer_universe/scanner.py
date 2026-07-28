"""Gate live tickers + daily day-return board (no orders)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from logger import log
from services.gainer_universe.filters import normalize_symbol, passes_spot_usdt_filter


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_gate_tickers() -> dict[str, dict]:
    import ccxt

    ex = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    return ex.fetch_tickers() or {}


def filter_and_rank_live(
    tickers: dict[str, dict],
    cfg: dict,
) -> list[dict]:
    """Rank by 24h % among liquid USDT spot pairs."""
    min_vol = float(cfg.get("min_volume_usdt_24h") or 500_000)
    out: list[dict] = []
    for raw_sym, t in (tickers or {}).items():
        if not isinstance(t, dict):
            continue
        sym = normalize_symbol(raw_sym)
        if not passes_spot_usdt_filter(
            sym,
            blacklist_suffixes=cfg.get("blacklist_suffixes"),
            blacklist_bases=cfg.get("blacklist_bases"),
            blacklist_name_keywords=cfg.get("blacklist_name_keywords"),
        ):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            last = float(t.get("last") or 0)
            base_vol = float(t.get("baseVolume") or 0)
            qv = last * base_vol if last > 0 else 0.0
        qv = float(qv or 0)
        if qv < min_vol:
            continue
        pct = t.get("percentage")
        if pct is None:
            # Gate sometimes only has info
            info = t.get("info") or {}
            try:
                pct = float(info.get("change_percentage") or info.get("change") or 0)
            except (TypeError, ValueError):
                pct = 0.0
        try:
            pct_f = float(pct or 0)
        except (TypeError, ValueError):
            pct_f = 0.0
        last = float(t.get("last") or 0)
        out.append(
            {
                "symbol": sym,
                "pct_24h": round(pct_f, 3),
                "quote_volume": round(qv, 2),
                "last": last,
            }
        )
    out.sort(key=lambda r: (r["pct_24h"], r["quote_volume"]), reverse=True)
    top_n = int(cfg.get("live_top_n") or 50)
    for i, row in enumerate(out[: max(1, top_n)], 1):
        row["rank"] = i
    return out[: max(1, top_n)]


def liquid_symbols_by_volume(tickers: dict[str, dict], cfg: dict) -> list[str]:
    min_vol = float(cfg.get("daily_min_volume") or cfg.get("min_volume_usdt_24h") or 300_000)
    scan = int(cfg.get("universe_top_by_volume") or 250)
    rows: list[tuple[str, float]] = []
    for raw_sym, t in (tickers or {}).items():
        if not isinstance(t, dict):
            continue
        sym = normalize_symbol(raw_sym)
        if not passes_spot_usdt_filter(
            sym,
            blacklist_suffixes=cfg.get("blacklist_suffixes"),
            blacklist_bases=cfg.get("blacklist_bases"),
            blacklist_name_keywords=cfg.get("blacklist_name_keywords"),
        ):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            last = float(t.get("last") or 0)
            base_vol = float(t.get("baseVolume") or 0)
            qv = last * base_vol if last > 0 else 0.0
        qv = float(qv or 0)
        if qv < min_vol:
            continue
        rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[: max(1, scan)]]


def _day_returns_from_bars(bars: list) -> list[dict]:
    """Per-bar day_ret using prev close."""
    if not bars:
        return []
    bars = sorted(bars, key=lambda b: int(b[0]))
    out = []
    for i, b in enumerate(bars):
        ts = datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc)
        day = ts.date().isoformat()
        o, h, l, c, v = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
        if c <= 0:
            continue
        if i > 0 and float(bars[i - 1][4]) > 0:
            prev = float(bars[i - 1][4])
            ret = (c / prev - 1.0) * 100.0
        elif o > 0:
            ret = (c / o - 1.0) * 100.0
        else:
            continue
        out.append(
            {
                "day": day,
                "day_ret_pct": round(ret, 3),
                "close": c,
                "volume": v,
            }
        )
    return out


def build_daily_history(
    symbols: list[str],
    cfg: dict,
    *,
    days: int | None = None,
    fetch_ohlcv_fn: Callable | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict]]:
    """Map YYYY-MM-DD -> ranked list of {symbol, day_ret_pct, rank, volume}."""
    from historical_prices import _fetch_ohlcv_range

    now = now or _now()
    hist_days = int(days if days is not None else cfg.get("daily_history_days") or 10)
    start = now - timedelta(days=hist_days + 2)
    workers = int(cfg.get("scan_workers") or 8)
    fetch = fetch_ohlcv_fn or (
        lambda sym, s, e: _fetch_ohlcv_range(sym, s, e, timeframe="1d")
    )

    by_day: dict[str, list[dict]] = {}

    def one(sym: str) -> tuple[str, list[dict]]:
        try:
            bars = fetch(sym, start, now) or []
            return sym, _day_returns_from_bars(bars)
        except Exception:
            return sym, []

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym, rows = fut.result()
            for r in rows:
                by_day.setdefault(r["day"], []).append(
                    {
                        "symbol": sym,
                        "day_ret_pct": r["day_ret_pct"],
                        "volume": r.get("volume") or 0,
                    }
                )

    min_ret = float(cfg.get("min_day_ret_pct") or 3.0)
    top_max = int(cfg.get("daily_top_max") or 80)
    ranked: dict[str, list[dict]] = {}
    for day, rows in by_day.items():
        rows = [r for r in rows if float(r.get("day_ret_pct") or 0) >= min_ret]
        rows.sort(key=lambda r: float(r["day_ret_pct"]), reverse=True)
        slice_ = rows[: max(1, top_max)]
        for i, r in enumerate(slice_, 1):
            r["rank"] = i
        ranked[day] = slice_
    return ranked


def compute_streaks(
    daily_history: dict[str, list[dict]],
    cfg: dict,
) -> list[dict]:
    """Symbols appearing in top20 on >= streak_min_days within lookback."""
    lookback = int(cfg.get("streak_lookback_days") or 3)
    min_days = int(cfg.get("streak_min_days_in_top20") or 2)
    days = sorted(daily_history.keys())
    if not days:
        return []
    window = days[-lookback:]
    counts: dict[str, int] = {}
    for d in window:
        for r in (daily_history.get(d) or [])[:20]:
            sym = r.get("symbol")
            if sym:
                counts[sym] = counts.get(sym, 0) + 1
    out = [
        {"symbol": s, "days_in_top20": n, "lookback_days": lookback}
        for s, n in counts.items()
        if n >= min_days
    ]
    out.sort(key=lambda x: x["days_in_top20"], reverse=True)
    return out


def build_eligible(
    daily_history: dict[str, list[dict]],
    live_top: list[dict],
    streaks: list[dict],
    cfg: dict,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Prev-day tops (+ continuation) with TTL — no same-day oracle."""
    now = now or _now()
    ttl_h = float(cfg.get("prev_top_ttl_hours") or 36)
    eligible_until = (now + timedelta(hours=ttl_h)).isoformat()

    days = sorted(daily_history.keys())
    # last closed day: prefer yesterday if present
    today = now.date().isoformat()
    closed = [d for d in days if d < today]
    prev_day = closed[-1] if closed else (days[-1] if days else None)

    eligible: list[dict] = []
    seen: set[str] = set()

    if prev_day:
        for r in daily_history.get(prev_day) or []:
            sym = r.get("symbol")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            eligible.append(
                {
                    "symbol": sym,
                    "source": "gate_prev_top",
                    "rank": int(r.get("rank") or 0),
                    "day_ret": float(r.get("day_ret_pct") or 0),
                    "day": prev_day,
                    "eligible_until": eligible_until,
                }
            )

    if cfg.get("enable_continuation", True):
        live_map = {x["symbol"]: x for x in live_top}
        max_chase = float(cfg.get("continuation_max_chase_pct_today") or 15.0)
        for st in streaks:
            sym = st.get("symbol")
            if not sym or sym in seen:
                continue
            live = live_map.get(sym)
            pct = float((live or {}).get("pct_24h") or 0)
            if pct > max_chase:
                continue  # too late / chase
            # allow if still liquid enough in live or was on board
            seen.add(sym)
            eligible.append(
                {
                    "symbol": sym,
                    "source": "gainer_continuation",
                    "rank": 0,
                    "day_ret": pct,
                    "day": prev_day or today,
                    "eligible_until": eligible_until,
                    "streak_days": st.get("days_in_top20"),
                }
            )

    cap = int(cfg.get("expand_inject_max") or 40)
    # prefer higher day_ret / rank
    eligible.sort(
        key=lambda x: (
            0 if x.get("source") == "gate_prev_top" else 1,
            -float(x.get("day_ret") or 0),
            int(x.get("rank") or 999),
        )
    )
    return eligible[: max(1, cap)]


def run_scan(
    cfg: dict,
    *,
    prev_state: dict | None = None,
    fetch_tickers_fn: Callable | None = None,
    fetch_ohlcv_fn: Callable | None = None,
    now: datetime | None = None,
    force_daily: bool = False,
) -> dict[str, Any]:
    """Full scan snapshot for store. Fail-soft: keep prev on partial errors."""
    now = now or _now()
    prev = dict(prev_state or {})
    errors: list[str] = []

    tickers: dict = {}
    try:
        tickers = (fetch_tickers_fn or fetch_gate_tickers)() or {}
    except Exception as e:
        errors.append(f"tickers:{e}")
        log(f"gainer_universe tickers failed: {e}", "WARNING")

    live_top: list[dict] = []
    if tickers:
        try:
            live_top = filter_and_rank_live(tickers, cfg)
        except Exception as e:
            errors.append(f"live_rank:{e}")

    daily_history = dict(prev.get("daily_history") or {})
    need_daily = force_daily or not daily_history
    if not need_daily:
        last_d = prev.get("daily_scanned_at")
        try:
            if last_d:
                ts = datetime.fromisoformat(str(last_d).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (now - ts).total_seconds()
                need_daily = age >= float(cfg.get("daily_refresh_sec") or 900)
            else:
                need_daily = True
        except Exception:
            need_daily = True

    if need_daily and tickers:
        try:
            syms = liquid_symbols_by_volume(tickers, cfg)
            daily_history = build_daily_history(
                syms, cfg, fetch_ohlcv_fn=fetch_ohlcv_fn, now=now
            )
        except Exception as e:
            errors.append(f"daily:{e}")
            log(f"gainer_universe daily board failed: {e}", "WARNING")
            daily_history = dict(prev.get("daily_history") or {})

    streaks = compute_streaks(daily_history, cfg) if daily_history else []
    eligible = build_eligible(daily_history, live_top, streaks, cfg, now=now)

    return {
        "live_scanned_at": now.isoformat(),
        "daily_scanned_at": now.isoformat() if need_daily else prev.get("daily_scanned_at"),
        "live_top": live_top,
        "daily_history": daily_history,
        "streaks": streaks,
        "eligible": eligible,
        "last_error": "; ".join(errors) if errors else None,
        "prev_day": (
            sorted([d for d in daily_history.keys() if d < now.date().isoformat()])[-1]
            if daily_history
            else None
        ),
        "counts": {
            "live_top": len(live_top),
            "eligible": len(eligible),
            "streaks": len(streaks),
            "history_days": len(daily_history),
        },
    }
