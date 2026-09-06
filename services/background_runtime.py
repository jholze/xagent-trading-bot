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
_stop = threading.Event()
_last_fetch_at = 0.0
_last_accuracy: dict = {}
_fetch_in_progress = False
_last_news_pulse_at = 0.0


def register_pipeline(pipeline) -> None:
    global _pipeline
    with _lock:
        _pipeline = pipeline


def _ensure_trending_watchlist() -> None:
    """Refresh CMC trending overlay before social fetch (fallback when no watchlist passed)."""
    try:
        from core.config import get_bot_config
        from data_manager import prune_non_gate_watchlist_sources
        from services.dry_run_watchlist import sync_trending_watchlist_once

        prune_non_gate_watchlist_sources()
        sync_trending_watchlist_once(get_bot_config())
    except Exception as e:
        log(f"Trending watchlist sync failed: {e}", "WARNING")


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
            if watchlist is None:
                _ensure_trending_watchlist()
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
    if watchlist is None:
        _ensure_trending_watchlist()
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


def _maybe_tick_news_pulse(cfg=None) -> None:
    """Self-gated news ingest + market-pulse cache refresh.

    No-op unless sell_policy.correlated_tier.news_pulse_enabled is true.
    Fail-open: any error is swallowed so the rest of the background loop
    keeps running and callers never see a fabricated pulse.
    """
    global _last_news_pulse_at
    try:
        raw = None
        if cfg is not None:
            raw = getattr(cfg, "raw", None)
            if raw is None and isinstance(cfg, dict):
                raw = cfg
        from services.correlated_tier.config import correlated_tier_config

        ct = correlated_tier_config(raw if isinstance(raw, dict) else None)
        if not ct.get("news_pulse_enabled"):
            return
        interval = float(ct.get("news_pulse_poll_interval_sec") or 900)
        now = time.time()
        if _last_news_pulse_at > 0 and (now - _last_news_pulse_at) < interval:
            return
        try:
            from intelligence.memory.news_providers import poll_and_ingest_news

            poll_and_ingest_news(config=raw if isinstance(raw, dict) else None)
        except Exception as e:
            log(f"Background news ingest skipped: {e}", "DEBUG")
        try:
            from intelligence.memory.market_pulse import (
                market_pulse_score,
                set_cached_market_pulse,
            )

            since = int(ct.get("news_pulse_since_minutes") or 30)
            result = market_pulse_score(
                since_minutes=since,
                config_raw=raw if isinstance(raw, dict) else None,
            )
            set_cached_market_pulse(result)
        except Exception as e:
            log(f"Background market pulse score skipped: {e}", "DEBUG")
        _last_news_pulse_at = now
    except Exception as e:
        log(f"Background news pulse failed: {e}", "WARNING")


def reset_background_runtime_for_tests() -> None:
    """Stop the background-runtime daemon so it cannot outlive a pytest test (#329)."""
    global _running, _thread
    _stop.set()
    _running = False
    thread = _thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    _thread = None
    _stop.clear()


def _loop():
    global _last_fetch_at, _last_accuracy
    while _running and not _stop.is_set():
        try:
            from core.config import get_bot_config

            cfg = get_bot_config()
            cfg.refresh()
            try:
                _maybe_tick_news_pulse(cfg)
            except Exception as e:
                log(f"Background news pulse failed: {e}", "WARNING")
            arch = cfg.architecture_config
            if not arch.get("background_social_enabled", True):
                _stop.wait(5)
                continue
            if _pipeline is None:
                _stop.wait(2)
                continue

            interval = int(
                arch.get("background_social_interval_sec")
                or cfg.raw.get("update_interval", 240)
            )
            from core.tenant_context import multi_tenant_enabled

            if multi_tenant_enabled():
                from services.cycle_shared import union_tenant_watchlists

                wl = union_tenant_watchlists()
            else:
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

            # WQE-R5: periodic det+memory rescore (no LLM on hot path)
            try:
                from services.watchlist_quality.config import wqe_shadow_active
                from services.watchlist_quality.engine import maybe_run_shadow_after_watchlist_load

                raw = cfg.raw
                if wqe_shadow_active(raw):
                    wq2 = dict(raw.get("watchlist_quality") or {})
                    ai = dict(wq2.get("ai") or {})
                    ai["enabled"] = bool(ai.get("background_ai", False))
                    wq2["ai"] = ai
                    raw2 = dict(raw)
                    raw2["watchlist_quality"] = wq2
                    try:
                        coins = wl  # from cycle above when social enabled
                    except NameError:
                        coins = load_effective_watchlist()
                    maybe_run_shadow_after_watchlist_load(coins, config=raw2)
            except Exception as e:
                log(f"Background WQE rescore skipped: {e}", "DEBUG")

            _stop.wait(max(30, interval))
        except Exception as e:
            log(f"Background runtime loop error: {e}", "ERROR")
            _stop.wait(10)


def ensure_started():
    global _running, _thread
    with _lock:
        if _running:
            return
        _stop.clear()
        _running = True
        _thread = threading.Thread(target=_loop, daemon=True, name="background-runtime")
        _thread.start()
        log("Background runtime started (social + backtest)", "INFO")