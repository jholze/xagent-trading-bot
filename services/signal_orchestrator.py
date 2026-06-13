from datetime import datetime

from core.config import get_bot_config
from core.models import TradeOrder
from data_manager import get_text, load_live_trade_history, load_trade_history, uses_exchange_ledger
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from services.audit_trail import AuditTrail
from services.trading_service import TradingService
from core.actions import is_sell
from strategies.positions import get_position
from strategies.decision_engine import DecisionEngine
from strategies.registry import resolve_coin_config


class SignalOrchestrator:
    """Coordinates analysis, execution, and notification without strategy↔telegram coupling."""

    def __init__(
        self,
        market_service: MarketService = None,
        portfolio: PortfolioService = None,
        notify_callback=None,
    ):
        self.config = get_bot_config()
        self.market = market_service or MarketService()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.trading = TradingService(self.config, self.portfolio)
        self.notify_callback = notify_callback
        self.decision_engine = DecisionEngine(self.market)
        self.audit = AuditTrail(self.config)

    def analyze(self, coin: dict, current_price: float, x_signals=None, cmc_signals=None):
        return self.decision_engine.evaluate(coin, current_price, x_signals, cmc_signals)

    def execute_if_needed(self, analysis, coin: dict, current_price: float, x_signals=None):
        if analysis is None or analysis.action == "HOLD":
            return None

        self.trading.refresh()
        symbol = analysis.symbol
        tf = analysis.timeframe
        coin_cfg = resolve_coin_config(coin)
        strategy_params = coin_cfg.get("strategy_params") or {}
        request_extra = {}
        if strategy_params.get("hermes_experiment_id"):
            source = "hermes"
            request_extra = {
                "hermes_experiment_id": strategy_params.get("hermes_experiment_id"),
                "hermes_updated_at": strategy_params.get("hermes_updated_at"),
            }
        elif "x" in (analysis.sources or []):
            source = "x"
        elif "cmc" in (analysis.sources or []):
            source = "cmc"
        else:
            source = "auto"
        trust_score = analysis.x_confidence if source == "x" else None

        if "BUY" in analysis.action:
            order = TradeOrder(
                type="BUY",
                symbol=symbol,
                price=current_price,
                amount=0,
                usdt_amount=0,
                signal=analysis.action,
                source=source,
            )
        else:
            pos = get_position(symbol, tf)
            from strategies.positions import sell_fraction_for_signal
            fraction = sell_fraction_for_signal(analysis.action)
            amount_sold = float(pos["amount"]) * fraction
            sell_signal = analysis.normalized_action or analysis.action
            order = TradeOrder(
                type="SELL",
                symbol=symbol,
                price=current_price,
                amount=amount_sold,
                signal=sell_signal,
                source=source,
            )

        return self.trading.execute_order(
            order,
            tf,
            source=source,
            trust_score=trust_score,
            confidence=analysis.confidence,
            request_extra=request_extra or None,
        )

    def process_coin(self, coin: dict, current_price: float, x_signals=None, cmc_signals=None, quiet: bool = False) -> dict:
        if not current_price:
            return {"action": "HOLD", "symbol": coin.get("symbol", ""), "normalized_action": "HOLD"}

        analysis = self.analyze(coin, current_price, x_signals, cmc_signals)
        if analysis is None:
            return {"action": "HOLD", "symbol": coin.get("symbol", ""), "normalized_action": "HOLD"}

        trade_result = self.execute_if_needed(analysis, coin, current_price)
        self.audit.record(coin, analysis, trade_result, current_price)

        symbol = coin["symbol"]
        tf = analysis.timeframe
        pos = get_position(symbol, tf)
        has_position = float(pos.get("amount", 0)) > 0

        if self.config.raw.get("debug", False):
            print(get_text("debug_ampel_change").format(
                symbol=symbol,
                old=pos.get("last_ampel", "🟡"),
                new=analysis.ampel_emoji,
                old_rsi=pos.get("last_rsi", 45.0),
                new_rsi=analysis.rsi,
                send=analysis.should_notify,
                reason=analysis.notify_reason,
            ))

        should_notify = analysis.should_notify
        if is_sell(analysis.action) and not has_position:
            should_notify = False

        trade_executed = bool(trade_result.executed) if trade_result else False
        if should_notify and self.notify_callback:
            self.notify_callback(
                analysis.action,
                coin,
                current_price,
                analysis.rsi,
                analysis.lower_bb,
                analysis.vol_multiplier,
                analysis.ampel_emoji,
                analysis.ampel_text,
                executed=trade_executed if trade_result else None,
                trade_message=trade_result.message if trade_result else None,
                trade_result=trade_result,
                sources=analysis.sources,
                timeframe=tf,
            )

        pos["last_ampel"] = analysis.ampel_emoji
        pos["last_rsi"] = analysis.rsi

        unrealized = 0.0
        if has_position and pos.get("average_entry", 0) > 0:
            unrealized = (current_price - pos["average_entry"]) * float(pos["amount"])

        history = (
            load_live_trade_history()
            if uses_exchange_ledger(self.config.trading_mode)
            else load_trade_history()
        )
        realized = history.get("total_pnl", history.get("realized_pnl", 0))
        pos_info = (
            f" | Pos: {float(pos.get('amount', 0)):.2f} | Unrealized: ${unrealized:.1f}"
            if has_position else " | No position"
        )
        executed = f" | Executed: {trade_result.order_type}" if trade_result and trade_result.executed else ""
        rationale = f" | {analysis.rationale}" if analysis.rationale else ""
        if not quiet:
            print(
                f"{symbol} → {analysis.action} ({analysis.normalized_action}) | RSI: {analysis.rsi:.1f} | "
                f"Vol: {analysis.vol_multiplier:.2f}x | Ampel: {analysis.ampel_emoji} {analysis.ampel_text}"
                f"{rationale}{pos_info}{executed} | Bal: ${history.get('virtual_balance', 0):.0f} | "
                f"RealPnL: ${float(realized or 0):.1f}\n"
            )
        return {
            "action": analysis.action,
            "normalized_action": analysis.normalized_action,
            "symbol": symbol,
            "rsi": analysis.rsi,
            "vol_multiplier": analysis.vol_multiplier,
            "ampel_emoji": analysis.ampel_emoji,
            "ampel_text": analysis.ampel_text,
            "rationale": analysis.rationale,
            "sources": list(analysis.sources or []),
            "confidence": analysis.confidence,
            "executed": bool(trade_result.executed) if trade_result else False,
            "order_type": trade_result.order_type if trade_result else None,
            "trade_message": trade_result.message if trade_result else "",
            "unrealized": unrealized,
        }