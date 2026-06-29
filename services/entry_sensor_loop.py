"""Fast 15m poll loop — volume sensor only, same position/risk path as 4h/1h."""

from __future__ import annotations

import threading
import time

from core.config import get_bot_config
from data_manager import load_effective_watchlist
from logger import log
from price_fetcher import get_prices_batch
from strategies.entry_sensor_15m import evaluate_entry_sensor_15m, set_pending_sensor_result
from strategies import watch_15m_state

_loop_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _coin_by_symbol(symbol: str) -> dict | None:
    for coin in load_effective_watchlist():
        if coin.get("symbol") == symbol and coin.get("active", True):
            return coin
    return None


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

    for entry in watched:
        symbol = entry["symbol"]
        coin = _coin_by_symbol(symbol)
        if not coin:
            continue

        price = float(prices.get(symbol) or 0)
        if price <= 0:
            continue

        market_svc = orchestrator.market
        metrics = market_svc.fetch_15m_sensor_metrics(symbol, cfg)
        indicators = market_svc.fetch_indicators(symbol, entry.get("timeframe", "4h"), price)
        rsi_4h = float(indicators.get("rsi", 45))

        result = evaluate_entry_sensor_15m(
            watched=True,
            metrics=metrics,
            cfg=cfg,
            rsi_4h=rsi_4h,
            hours_since_reject=watch_15m_state.hours_since_sensor_reject(symbol),
            tech_already_buy=False,
        )

        if not result.triggered:
            continue

        set_pending_sensor_result(symbol, result)
        mode = str(cfg.get("mode", "shadow")).strip().lower()

        if mode == "active":
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