"""Optional Redis stream publisher (no-op when Redis unavailable)."""

from __future__ import annotations

import json
from typing import Any

from bus.redis_client import get_redis
from bus.schemas import NotificationMessage


def publish_notification(msg: NotificationMessage, *, key_prefix: str = "aria:", redis_url: str | None = None):
    client = get_redis(redis_url, key_prefix=key_prefix)
    if not client:
        return
    stream = f"{key_prefix}notifications"
    payload = {
        "id": msg.id,
        "priority": str(msg.priority),
        "text": msg.text[:4000],
        "kind": msg.kind,
        "source": msg.source,
        "enqueued_at": msg.enqueued_at,
        "chat_id": str(msg.chat_id or ""),
    }
    try:
        client.xadd(stream, payload, maxlen=5000, approximate=True)
    except Exception:
        pass


def publish_job(
    job_id: str,
    kind: str,
    chat_id: str,
    params: dict,
    *,
    key_prefix: str = "aria:",
    redis_url: str | None = None,
):
    client = get_redis(redis_url, key_prefix=key_prefix)
    if not client:
        return
    stream = f"{key_prefix}jobs.heavy"
    try:
        client.xadd(
            stream,
            {
                "job_id": job_id,
                "kind": kind,
                "chat_id": str(chat_id),
                "params": json.dumps(params, default=str)[:8000],
            },
            maxlen=500,
            approximate=True,
        )
    except Exception:
        pass


def publish_trade_intent(intent, *, key_prefix: str = "aria:", redis_url: str | None = None):
    client = get_redis(redis_url, key_prefix=key_prefix)
    if not client:
        return
    stream = f"{key_prefix}commands.trade"
    order = intent.order
    payload = {
        "intent_id": intent.intent_id,
        "idempotency_key": intent.idempotency_key,
        "scope": intent.scope,
        "source": intent.source,
        "timeframe": intent.timeframe,
        "order": json.dumps(
            {
                "type": order.type,
                "symbol": order.symbol,
                "price": order.price,
                "amount": order.amount,
                "usdt_amount": order.usdt_amount,
                "signal": order.signal,
            },
            default=str,
        )[:4000],
        "enqueued_at": intent.intent_id,
    }
    try:
        client.xadd(stream, payload, maxlen=2000, approximate=True)
    except Exception:
        pass


def publish_signal_snapshot(snapshot: dict, *, key_prefix: str = "aria:", redis_url: str | None = None):
    client = get_redis(redis_url, key_prefix=key_prefix)
    if not client:
        return
    stream = f"{key_prefix}events.signals"
    flat = {k: json.dumps(v, default=str)[:8000] if not isinstance(v, str) else v[:8000] for k, v in snapshot.items()}
    try:
        client.xadd(stream, flat, maxlen=200, approximate=True)
    except Exception:
        pass