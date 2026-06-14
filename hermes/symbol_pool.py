"""Dynamic Hermes symbol pool — pins, open positions, and CMC activity."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from core.config import BotConfig, get_bot_config
from data_manager import load_effective_watchlist, resolve_positions_file
from hermes.cmc_replay import coin_base, recent_signal_activity
from hermes.memory import store
from logger import log


def _position_symbols(scope: str = "live") -> list[str]:
    path = resolve_positions_file(scope)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"symbol_pool: failed to read positions ({path}): {e}", "WARNING")
        return []

    symbols = []
    for key, pos in (data.get("positions") or {}).items():
        if float(pos.get("amount") or 0) <= 0:
            continue
        parts = key.split("_")
        if len(parts) < 3:
            continue
        base = parts[0]
        symbols.append(f"{base}/USDT")
    return symbols


def _watchlist_symbols(config: BotConfig | None = None) -> list[str]:
    cfg = config or get_bot_config()
    symbols = []
    for coin in load_effective_watchlist():
        sym = coin.get("symbol", "")
        if sym and sym.endswith("/USDT"):
            symbols.append(sym)
    return symbols


def _top_cmc_symbols(
    candidates: list[str],
    top_n: int,
    hours: int,
) -> list[str]:
    if not candidates or top_n <= 0:
        return []
    activity = recent_signal_activity(candidates, hours=hours)
    ranked = sorted(candidates, key=lambda s: activity.get(s, 0), reverse=True)
    return [s for s in ranked if activity.get(s, 0) > 0][:top_n]


def _dedupe_ordered(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _pool_cache_fresh(hermes: dict, store_data: dict) -> bool:
    pool = store_data.get("active_pool") or {}
    updated = pool.get("updated_at")
    if not updated:
        return False
    try:
        ts = datetime.fromisoformat(updated)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    refresh_h = float(hermes.get("symbols_refresh_hours", 6))
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return age_h < refresh_h


def _filter_ohlcv(
    symbols: list[str],
    timeframe: str,
    ohlcv_check,
) -> list[str]:
    if not ohlcv_check:
        return symbols
    ok = []
    for symbol in symbols:
        try:
            if ohlcv_check(symbol, timeframe):
                ok.append(symbol)
            else:
                log(f"symbol_pool: skip {symbol} (no OHLCV)", "DEBUG")
        except Exception as e:
            log(f"symbol_pool: OHLCV check failed for {symbol}: {e}", "WARNING")
    return ok or symbols[:1]


def resolve_active_symbols(
    config: BotConfig | None = None,
    ohlcv_check=None,
    force_refresh: bool = False,
) -> list[str]:
    """Return ordered list of symbols Hermes may experiment on."""
    cfg = config or get_bot_config()
    hermes = cfg.hermes_config
    mode = hermes.get("symbols_mode", "static")

    if mode != "hybrid":
        return list(hermes.get("symbols", ["ARIA/USDT"]))

    store_data = store.load_baseline_store()
    if not force_refresh and _pool_cache_fresh(hermes, store_data):
        cached = (store_data.get("active_pool") or {}).get("symbols") or []
        if cached:
            return cached

    pins = list(hermes.get("symbols_pin") or hermes.get("symbols", ["ARIA/USDT"]))
    pool: list[str] = list(pins)

    if hermes.get("symbols_include_positions", True):
        pool.extend(_position_symbols("live"))

    watchlist = _watchlist_symbols(cfg)
    top_n = int(hermes.get("symbols_cmc_top_n", 4))
    hours = int(hermes.get("rotation_hours", 24))
    pool.extend(_top_cmc_symbols(watchlist, top_n, hours))

    pool = _dedupe_ordered(pool)
    max_symbols = int(hermes.get("symbols_max", 8))
    pool = pool[:max_symbols]

    timeframe = (hermes.get("timeframes") or ["4h"])[0]
    pool = _filter_ohlcv(pool, timeframe, ohlcv_check)

    store_data["active_pool"] = {
        "symbols": pool,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "pins": pins,
            "positions": hermes.get("symbols_include_positions", True),
            "cmc_top_n": top_n,
        },
    }
    store.save_baseline_store(store_data)
    return pool


def format_active_pool_line(config: BotConfig | None = None) -> str:
    cfg = config or get_bot_config()
    hermes = cfg.hermes_config
    if hermes.get("symbols_mode", "static") != "hybrid":
        static = ", ".join(hermes.get("symbols", []))
        return f"Pool (static): {static}"
    pool = resolve_active_symbols(cfg)
    return f"Pool (hybrid): {', '.join(pool)}"


def coin_in_pool(symbol: str, pool: list[str]) -> bool:
    base = coin_base(symbol)
    return any(coin_base(s) == base for s in pool)