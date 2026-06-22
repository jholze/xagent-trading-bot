"""Trading engine runtime — intent queue consumer (hot-start safe)."""

from __future__ import annotations

import uuid

from bus.locks import ledger_lock
from bus.trade_intents import TradeIntent, TradeIntentQueue, make_idempotency_key, trade_intent_queue
from core.models import TradeOrder, TradeResult
from logger import log

_started = False


def should_queue_intent(source: str, cfg=None) -> bool:
    from core.config import get_bot_config

    cfg = cfg or get_bot_config()
    arch = cfg.architecture_config
    if not arch.get("trade_intent_queue_enabled", False):
        return False
    if arch.get("trade_intent_async_auto_only", True) and source == "manual":
        return False
    mode = arch.get("trading_engine_mode", "in_process")
    return mode in ("in_process", "external", "distributed")


def ensure_started():
    global _started
    if _started and trade_intent_queue.running:
        return
    from services.trading_service import TradingService

    svc = TradingService()

    def _execute_intent(intent: TradeIntent) -> TradeResult:
        with ledger_lock(intent.scope):
            return svc._execute_order_locked(
                intent.order,
                intent.timeframe,
                source=intent.source,
                trust_score=intent.trust_score,
                confidence=intent.confidence,
                indicators=intent.indicators,
                order_id=intent.order_id,
                request_extra=intent.request_extra,
                idempotency_key=intent.idempotency_key,
                _lock_held=True,
            )

    trade_intent_queue.start(_execute_intent)
    _started = True
    log("Trading engine runtime ready", "INFO")


def submit_trade_intent(
    order: TradeOrder,
    timeframe: str,
    *,
    source: str = "auto",
    trust_score: float | None = None,
    confidence: float | None = None,
    indicators: dict | None = None,
    order_id: str | None = None,
    request_extra: dict | None = None,
    idempotency_key: str | None = None,
    scope: str | None = None,
    wait_timeout: float = 120.0,
) -> TradeResult:
    from core.config import get_bot_config
    from data_manager import resolve_ledger_scope

    ensure_started()
    cfg = get_bot_config()
    scope = scope or resolve_ledger_scope(cfg.trading_mode)
    key = idempotency_key or getattr(order, "idempotency_key", "") or make_idempotency_key(
        order.symbol, timeframe, order.signal or order.type, source, scope
    )
    intent = TradeIntent(
        intent_id=uuid.uuid4().hex[:12],
        idempotency_key=key,
        scope=scope,
        order=order,
        timeframe=timeframe,
        source=source,
        trust_score=trust_score,
        confidence=confidence,
        indicators=indicators,
        order_id=order_id,
        request_extra=request_extra,
    )
    trade_intent_queue.submit(intent)
    return intent.wait(timeout=wait_timeout)