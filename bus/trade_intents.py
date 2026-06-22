"""Trade intent queue — single-writer execution path (Phase 5)."""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.models import TradeOrder, TradeResult
from logger import log


@dataclass
class TradeIntent:
    intent_id: str
    idempotency_key: str
    scope: str
    order: TradeOrder
    timeframe: str
    source: str
    trust_score: float | None = None
    confidence: float | None = None
    indicators: dict | None = None
    order_id: str | None = None
    request_extra: dict | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _result: TradeResult | None = field(default=None, repr=False)
    _error: str | None = field(default=None, repr=False)

    def set_result(self, result: TradeResult):
        self._result = result
        self._done.set()

    def set_error(self, message: str):
        self._error = message
        self._done.set()

    def wait(self, timeout: float = 120.0) -> TradeResult:
        if not self._done.wait(timeout=timeout):
            return TradeResult(False, self.order.type, self.order.symbol, message="Trade intent timeout")
        if self._error:
            return TradeResult(False, self.order.type, self.order.symbol, message=self._error)
        return self._result or TradeResult(False, self.order.type, self.order.symbol, message="No result")


class TradeIntentQueue:
    def __init__(self):
        self._queue: queue.Queue[TradeIntent | None] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._executor: Callable[..., TradeResult] | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, executor: Callable[..., TradeResult]):
        if self._running:
            return
        self._executor = executor
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="trading-engine")
        self._thread.start()
        log("Trade intent queue started", "INFO")

    def stop(self):
        self._running = False
        self._queue.put(None)

    def submit(self, intent: TradeIntent) -> TradeIntent:
        self._publish_redis(intent)
        self._queue.put(intent)
        return intent

    def _publish_redis(self, intent: TradeIntent):
        try:
            from bus.publisher import publish_trade_intent
            from core.config import get_bot_config

            arch = get_bot_config().architecture_config
            publish_trade_intent(intent, key_prefix=arch.get("key_prefix", "aria:"), redis_url=arch.get("redis_url"))
        except Exception:
            pass

    def _loop(self):
        while self._running:
            try:
                intent = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if intent is None:
                break
            try:
                if self._executor is None:
                    intent.set_error("No executor")
                    continue
                from bus.heartbeats import heartbeat_registry
                from core.config import get_bot_config

                arch = get_bot_config().architecture_config
                heartbeat_registry.beat(
                    "trading_engine",
                    meta={"intent": intent.intent_id},
                    ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                    key_prefix=arch.get("key_prefix", "aria:"),
                )
                result = self._executor(intent)
                intent.set_result(result)
            except Exception as e:
                log(f"Trade intent {intent.intent_id} failed: {e}", "ERROR")
                intent.set_error(str(e))
            finally:
                self._queue.task_done()


trade_intent_queue = TradeIntentQueue()


def make_idempotency_key(
    symbol: str,
    timeframe: str,
    signal: str,
    source: str,
    scope: str,
    *,
    bucket: str | None = None,
) -> str:
    from datetime import datetime

    hour = bucket or datetime.now().strftime("%Y%m%d%H")
    sig = (signal or "MARKET").upper()
    return f"{scope}:{symbol}:{timeframe}:{sig}:{source}:{hour}"