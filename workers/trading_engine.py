#!/usr/bin/env python3
"""Standalone trading engine (Phase 5) — consumes Redis trade intents."""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import get_bot_config
from core.models import TradeOrder
from logger import log
from services.architecture_runtime import ensure_started
from services.trading_engine_runtime import ensure_started as ensure_trading_engine

# Pending entries idle longer than this are reclaimed for one retry.
_PENDING_IDLE_MS = 60_000
_RECLAIM_COUNT = 10
_CONSUMER = "worker-1"


def _process_trade_message(svc, stream: str, group: str, client, msg_id, fields) -> None:
    """Execute one trade intent message. Raises on failure (caller decides ack)."""
    raw_order = json.loads(fields.get("order", "{}"))
    order = TradeOrder(
        type=raw_order.get("type", "BUY"),
        symbol=raw_order.get("symbol", ""),
        price=float(raw_order.get("price") or 0),
        amount=float(raw_order.get("amount") or 0),
        usdt_amount=float(raw_order.get("usdt_amount") or 0),
        signal=raw_order.get("signal", ""),
        source=fields.get("source", "auto"),
        idempotency_key=fields.get("idempotency_key", ""),
    )
    from bus.trade_intents import TradeIntent

    intent = TradeIntent(
        intent_id=fields.get("intent_id", msg_id),
        idempotency_key=fields.get("idempotency_key", ""),
        scope=fields.get("scope", "paper"),
        order=order,
        timeframe=fields.get("timeframe", "4h"),
        source=fields.get("source", "auto"),
        tenant_id=fields.get("tenant_id", "default"),
        owner_chat_id=fields.get("owner_chat_id", ""),
    )
    from bus.locks import ledger_lock
    from core.tenant_context import tenant_context

    with tenant_context(
        intent.tenant_id,
        scope=intent.scope,
        owner_chat_id=intent.owner_chat_id,
    ):
        with ledger_lock(intent.scope, tenant_id=intent.tenant_id):
            result = svc._execute_order_locked(
                intent.order,
                intent.timeframe,
                source=intent.source,
                idempotency_key=intent.idempotency_key,
                _lock_held=True,
            )
    log(f"External engine filled {result.order_type} {result.symbol}: {result.executed}", "INFO")
    client.xack(stream, group, msg_id)


def _reclaim_stale_pending(client, stream: str, group: str) -> list:
    """Claim messages pending longer than the idle threshold for one retry."""
    try:
        resp = client.xautoclaim(
            stream,
            group,
            _CONSUMER,
            _PENDING_IDLE_MS,
            "0-0",
            count=_RECLAIM_COUNT,
        )
        # redis-py: (next_id, [(id, fields), ...], [deleted_ids?])
        if resp and len(resp) >= 2:
            return list(resp[1] or [])
    except Exception as e:
        log(f"xautoclaim failed, trying xpending/xclaim: {e}", "WARNING")
        try:
            pending = client.xpending_range(
                stream, group, min="-", max="+", count=_RECLAIM_COUNT, idle=_PENDING_IDLE_MS
            )
            claimed = []
            for entry in pending or []:
                msg_id = entry.get("message_id") if isinstance(entry, dict) else entry[0]
                if not msg_id:
                    continue
                rows = client.xclaim(stream, group, _CONSUMER, _PENDING_IDLE_MS, [msg_id])
                if rows:
                    claimed.extend(rows)
            return claimed
        except Exception as e2:
            log(f"pending reclaim failed: {e2}", "WARNING")
    return []


def _consume_redis_once():
    from bus.redis_client import get_redis
    from services.trading_service import TradingService

    cfg = get_bot_config()
    arch = cfg.architecture_config
    if arch.get("trading_engine_mode") not in ("external", "distributed"):
        return False

    client = get_redis(arch.get("redis_url"), key_prefix=arch.get("key_prefix", "aria:"))
    if not client:
        return False

    stream = f"{arch.get('key_prefix', 'aria:')}commands.trade"
    group = "trading-engine"
    try:
        client.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception:
        pass

    svc = TradingService()

    # Reclaim stale pending entries (first failure left them unacked).
    reclaimed = _reclaim_stale_pending(client, stream, group)
    for msg_id, fields in reclaimed:
        try:
            _process_trade_message(svc, stream, group, client, msg_id, fields or {})
        except Exception as e:
            # Second failure: ack and surface payload for manual replay.
            try:
                client.xack(stream, group, msg_id)
            except Exception:
                pass
            log(
                f"External trade intent dead-lettered after reclaim retry "
                f"msg_id={msg_id} error={e} payload={fields}",
                "ERROR",
            )

    rows = client.xreadgroup(group, _CONSUMER, {stream: ">"}, count=1, block=2000)
    if not rows:
        return True

    for _stream, messages in rows:
        for msg_id, fields in messages:
            try:
                _process_trade_message(svc, stream, group, client, msg_id, fields or {})
            except Exception as e:
                # Leave unacked so xautoclaim can retry after idle threshold.
                log(f"External trade intent failed (will reclaim later): {e}", "ERROR")
    return True


def main():
    cfg = get_bot_config()
    arch = cfg.architecture_config
    ensure_started(force_refresh=True)
    ensure_trading_engine()
    log(f"Trading engine worker mode={arch.get('trading_engine_mode')}", "INFO")

    while True:
        try:
            if arch.get("trading_engine_mode") in ("external", "distributed"):
                _consume_redis_once()
            else:
                from bus.heartbeats import heartbeat_registry

                heartbeat_registry.beat(
                    "trading_engine",
                    ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                    key_prefix=arch.get("key_prefix", "aria:"),
                )
                time.sleep(5)
        except Exception as e:
            log(f"Trading engine worker error: {e}", "ERROR")
            time.sleep(5)


if __name__ == "__main__":
    main()
