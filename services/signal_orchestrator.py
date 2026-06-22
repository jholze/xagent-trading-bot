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
from notifications.user_explain import (
    explain_hold_with_social,
    explain_trade,
    explanations_config,
)


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

    def analyze(self, coin: dict, current_price: float, x_signals=None, cmc_signals=None, lc_signals=None):
        return self.decision_engine.evaluate(coin, current_price, x_signals, cmc_signals, lc_signals)

    def _build_social_context(self, symbol: str, x_signals=None, cmc_signals=None, lc_signals=None, coin: dict = None) -> dict:
        base = symbol.split("/")[0]
        ctx = {}
        coin_x = self.decision_engine._signals_for_coin(symbol, x_signals)
        coin_cmc = self.decision_engine._signals_for_coin(symbol, cmc_signals)
        coin_lc = self.decision_engine._signals_for_coin(symbol, lc_signals)
        if coin_x:
            s = coin_x[0]
            ctx["x"] = {
                "account": getattr(s, "account", "?"),
                "action": getattr(s, "action", "HOLD"),
                "confidence": getattr(s, "confidence", 0),
                "trust_score": getattr(s, "trust_score", "?"),
                "rationale": getattr(s, "rationale", ""),
            }
        if coin_cmc:
            s = coin_cmc[0]
            ctx["cmc"] = {
                "action": getattr(s, "action", "HOLD"),
                "confidence": getattr(s, "confidence", 0),
                "votes_bullish": getattr(s, "votes_bullish", 0),
                "votes_bearish": getattr(s, "votes_bearish", 0),
                "rationale": getattr(s, "rationale", ""),
            }
        if coin_lc:
            s = coin_lc[0]
            ctx["lc"] = {
                "action": getattr(s, "action", "HOLD"),
                "confidence": getattr(s, "confidence", 0),
                "trust_score": getattr(s, "trust_score", 72),
                "effective_confidence": getattr(s, "effective_confidence", 0),
                "galaxy_score": getattr(s, "galaxy_score", 0),
                "alt_rank": getattr(s, "alt_rank", 0),
                "sentiment": getattr(s, "sentiment", 0),
                "rationale": getattr(s, "rationale", ""),
            }
        if coin:
            coin_cfg = resolve_coin_config(coin)
            sp = coin_cfg.get("strategy_params") or {}
            if sp.get("hermes_experiment_id"):
                ctx["hermes"] = {"experiment_id": sp.get("hermes_experiment_id")}
        return ctx

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
        elif "lc" in (analysis.sources or []):
            source = "lc"
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

        from bus.trade_intents import make_idempotency_key
        from data_manager import resolve_ledger_scope

        scope = resolve_ledger_scope(self.config.trading_mode)
        idem = make_idempotency_key(
            symbol, tf, order.signal or analysis.normalized_action, source, scope
        )
        order.idempotency_key = idem
        return self.trading.execute_order(
            order,
            tf,
            source=source,
            trust_score=trust_score,
            confidence=analysis.confidence,
            request_extra=request_extra or None,
            idempotency_key=idem,
        )

    def process_coin(self, coin: dict, current_price: float, x_signals=None, cmc_signals=None, lc_signals=None, quiet: bool = False) -> dict:
        if not current_price:
            return {"action": "HOLD", "symbol": coin.get("symbol", ""), "normalized_action": "HOLD"}

        analysis = self.analyze(coin, current_price, x_signals, cmc_signals, lc_signals)
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
        exp_cfg = explanations_config(self.config)
        social_ctx = self._build_social_context(symbol, x_signals, cmc_signals, lc_signals, coin=coin)
        explained = explain_trade(analysis, trade_result, social_ctx=social_ctx, signal=analysis.action)

        notify_trade = should_notify
        if trade_result and not trade_executed and not exp_cfg.get("notify_blocked_trades", True):
            notify_trade = False

        if notify_trade and self.notify_callback:
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
                why_de=explained.get("why_de"),
                tech_line=explained.get("tech_line"),
                source_de=explained.get("source_de"),
                social_lines=explained.get("social_lines"),
                confidence=analysis.confidence,
            )
        elif (
            exp_cfg.get("notify_social_hold_explanations")
            and analysis.normalized_action == "HOLD"
            and social_ctx
        ):
            from strategies.positions import count_open_positions

            hold_why = explain_hold_with_social(
                analysis,
                social_ctx,
                blockers={
                    "open_positions": count_open_positions(),
                    "max_open_positions": self.config.max_open_positions,
                    "has_position": has_position,
                },
            )
            if hold_why:
                from telegram_notifier import send_hold_explanation_message
                send_hold_explanation_message(symbol, hold_why, explained.get("tech_line", ""))

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
            "why_de": explained.get("why_de", ""),
        }