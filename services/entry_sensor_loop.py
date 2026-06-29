"""Fast 15m poll loop — volume sensor only, same position/risk path as 4h/1h."""

from __future__ import annotations

import threading
import time

from core.config import get_bot_config
from data_manager import load_effective_watchlist
from logger import log
from price_fetcher import get_prices_batch
from strategies.entry_sensor_15m import evaluate_entry_sensor_15m, set_pending_sensor_metrics
from strategies import watch_15m_state

_loop_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_poll_at: dict[str, float] = {}


def _coin_by_symbol(symbol: str) -> dict | None:
    for coin in load_effective_watchlist():
        if coin.get("symbol") == symbol and coin.get("active", True):
            return coin
    return None


def _min_poll_gap_sec(cfg: dict) -> float:
    return float(cfg.get("min_poll_gap_sec_per_coin", cfg.get("poll_interval_sec", 20)))


def _should_poll_symbol(symbol: str, cfg: dict, now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    gap = _min_poll_gap_sec(cfg)
    last = _last_poll_at.get(symbol, 0.0)
    if now - last < gap:
        return False
    _last_poll_at[symbol] = now
    return True


def reset_poll_state_for_tests() -> None:
    _last_poll_at.clear()


def _poll_once(orchestrator) -> None:
    cfg = get_bot_config().entry_sensor_15m_config
    if not cfg.get("enabled", True):
        return

    watch_15m_state.prune_ttl()
    watched = watch_15m_state.list_watched()
    if not watched:
        return

    symbols = [w["symbol"] for w in watched]
    prices = get_prices_batch(symbols)
    market_svc = orchestrator.market
    vol_avg_period = int(cfg.get("vol_avg_period", 20))
    ema_period = int(cfg.get("ema_period", 9))
    ohlcv_limit = vol_avg_period + 30
    poll_now = time.monotonic()

    for entry in watched:
        symbol = entry["symbol"]
        if not _should_poll_symbol(symbol, cfg, poll_now):
            continue

        coin = _coin_by_symbol(symbol)
        if not coin:
            continue

        price = float(prices.get(symbol) or 0)
        if price <= 0:
            continue

        df = market_svc.fetch_ohlcv(symbol, "15m", ohlcv_limit)
        metrics = market_svc.compute_15m_sensor_metrics(
            df,
            ema_period=ema_period,
            vol_avg_period=vol_avg_period,
        )
        rsi_4h = float(entry.get("rsi_4h") or 45)
        tech_already_buy = bool(entry.get("tech_buy", False))

        result = evaluate_entry_sensor_15m(
            watched=True,
            metrics=metrics,
            cfg=cfg,
            rsi_4h=rsi_4h,
            hours_since_reject=watch_15m_state.hours_since_sensor_reject(symbol),
            tech_already_buy=tech_already_buy,
        )

        if not result.triggered:
            continue

        mode = str(cfg.get("mode", "shadow")).strip().lower()

        if mode == "active":
            set_pending_sensor_metrics(symbol, metrics)
            try:
                orchestrator.process_coin(coin, price, quiet=True)
                log(f"15m sensor active buy path for {symbol}: {result.rationale}", "INFO")
            except Exception as e:
                log(f"15m sensor execute failed for {symbol}: {e}", "ERROR")
                watch_15m_state.record_sensor_reject(symbol)
        else:
            log(
                f"15m sensor shadow {symbol}: {result.rationale} ({result.action})",
                "INFO",
            )


def _loop_main(orchestrator) -> None:
    while not _stop_event.is_set():
        try:
            get_bot_config().refresh()
            _poll_once(orchestrator)
        except Exception as e:
            log(f"Entry sensor loop error: {e}", "ERROR")
        interval = float(get_bot_config().entry_sensor_15m_config.get("poll_interval_sec", 20))
        _stop_event.wait(max(5.0, interval))


def start_entry_sensor_loop(orchestrator) -> threading.Thread | None:
    """Start daemon thread; idempotent."""
    global _loop_thread
    cfg = get_bot_config().entry_sensor_15m_config
    if not cfg.get("enabled", True):
        return None
    if _loop_thread is not None and _loop_thread.is_alive():
        return _loop_thread

    _stop_event.clear()
    _loop_thread = threading.Thread(
        target=_loop_main,
        args=(orchestrator,),
        daemon=True,
        name="entry-sensor-15m",
    )
    _loop_thread.start()
    log(
        f"15m entry sensor loop started (mode={cfg.get('mode', 'shadow')}, "
        f"interval={cfg.get('poll_interval_sec', 20)}s)",
        "INFO",
    )
    return _loop_thread


def stop_entry_sensor_loop() -> None:
    _stop_event.set()