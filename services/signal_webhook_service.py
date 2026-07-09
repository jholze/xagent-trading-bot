"""Ingest external trading alerts into the 15m watch queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from logger import log
from strategies import watch_15m_state
from webhooks.adapters import parse_signal_payload
from webhooks.schemas import ExternalSignal
from webhooks.store import ingest as store_ingest


@dataclass
class SignalWebhookResult:
    ok: bool
    signal: ExternalSignal | None = None
    watch_set: bool = False
    message: str = ""
    redis_published: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "watch_set": self.watch_set,
            "message": self.message,
        }
        if self.signal:
            payload["signal"] = self.signal.as_dict()
        return payload


def signal_webhook_enabled(config_raw: dict | None = None) -> bool:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    arch = (config_raw or {}).get("architecture") or {}
    return bool(arch.get("signal_webhook_enabled", True))


def _arch(config_raw: dict | None) -> dict:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    return (config_raw or {}).get("architecture") or {}


def _resolve_timeframe(symbol: str) -> str:
    from data_manager import load_effective_watchlist

    for coin in load_effective_watchlist():
        if coin.get("symbol") == symbol:
            return str(coin.get("timeframe") or "4h")
    return "4h"


def _webhook_priority_enabled(config_raw: dict | None) -> bool:
    if config_raw is None:
        from core.config import get_bot_config

        return bool(get_bot_config().entry_sensor_15m_config.get("webhook_priority_poll", True))
    return bool((config_raw.get("entry_sensor_15m") or {}).get("webhook_priority_poll", True))


def process_signal_webhook(
    body: dict | str | None,
    *,
    source: str = "generic",
    config_raw: dict | None = None,
) -> SignalWebhookResult:
    if not signal_webhook_enabled(config_raw):
        return SignalWebhookResult(ok=False, message="signal_webhook_disabled")

    signal = parse_signal_payload(body, source=source)
    if signal is None or not signal.symbol:
        return SignalWebhookResult(ok=False, message="invalid_payload")

    arch = _arch(config_raw)
    rate_limit = int(arch.get("signal_webhook_rate_limit_per_min", 10))
    accepted, status = store_ingest(signal, config_raw=config_raw, rate_limit_per_min=rate_limit)
    if not accepted:
        return SignalWebhookResult(ok=False, signal=signal, message=status)

    from webhooks.store import publish_redis

    redis_ok = publish_redis(signal, config_raw=config_raw)

    cfg = (config_raw or {}).get("entry_sensor_15m") or {}
    if config_raw is None:
        from core.config import get_bot_config

        cfg = get_bot_config().entry_sensor_15m_config

    if not cfg.get("enabled", True):
        log(f"signal_webhook accepted but entry_sensor disabled: {signal.symbol}", "INFO")
        return SignalWebhookResult(
            ok=True,
            signal=signal,
            message="accepted_sensor_disabled",
            redis_published=redis_ok,
        )

    ttl_hours = float(cfg.get("watch_ttl_hours", 24))
    timeframe = _resolve_timeframe(signal.symbol)
    priority = _webhook_priority_enabled(config_raw)

    watch_15m_state.set_watch(
        signal.symbol,
        timeframe,
        reason=signal.reason(),
        ttl_hours=ttl_hours,
        priority_poll=priority,
        webhook_strength=signal.strength,
        webhook_source=signal.source,
        webhook_event_type=signal.event_type,
    )

    eval_enqueued = False
    try:
        from bus.eval_queue import eval_queue_enabled
        from services.eval_queue_runtime import enqueue_webhook_eval

        if eval_queue_enabled(config_raw):
            eval_enqueued = enqueue_webhook_eval(
                signal.symbol, timeframe, config_raw=config_raw,
            )
    except Exception as e:
        log(f"signal_webhook eval enqueue failed {signal.symbol}: {e}", "WARNING")

    log(
        f"signal_webhook accepted {signal.source} {signal.symbol} "
        f"{signal.event_type} strength={signal.strength:.2f}"
        f"{f' eval_queued={eval_enqueued}' if eval_enqueued else ''}",
        "INFO",
    )

    return SignalWebhookResult(
        ok=True,
        signal=signal,
        watch_set=True,
        message="accepted",
        redis_published=redis_ok,
    )