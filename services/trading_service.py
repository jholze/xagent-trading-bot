from core.config import BotConfig, get_bot_config
from core.models import RiskDecision, TradeOrder, TradeResult
from execution.factory import get_execution_adapter
from logger import log
from risk.risk_manager import RiskManager
from services.market_service import MarketService
from services.order_service import OrderService
from services.portfolio_service import PortfolioService


class TradingService:
    """Unified trading facade — respects trading_mode, risk limits, and safety gates."""

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        risk_manager: RiskManager = None,
        market_service: MarketService = None,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.market = market_service or MarketService()
        self.risk = risk_manager or RiskManager(self.config, self.portfolio, self.market)

    def refresh(self):
        self.config.refresh()
        return self

    @property
    def adapter(self):
        return get_execution_adapter(self.config, self.portfolio)

    def mode_label(self) -> str:
        from data_manager import is_demo_mode, resolve_ledger_backend

        if is_demo_mode():
            backend = resolve_ledger_backend("demo", self.config.raw)
            return f"demo ({backend} ledger)"
        mode = self.config.trading_mode
        if mode == "live":
            confirmed = "CONFIRMED" if self.config.live_confirmed else "needs /live_confirm"
            dry = " [DRY RUN]" if self.config.live_config.get("dry_run", True) else ""
            return f"live ({confirmed}){dry}"
        if mode == "off":
            return "off (analysis only)"
        backend = self.config.paper_config.get("backend", "local")
        if backend == "mongo":
            dual = " + JSON dual-write" if self.config.ledger_dual_write else ""
            return f"paper (mongo ledger{dual})"
        if backend != "local":
            return f"paper ({backend})"
        return "paper (local ledger)"

    def can_execute(self, source: str = "auto", trust_score: float = None) -> tuple:
        mode = self.config.trading_mode
        if mode == "off":
            return False, "Trading disabled (mode=off). Use /mode paper to enable."
        if mode == "paper":
            return True, ""
        if mode == "live":
            if not self.config.live_confirmed:
                return False, "Live trading requires /live_confirm first."
            min_trust = self.config.live_config.get("require_min_trust_score", 70)
            if source == "x" and trust_score is not None and trust_score < min_trust:
                return False, f"Trust score {trust_score:.0f} below live minimum ({min_trust})."
            return True, ""
        return False, f"Unknown trading mode: {mode}"

    def max_usdt_for_order(self) -> float:
        if self.config.trading_mode == "live":
            return float(self.config.live_config.get("max_usdt_per_trade", self.config.max_usdt_per_trade))
        return self.config.max_usdt_per_trade

    def evaluate_risk(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "manual",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
    ):
        return self.risk.evaluate(
            order,
            timeframe,
            source=source,
            trust_score=trust_score,
            confidence=confidence,
            indicators=indicators,
        )

    def execute_order(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "manual",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
        order_id: str = None,
        request_extra: dict = None,
        idempotency_key: str = None,
    ) -> TradeResult:
        from bus.trade_intents import make_idempotency_key
        from data_manager import resolve_ledger_scope
        from services.trading_engine_runtime import should_queue_intent, submit_trade_intent

        scope = resolve_ledger_scope(self.config.trading_mode)
        idem = idempotency_key or order.idempotency_key or ""
        if not idem and source != "manual":
            idem = make_idempotency_key(
                order.symbol, timeframe, order.signal or order.type, source, scope
            )
            order.idempotency_key = idem

        if should_queue_intent(source, self.config):
            return submit_trade_intent(
                order,
                timeframe,
                source=source,
                trust_score=trust_score,
                confidence=confidence,
                indicators=indicators,
                order_id=order_id,
                request_extra=request_extra,
                idempotency_key=idem,
                scope=scope,
            )

        from bus.locks import ledger_lock

        with ledger_lock(scope, cfg=self.config):
            return self._execute_order_locked(
                order,
                timeframe,
                source=source,
                trust_score=trust_score,
                confidence=confidence,
                indicators=indicators,
                order_id=order_id,
                request_extra=request_extra,
                idempotency_key=idem,
                _lock_held=True,
            )

    def _result_from_ledger(self, record: dict) -> TradeResult:
        status = record.get("status", "")
        side = (record.get("side") or "").upper()
        symbol = record.get("symbol", "")
        execution = record.get("execution") or {}
        if status == "filled":
            return TradeResult(
                True,
                side or "BUY",
                symbol,
                amount=float(execution.get("amount") or record.get("request", {}).get("amount") or 0),
                price=float(execution.get("price") or record.get("request", {}).get("price") or 0),
                usdt_amount=float(execution.get("usdt") or record.get("request", {}).get("usdt") or 0),
                pnl=float(record.get("pnl") or 0),
                message="Idempotent replay",
                order_id=record.get("id", ""),
            )
        msg = record.get("error") or f"Prior order status: {status}"
        return TradeResult(False, side or "BUY", symbol, message=msg, order_id=record.get("id", ""))

    def _execute_order_locked(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "manual",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
        order_id: str = None,
        request_extra: dict = None,
        idempotency_key: str = None,
        _lock_held: bool = False,
    ) -> TradeResult:
        self.refresh()
        ledger = OrderService()
        ledger_id = order_id or order.order_id or None
        idem = idempotency_key or order.idempotency_key or ""

        if idem and not ledger_id:
            prior = ledger.find_by_idempotency_key(idem)
            if prior and prior.get("status") in ("filled", "rejected", "executing", "failed"):
                return self._result_from_ledger(prior)

        ok, reason = self.can_execute(source=source, trust_score=trust_score)
        if not ok:
            log(f"Trade blocked: {reason}", "WARNING")
            if not ledger_id:
                ledger.record_rejected(
                    order,
                    RiskDecision(approved=False, message=reason, code="mode_blocked", order=order),
                    timeframe=timeframe,
                    request_extra=request_extra,
                )
            return TradeResult(False, order.type, order.symbol, message=reason, order_id=ledger_id or "")

        decision = self.risk.evaluate(
            order,
            timeframe,
            source=source,
            trust_score=trust_score,
            confidence=confidence,
            indicators=indicators,
        )
        if not decision.approved:
            log(f"Risk rejected {order.type} {order.symbol}: {decision.message}", "WARNING")
            if not ledger_id:
                ledger.record_rejected(order, decision, timeframe=timeframe, request_extra=request_extra)
            else:
                ledger.update_status(ledger_id, "rejected", error=decision.message, risk=ledger._risk_snapshot(decision))
            return TradeResult(False, order.type, order.symbol, message=decision.message, order_id=ledger_id or "")

        approved_order = decision.order
        if ledger_id:
            ledger.update_status(ledger_id, "executing", risk=ledger._risk_snapshot(decision))
            approved_order.order_id = ledger_id
        else:
            created = ledger.create_from_request(
                approved_order,
                timeframe=timeframe,
                status="executing",
                risk=decision,
                request_extra=request_extra,
                idempotency_key=idem,
            )
            ledger_id = created["id"]
            approved_order.order_id = ledger_id

        result = self.adapter.execute(approved_order, timeframe)
        result.order_id = ledger_id
        ledger.link_execution_result(ledger_id, result, approved_order)
        if result.executed:
            log(
                f"{self.adapter.mode.upper()} {approved_order.type} {approved_order.symbol} "
                f"executed (${approved_order.usdt_amount:.0f})",
                "INFO",
            )
            if approved_order.type in ("BUY", "SELL"):
                try:
                    from notifications.telegram_commands.position_display import send_positions_snapshot
                    send_positions_snapshot(trade_result=result, mode_label=self.mode_label())
                except Exception as e:
                    log(f"Positions snapshot failed: {e}", "WARNING")
        elif decision.size_multiplier != 1.0 and not result.message:
            result.message = f"Size multiplier: {decision.size_multiplier:.2f}x"
        return result

    def execute_buy(
        self, symbol: str, timeframe: str, price: float, usdt: float = None, order_id: str = None,
    ) -> TradeResult:
        order = TradeOrder(
            type="BUY",
            symbol=symbol,
            price=price,
            amount=0,
            usdt_amount=usdt or 0,
            source="manual",
            order_id=order_id or "",
        )
        return self.execute_order(order, timeframe, source="manual", order_id=order_id)

    def execute_sell(
        self, symbol: str, timeframe: str, price: float, signal: str, amount: float, order_id: str = None,
    ) -> TradeResult:
        order = TradeOrder(
            type="SELL", symbol=symbol, price=price, amount=amount, signal=signal, source="manual",
            order_id=order_id or "",
        )
        return self.execute_order(order, timeframe, source="manual", order_id=order_id)