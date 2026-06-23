"""Background social fetch + strategy backtest (Phase 4, hot-start safe)."""

from __future__ import annotations

import threading
import time

from data_manager import load_effective_watchlist
from logger import log

_lock = threading.Lock()
_pipeline = None
_running = False
_thread: threading.Thread | None = None
_last_fetch_at = 0.0
_last_accuracy: dict = {}
_fetch_in_progress = False


def register_pipeline(pipeline) -> None:
    global _pipeline
    with _lock:
        _pipeline = pipeline


def social_ever_fetched() -> bool:
    return _last_fetch_at > 0


def social_fetch_fresh(max_age_sec: float) -> bool:
    if _last_fetch_at <= 0:
        return False
    return (time.time() - _last_fetch_at) < max(1.0, float(max_age_sec))


def get_last_accuracy() -> dict:
    return dict(_last_accuracy)


def request_social_fetch(watchlist: list | None = None) -> bool:
    """Kick async social fetch if not already running."""
    if _fetch_in_progress or _pipeline is None:
        return False

    def _run():
        global _fetch_in_progress, _last_fetch_at, _last_accuracy
        _fetch_in_progress = True
        try:
            wl = watchlist or load_effective_watchlist()
            accuracy = _pipeline.run_cycle_fetches(wl)
            _last_accuracy = accuracy or {}
            _publish_snapshot(wl)
            _last_fetch_at = time.time()
        except Exception as e:
            log(f"Background social fetch failed: {e}", "WARNING")
        finally:
            _fetch_in_progress = False

    threading.Thread(target=_run, daemon=True, name="social-fetch-kick").start()
    return True


def run_social_cycle_sync(watchlist: list | None = None) -> dict:
    """Blocking social fetch (bootstrap / fallback)."""
    global _last_fetch_at, _last_accuracy
    if _pipeline is None:
        return {}
    wl = watchlist or load_effective_watchlist()
    accuracy = _pipeline.run_cycle_fetches(wl)
    _last_accuracy = accuracy or {}
    _publish_snapshot(wl)
    _last_fetch_at = time.time()
    return _last_accuracy


def _publish_snapshot(watchlist: list):
    if _pipeline is None:
        return
    try:
        from bus.publisher import publish_signal_snapshot
        from bus.signals import signal_snapshot_store
        from core.config import get_bot_config

        symbols = [c["symbol"] for c in watchlist if c.get("active", True)]
        x_sig = _pipeline.refresh_signals()
        cmc_sig = _pipeline.refresh_cmc_signals()
        lc_sig = _pipeline.refresh_lc_signals()
        snap = signal_snapshot_store.publish_objects(
            x_signals=x_sig,
            cmc_signals=cmc_sig,
            lc_signals=lc_sig,
            watchlist_symbols=symbols,
            accuracy=get_last_accuracy(),
        )
        arch = get_bot_config().architecture_config
        publish_signal_snapshot(snap, key_prefix=arch.get("key_prefix", "aria:"), redis_url=arch.get("redis_url"))
    except Exception as e:
        log(f"Background snapshot publish failed: {e}", "WARNING")


def _loop():
    global _last_fetch_at, _last_accuracy
    while _running:
        try:
            from core.config import get_bot_config

            cfg = get_bot_config()
            cfg.refresh()
            arch = cfg.architecture_config
            if not arch.get("background_social_enabled", True):
                time.sleep(5)
                continue
            if _pipeline is None:
                time.sleep(2)
                continue

            interval = int(
                arch.get("background_social_interval_sec")
                or cfg.raw.get("update_interval", 240)
            )
            wl = load_effective_watchlist()
            if not _fetch_in_progress:
                try:
                    from bus.heartbeats import heartbeat_registry

                    heartbeat_registry.beat(
                        "background_social",
                        ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                        key_prefix=arch.get("key_prefix", "aria:"),
                    )
                except Exception:
                    pass
                accuracy = _pipeline.run_cycle_fetches(wl)
                _last_accuracy = accuracy or {}
                _publish_snapshot(wl)
                _last_fetch_at = time.time()

            if arch.get("background_backtest_enabled", True):
                try:
                    from services.strategy_backtest_worker import tick_strategy_backtest

                    tick_strategy_backtest()
                    from bus.heartbeats import heartbeat_registry

                    heartbeat_registry.beat(
                        "strategy_backtest",
                        ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                        key_prefix=arch.get("key_prefix", "aria:"),
                    )
                except Exception as e:
                    log(f"Background strategy backtest failed: {e}", "WARNING")

            time.sleep(max(30, interval))
        except Exception as e:
            log(f"Background runtime loop error: {e}", "ERROR")
            time.sleep(10)


def ensure_started():
    global _running, _thread
    with _lock:
        if _running:
            return
        _running = True
        _thread = threading.Thread(target=_loop, daemon=True, name="background-runtime")
        _thread.start()
        log("Background runtime started (social + backtest)", "INFO")