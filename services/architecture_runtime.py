"""Hot-start architecture services without bot restart (idempotent)."""

from __future__ import annotations

import threading
import time

from logger import log

_lock = threading.Lock()
_started = False
_last_mode: str | None = None
_last_stale_warn_at = 0.0


def ensure_started(force_refresh: bool = False):
    """Start notification worker + heartbeats on first call (safe from price_loop)."""
    global _started, _last_mode
    from core.config import get_bot_config

    cfg = get_bot_config()
    arch = cfg.architecture_config
    mode = arch.get("notification_mode", "async")

    with _lock:
        if _started and not force_refresh and mode == _last_mode:
            _heartbeat_tick(cfg)
            _maybe_warn_stale(cfg)
            return

        from bus.jobs import heavy_job_queue
        from services.background_runtime import ensure_started as ensure_background
        from services.trading_engine_runtime import ensure_started as ensure_trading_engine

        if not heavy_job_queue.running:
            heavy_job_queue.start()
        ensure_background()
        ensure_trading_engine()
        _ensure_eval_worker()

        if mode == "direct":
            _started = True
            _last_mode = mode
            log("Architecture runtime: notification_mode=direct (sync)", "INFO")
            _heartbeat_tick(cfg)
            return

        from bus.notifications import notification_publisher
        from telegram_notifier import _send_telegram_direct

        rate = float(arch.get("notification_rate_limit_sec", 1.0))
        notification_publisher._rate_limit_sec = rate
        if not notification_publisher.running:
            notification_publisher.start(_send_telegram_direct)

        _started = True
        _last_mode = mode
        _heartbeat_tick(cfg)
        log("Architecture runtime: async notification worker active", "INFO")


def _ensure_eval_worker():
    try:
        from bus.eval_queue import eval_queue_enabled
        if not eval_queue_enabled():
            return
        from services.eval_queue_runtime import _orchestrator, ensure_started

        if _orchestrator is not None:
            ensure_started(_orchestrator)
    except Exception as e:
        log(f"Eval worker ensure failed: {e}", "WARNING")


def register_eval_orchestrator(orchestrator) -> None:
    """Bind orchestrator for eval worker (call once at bot startup)."""
    try:
        from bus.eval_queue import eval_queue_enabled
        from services.eval_queue_runtime import ensure_started

        if eval_queue_enabled():
            ensure_started(orchestrator)
    except Exception as e:
        log(f"Eval worker register failed: {e}", "WARNING")


def _heartbeat_tick(cfg):
    from bus.heartbeats import heartbeat_registry

    arch = cfg.architecture_config
    ttl = int(arch.get("heartbeat_ttl_sec", 120))
    prefix = arch.get("key_prefix", "aria:")
    heartbeat_registry.beat(
        "monolith",
        meta={"notification_mode": arch.get("notification_mode", "async")},
        ttl_sec=ttl,
        key_prefix=prefix,
    )
    for worker in (
        "price_loop",
        "eval_worker",
        "ask_bridge",
        "webhook_watchdog",
        "heavy_job_worker",
        "notification_worker",
        "background_social",
        "strategy_backtest",
        "trading_engine",
    ):
        heartbeat_registry.beat(worker, ttl_sec=ttl, key_prefix=prefix)
    if hermes_runs_in_process(cfg):
        heartbeat_registry.beat("hermes", ttl_sec=ttl, key_prefix=prefix)
    else:
        # External Hermes must not linger as a zombie local entry → permanent stale spam
        heartbeat_registry.drop("hermes")


def _external_hermes_stale(arch: dict) -> bool:
    """True when external Hermes Redis health key is missing/too old.

    Fail-open if Redis unreachable (no spam when Redis is down).
    """
    from bus.heartbeats import heartbeat_registry

    prefix = str(arch.get("key_prefix") or "aria:")
    # Hermes cycles every HERMES_INTERVAL_SEC (default 1800); allow 2.5× margin
    try:
        import os

        interval = max(120, int(os.environ.get("HERMES_INTERVAL_SEC", "1800")))
    except (TypeError, ValueError):
        interval = 1800
    max_age = float(arch.get("hermes_heartbeat_max_age_sec") or (interval * 2.5))
    return not heartbeat_registry.redis_alive(
        "hermes", key_prefix=prefix, max_age_sec=max_age
    )


def _maybe_warn_stale(cfg):
    global _last_stale_warn_at
    from bus.heartbeats import heartbeat_registry

    arch = cfg.architecture_config
    if not arch.get("heartbeat_warn_enabled", True):
        return
    ttl = int(arch.get("heartbeat_ttl_sec", 120))
    stale = list(heartbeat_registry.stale_workers(ttl_sec=ttl))

    # External Hermes: never use local zombie; check Redis health key instead
    if arch.get("hermes_external") or not hermes_runs_in_process(cfg):
        heartbeat_registry.drop("hermes")
        stale = [w for w in stale if w != "hermes"]
        try:
            if _external_hermes_stale(arch):
                stale.append("hermes")
        except Exception as e:
            log(f"external hermes health check failed (fail-open): {e}", "DEBUG")

    if not stale:
        return
    now = time.time()
    if now - _last_stale_warn_at < max(ttl, 300):
        return
    _last_stale_warn_at = now
    try:
        from telegram_notifier import send_telegram_message

        send_telegram_message(
            f"⚠️ <b>Heartbeat stale</b>: {', '.join(stale)}",
            priority=0,
        )
    except Exception as e:
        log(f"Stale heartbeat warn failed: {e}", "WARNING")


def hermes_runs_in_process(cfg=None) -> bool:
    from core.config import get_bot_config

    cfg = cfg or get_bot_config()
    arch = cfg.architecture_config
    if arch.get("hermes_external"):
        return False
    return cfg.hermes_enabled