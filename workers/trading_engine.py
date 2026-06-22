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


def _consume_redis_once():
    from bus.redis_client import get_redis
    from bus.trade_intents import TradeIntent
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

    rows = client.xreadgroup(group, "worker-1", {stream: ">"}, count=1, block=2000)
    if not rows:
        return True

    svc = TradingService()
    for _stream, messages in rows:
        for msg_id, fields in messages:
            try:
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
                intent = TradeIntent(
                    intent_id=fields.get("intent_id", msg_id),
                    idempotency_key=fields.get("idempotency_key", ""),
                    scope=fields.get("scope", "paper"),
                    order=order,
                    timeframe=fields.get("timeframe", "4h"),
                    source=fields.get("source", "auto"),
                )
                from bus.locks import ledger_lock

                with ledger_lock(intent.scope):
                    result = svc._execute_order_locked(
                        intent.order,
                        intent.timeframe,
                        source=intent.source,
                        idempotency_key=intent.idempotency_key,
                        _lock_held=True,
                    )
                log(f"External engine filled {result.order_type} {result.symbol}: {result.executed}", "INFO")
                client.xack(stream, group, msg_id)
            except Exception as e:
                log(f"External trade intent failed: {e}", "ERROR")
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