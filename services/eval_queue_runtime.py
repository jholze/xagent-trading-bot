"""Redis eval queue worker and meta-cycle producers."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from bus.eval_queue import (
    PRIORITY_ENTRY_15M,
    PRIORITY_POSITION_HEARTBEAT,
    PRIORITY_SOCIAL,
    PRIORITY_STALE,
    PRIORITY_WEBHOOK,
    EvalJob,
    enqueue_eval,
    eval_queue_config,
    eval_queue_enabled,
    last_processed_at,
    mark_eval_processed,
    pop_eval_batch,
    queue_depth,
)
from core.config import get_bot_config
from data_manager import load_effective_watchlist
from logger import log
from price_fetcher import get_prices_batch
from strategies import watch_15m_state
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    passes_vol_spike_prefilter,
    set_pending_sensor_metrics,
)
from strategies.positions import get_position, is_open_position

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_orchestrator = None
_recent_results: deque[dict] = deque(maxlen=300)
_stats_lock = threading.Lock()
_stats: dict[str, Any] = {
    "jobs_processed": 0,
    "jobs_failed": 0,
    "last_job_at": None,
    "last_job_symbol": None,
    "last_job_reason": None,
    "worker_started_at": None,
}
_latest_signals: dict[str, list] = {
    "x": [],
    "cmc": [],
    "lc": [],
}
_last_meta_seed_at: dict[str, float] = {}


def _coin_for_symbol(symbol: str, timeframe: str) -> dict:
    # Prefer WQE sensor universe when soft/enforce (W5); fail-open to full list
    try:
        from services.watchlist_quality.config import wqe_mode
        from services.watchlist_quality.universe import sensor_universe
        from core.config import get_bot_config

        cfg = get_bot_config().raw
        if wqe_mode(cfg) in ("soft", "enforce"):
            for coin in sensor_universe(load_effective_watchlist(), config=cfg):
                if coin.get("symbol") == symbol and coin.get("active", True):
                    return coin
    except Exception:
        pass
    for coin in load_effective_watchlist():
        if coin.get("symbol") == symbol and coin.get("active", True):
            return coin
    return {"symbol": symbol, "timeframe": timeframe, "active": True}


def update_signal_snapshot(
    x_signals: list | None = None,
    cmc_signals: list | None = None,
    lc_signals: list | None = None,
) -> None:
    if x_signals is not None:
        _latest_signals["x"] = x_signals
    if cmc_signals is not None:
        _latest_signals["cmc"] = cmc_signals
    if lc_signals is not None:
        _latest_signals["lc"] = lc_signals


def _record_result(result: dict | None, job: EvalJob) -> None:
    if not result:
        return
    payload = {
        **result,
        "tenant_id": job.tenant_id,
        "eval_reason": job.reason,
        "eval_priority": job.priority,
        "processed_at": time.time(),
    }
    _recent_results.append(payload)


def _bump_stats(*, symbol: str, reason: str, failed: bool = False) -> None:
    with _stats_lock:
        if failed:
            _stats["jobs_failed"] += 1
        else:
            _stats["jobs_processed"] += 1
        _stats["last_job_at"] = time.time()
        _stats["last_job_symbol"] = symbol
        _stats["last_job_reason"] = reason


def _process_entry_15m_job(orchestrator, job: EvalJob, price: float, coin: dict) -> dict | None:
    from core.actions import is_buy

    cfg = get_bot_config().entry_sensor_15m_config
    if _has_open_position(job.symbol, coin):
        watch_15m_state.clear_watch(job.symbol)
        return {"action": "HOLD", "symbol": job.symbol, "normalized_action": "HOLD"}

    from strategies.entry_sensor_15m import consume_pending_sensor_metrics

    market_svc = orchestrator.market
    vol_avg_period = int(cfg.get("vol_avg_period", 20))
    ema_period = int(cfg.get("ema_period", 9))
    ohlcv_limit = vol_avg_period + 30
    metrics = consume_pending_sensor_metrics(job.symbol, tenant_id=job.tenant_id)
    if not metrics:
        df = market_svc.fetch_ohlcv(job.symbol, "15m", ohlcv_limit)
        metrics = market_svc.compute_15m_sensor_metrics(
            df,
            ema_period=ema_period,
            vol_avg_period=vol_avg_period,
        )
    if not passes_vol_spike_prefilter(metrics, cfg):
        return {"action": "HOLD", "symbol": job.symbol, "normalized_action": "HOLD"}

    cooldown_h = float(cfg.get("cooldown_after_reject_hours", 2))
    hours_since = watch_15m_state.hours_since_sensor_reject(
        job.symbol, tenant_id=job.tenant_id,
    )
    if hours_since is not None and hours_since < cooldown_h:
        return {"action": "HOLD", "symbol": job.symbol, "normalized_action": "HOLD"}

    mode = str(cfg.get("mode", "shadow")).strip().lower()
    if mode != "active":
        return {"action": "HOLD", "symbol": job.symbol, "normalized_action": "HOLD"}

    # Shared 15m watch is global — operator buy/clear must not block satellites.
    if not watch_15m_state.is_watched(job.symbol):
        watch_15m_state.set_watch(
            job.symbol,
            job.timeframe or str(coin.get("timeframe") or "4h"),
            reason=f"entry_job:{job.tenant_id or 'default'}",
            ttl_hours=float(cfg.get("watch_ttl_hours", 24)),
        )

    set_pending_sensor_metrics(job.symbol, metrics, tenant_id=job.tenant_id)
    outcome = orchestrator.process_entry_sensor(
        coin, price, sensor_metrics=metrics, quiet=True,
    )
    executed = bool(outcome.get("executed"))
    sources = outcome.get("sources") or []
    if executed and ENTRY_SENSOR_SOURCE in sources and is_buy(outcome.get("action", "")):
        from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled

        # Multi-tenant: keep shared watch so other tenants still get the spike.
        if not multi_tenant_enabled() and (
            not job.tenant_id or job.tenant_id == DEFAULT_TENANT
        ):
            watch_15m_state.clear_watch(job.symbol)
    elif ENTRY_SENSOR_SOURCE in sources and not executed:
        watch_15m_state.record_sensor_reject(job.symbol, tenant_id=job.tenant_id)
    return outcome


def _has_open_position(symbol: str, coin: dict) -> bool:
    tf = str(coin.get("timeframe") or "4h")
    return is_open_position(get_position(symbol, tf))


def process_eval_job(orchestrator, job: EvalJob) -> dict | None:
    """Run one queued evaluation (serial — called only from worker thread)."""
    from core.tenant_routing import tenant_cycle_context

    with tenant_cycle_context(job.tenant_id):
        try:
            orchestrator.begin_tenant_cycle()
        except Exception as exc:
            log(f"eval_worker tenant init failed ({job.tenant_id}): {exc}", "WARNING")
        coin = _coin_for_symbol(job.symbol, job.timeframe)
        price = float(get_prices_batch([job.symbol]).get(job.symbol, 0) or 0)
        if price <= 0:
            return None

        reason = job.reason or ""
        if reason.startswith("entry_15m") or reason == "15m_watch":
            return _process_entry_15m_job(orchestrator, job, price, coin)

        return orchestrator.process_coin(
            coin,
            price,
            _latest_signals.get("x") or [],
            _latest_signals.get("cmc") or [],
            _latest_signals.get("lc") or [],
            quiet=True,
        )


def _worker_loop(orchestrator) -> None:
    from bus.heartbeats import heartbeat_registry

    cfg_raw = get_bot_config().raw
    arch = get_bot_config().architecture_config
    prefix = arch.get("key_prefix", "aria:")
    ttl = int(arch.get("heartbeat_ttl_sec", 120))

    while not _stop_event.is_set():
        try:
            get_bot_config().refresh()
            cfg_raw = get_bot_config().raw
            if not eval_queue_enabled(cfg_raw):
                _stop_event.wait(5.0)
                continue

            eq_cfg = eval_queue_config(cfg_raw)
            batch_size = int(eq_cfg.get("eval_batch_size", 3))
            poll_sec = float(eq_cfg.get("eval_worker_poll_sec", 2.0))

            heartbeat_registry.beat("eval_worker", ttl_sec=ttl, key_prefix=prefix)
            jobs = pop_eval_batch(batch_size, config_raw=cfg_raw)
            if not jobs:
                _stop_event.wait(poll_sec)
                continue

            for job in jobs:
                if _stop_event.is_set():
                    break
                try:
                    from core.interactive_priority import yield_to_interactive

                    yield_to_interactive()
                except Exception:
                    pass
                try:
                    result = process_eval_job(orchestrator, job)
                    mark_eval_processed(
                        job.symbol,
                        job.timeframe,
                        config_raw=cfg_raw,
                        tenant_id=job.tenant_id or None,
                    )
                    _record_result(result, job)
                    _bump_stats(symbol=job.symbol, reason=job.reason)
                    log(
                        f"eval_worker tenant={job.tenant_id or 'default'} "
                        f"{job.symbol} ({job.reason} p={job.priority}) "
                        f"→ {result.get('action') if result else 'skip'}"
                        f"{' exec' if result and result.get('executed') else ''}",
                        "DEBUG",
                    )
                except Exception as e:
                    _bump_stats(symbol=job.symbol, reason=job.reason, failed=True)
                    log(f"eval_worker failed {job.symbol}: {e}", "ERROR")
        except Exception as e:
            log(f"eval_worker loop error: {e}", "ERROR")
            _stop_event.wait(5.0)


def seed_meta_producers(
    *,
    watchlist: list[dict],
    open_positions: list[dict],
    x_signals: list | None = None,
    cmc_signals: list | None = None,
    lc_signals: list | None = None,
    config_raw: dict | None = None,
) -> dict[str, int]:
    """Enqueue heartbeat/stale/social/15m-watch jobs from the meta cycle."""
    global _last_meta_seed_at

    from core.tenant_context import resolve_tenant_id

    tenant_id = resolve_tenant_id()
    cfg_raw = config_raw or get_bot_config().raw
    if not eval_queue_enabled(cfg_raw):
        return {}

    update_signal_snapshot(x_signals, cmc_signals, lc_signals)
    eq_cfg = eval_queue_config(cfg_raw)
    now = time.time()
    meta_interval = float(eq_cfg.get("eval_meta_interval_sec", 300))
    last_seed = _last_meta_seed_at.get(tenant_id, 0.0)
    if now - last_seed < meta_interval:
        return {"skipped": 1, "tenant_id": tenant_id}
    _last_meta_seed_at[tenant_id] = now

    heartbeat_sec = float(eq_cfg.get("eval_position_heartbeat_sec", 300))
    stale_sec = float(eq_cfg.get("eval_stale_sec", 7200))
    counts = {"positions": 0, "stale": 0, "social": 0, "watches": 0}

    for pos in open_positions:
        symbol = pos.get("symbol")
        tf = str(pos.get("timeframe") or "4h")
        if not symbol:
            continue
        last = last_processed_at(symbol, tf, config_raw=cfg_raw, tenant_id=tenant_id)
        if last and (now - last) < heartbeat_sec:
            continue
        if enqueue_eval(
            symbol, tf,
            reason="position_heartbeat",
            priority=PRIORITY_POSITION_HEARTBEAT,
            config_raw=cfg_raw,
            tenant_id=tenant_id,
        ):
            counts["positions"] += 1

    watch_by_symbol = {c.get("symbol"): c for c in watchlist if c.get("active", True)}
    for symbol, coin in watch_by_symbol.items():
        tf = str(coin.get("timeframe") or "4h")
        last = last_processed_at(symbol, tf, config_raw=cfg_raw, tenant_id=tenant_id)
        if last and (now - last) < stale_sec:
            continue
        if enqueue_eval(
            symbol, tf,
            reason="stale_watchlist",
            priority=PRIORITY_STALE,
            config_raw=cfg_raw,
            tenant_id=tenant_id,
        ):
            counts["stale"] += 1

    for entry in watch_15m_state.list_watched():
        symbol = entry.get("symbol")
        if not symbol:
            continue
        tf = str(entry.get("timeframe") or _coin_for_symbol(symbol, "4h").get("timeframe") or "4h")
        if enqueue_eval(
            symbol, tf,
            reason="15m_watch",
            priority=PRIORITY_ENTRY_15M,
            config_raw=cfg_raw,
            tenant_id=tenant_id,
        ):
            counts["watches"] += 1

    for signal in x_signals or []:
        eff = getattr(signal, "effective_confidence", getattr(signal, "confidence", 0))
        if eff < 70:
            continue
        coin = getattr(signal, "coin", None)
        if not coin:
            continue
        sym = coin if "/" in str(coin) else f"{coin}/USDT"
        if sym not in watch_by_symbol:
            continue
        tf = str(watch_by_symbol[sym].get("timeframe") or "4h")
        if enqueue_eval(
            sym, tf,
            reason="social_x",
            priority=PRIORITY_SOCIAL,
            config_raw=cfg_raw,
            tenant_id=tenant_id,
        ):
            counts["social"] += 1

    for signal in cmc_signals or []:
        if signal.confidence < 60:
            continue
        sym = signal.coin if "/" in str(signal.coin) else f"{signal.coin}/USDT"
        if sym not in watch_by_symbol:
            continue
        tf = str(watch_by_symbol[sym].get("timeframe") or "4h")
        if enqueue_eval(
            sym, tf,
            reason="social_cmc",
            priority=PRIORITY_SOCIAL,
            config_raw=cfg_raw,
            tenant_id=tenant_id,
        ):
            counts["social"] += 1

    for signal in lc_signals or []:
        if signal.confidence < 55:
            continue
        sym = signal.coin if "/" in str(signal.coin) else f"{signal.coin}/USDT"
        if sym not in watch_by_symbol:
            continue
        tf = str(watch_by_symbol[sym].get("timeframe") or "4h")
        if enqueue_eval(
            sym, tf,
            reason="social_lc",
            priority=PRIORITY_SOCIAL,
            config_raw=cfg_raw,
            tenant_id=tenant_id,
        ):
            counts["social"] += 1

    if any(counts.values()):
        log(
            f"eval_meta seeded tenant={tenant_id}: pos={counts['positions']} stale={counts['stale']} "
            f"watches={counts['watches']} social={counts['social']}",
            "INFO",
        )
    return counts


def enqueue_eval_for_watchers(
    symbol: str,
    timeframe: str,
    *,
    reason: str,
    priority: int,
    config_raw: dict | None = None,
    force: bool = False,
    metrics: dict | None = None,
) -> int:
    """Enqueue for every active cycle tenant (same entry/webhook opportunity)."""
    from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled
    from core.tenant_routing import iter_price_cycle_tenants, tenant_cycle_context
    from strategies.entry_sensor_15m import set_pending_sensor_metrics

    tenants = (
        list(iter_price_cycle_tenants())
        if multi_tenant_enabled()
        else [DEFAULT_TENANT]
    )
    accepted = 0
    for tid in tenants:
        with tenant_cycle_context(tid):
            if metrics is not None:
                set_pending_sensor_metrics(symbol, metrics, tenant_id=tid)
            if enqueue_eval(
                symbol, timeframe, reason=reason, priority=priority,
                config_raw=config_raw, force=force, tenant_id=tid,
            ):
                accepted += 1
    return accepted


def enqueue_webhook_eval(symbol: str, timeframe: str, *, config_raw: dict | None = None) -> int:
    return enqueue_eval_for_watchers(
        symbol,
        timeframe,
        reason="webhook",
        priority=PRIORITY_WEBHOOK,
        config_raw=config_raw,
        force=True,
    )


def enqueue_entry_15m_eval(
    symbol: str,
    timeframe: str,
    *,
    config_raw: dict | None = None,
    metrics: dict | None = None,
) -> int:
    return enqueue_eval_for_watchers(
        symbol,
        timeframe,
        reason="entry_15m_vol_spike",
        priority=PRIORITY_ENTRY_15M,
        config_raw=config_raw,
        metrics=metrics,
    )


def get_recent_coin_results(
    since: float | None = None,
    *,
    tenant_id: str | None = None,
) -> list[dict]:
    from core.tenant_context import resolve_tenant_id

    tid = resolve_tenant_id(tenant_id)
    rows = list(_recent_results)
    if tid:
        rows = [r for r in rows if str(r.get("tenant_id") or "default") == tid]
    if since is None:
        return rows
    return [r for r in rows if float(r.get("processed_at", 0)) >= since]


def worker_stats() -> dict[str, Any]:
    with _stats_lock:
        out = dict(_stats)
    out["queue_depth"] = queue_depth()
    out["running"] = _worker_thread is not None and _worker_thread.is_alive()
    return out


def ensure_started(orchestrator) -> threading.Thread | None:
    """Start eval worker thread; idempotent."""
    global _worker_thread, _orchestrator

    _orchestrator = orchestrator
    cfg_raw = get_bot_config().raw
    if not eval_queue_enabled(cfg_raw):
        return None
    if _worker_thread is not None and _worker_thread.is_alive():
        return _worker_thread

    _stop_event.clear()
    with _stats_lock:
        _stats["worker_started_at"] = time.time()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        args=(orchestrator,),
        daemon=True,
        name="eval-queue-worker",
    )
    _worker_thread.start()
    log("Redis eval queue worker started", "INFO")
    return _worker_thread


def stop_eval_worker() -> None:
    _stop_event.set()


def reset_eval_runtime_for_tests() -> None:
    global _worker_thread, _orchestrator, _last_meta_seed_at
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=2.0)
    _worker_thread = None
    _orchestrator = None
    _recent_results.clear()
    _last_meta_seed_at = {}
    _latest_signals.update({"x": [], "cmc": [], "lc": []})
    with _stats_lock:
        _stats.update({
            "jobs_processed": 0,
            "jobs_failed": 0,
            "last_job_at": None,
            "last_job_symbol": None,
            "last_job_reason": None,
            "worker_started_at": None,
        })
    _stop_event.clear()