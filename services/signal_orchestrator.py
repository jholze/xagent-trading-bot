from datetime import datetime

from core.config import get_bot_config
from core.models import TradeOrder
from data_manager import get_text, load_live_trade_history, load_trade_history, uses_exchange_ledger
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from services.audit_trail import AuditTrail
from services.trading_service import TradingService
from core.actions import BUY_DCA, SELL_FULL, is_buy, is_sell
from strategies.positions import get_position
from strategies.decision_engine import DecisionEngine
from strategies.dca_portfolio import build_portfolio_dca_plan, portfolio_config
from strategies.registry import resolve_coin_config
from logger import log
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

    def begin_tenant_cycle(self) -> None:
        self.config.refresh()
        self.decision_engine.begin_tenant_cycle()

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

    def execute_if_needed(
        self,
        analysis,
        coin: dict,
        current_price: float,
        x_signals=None,
        sensor_metrics: dict | None = None,
    ):
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
        elif "dca_recovery" in (analysis.sources or []):
            source = "dca_recovery"
        elif "dca" in (analysis.sources or []):
            source = "dca"
        elif "entry_sensor_15m" in (analysis.sources or []):
            source = "entry_sensor_15m"
        else:
            source = "auto"
        trust_score = analysis.x_confidence if source == "x" else None

        if "BUY" in analysis.action:
            dca_usdt = float(getattr(analysis, "dca_usdt", 0) or 0)
            vol_ratio = None
            if sensor_metrics and source == "entry_sensor_15m":
                raw_ratio = float(sensor_metrics.get("volume_spike_ratio", 0) or 0)
                vol_ratio = raw_ratio if raw_ratio > 0 else None
            order = TradeOrder(
                type="BUY",
                symbol=symbol,
                price=current_price,
                amount=0,
                usdt_amount=dca_usdt,
                signal=analysis.normalized_action or analysis.action,
                source=source,
                entry_15m_vol_ratio=vol_ratio,
            )
        else:
            pos = get_position(symbol, tf)
            from strategies.positions import sell_fraction_for_signal

            strategy_params = getattr(analysis, "strategy_params", None) or {}
            if not strategy_params:
                try:
                    from strategies.registry import resolve_strategy_params

                    strategy_params = resolve_strategy_params(
                        {"symbol": symbol, "timeframe": tf},
                        has_position=True,
                        atr_pct=getattr(analysis, "atr_pct", 3.0),
                        frozen_tier=pos.get("strategy_tier"),
                    )
                except Exception:
                    strategy_params = {}
            fraction = sell_fraction_for_signal(
                analysis.action, symbol, tf, current_price, strategy_params,
            )
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

    def process_entry_sensor(
        self,
        coin: dict,
        current_price: float,
        sensor_metrics: dict | None = None,
        quiet: bool = True,
    ) -> dict:
        """15m sensor loop: analyze and execute BUY only — never sell from this path."""
        if not current_price:
            return {"action": "HOLD", "symbol": coin.get("symbol", ""), "normalized_action": "HOLD"}

        analysis = self.analyze(coin, current_price)
        if analysis is None:
            return {"action": "HOLD", "symbol": coin.get("symbol", ""), "normalized_action": "HOLD"}

        trade_result = None
        if is_buy(analysis.action):
            trade_result = self.execute_if_needed(
                analysis, coin, current_price, sensor_metrics=sensor_metrics,
            )
        self.audit.record(coin, analysis, trade_result, current_price)

        symbol = coin["symbol"]
        tf = analysis.timeframe
        pos = get_position(symbol, tf)
        has_position = float(pos.get("amount", 0)) > 0

        trade_executed = bool(trade_result.executed) if trade_result else False
        reported_action = analysis.action if is_buy(analysis.action) else "HOLD"
        reported_normalized = (
            analysis.normalized_action if is_buy(analysis.action) else "HOLD"
        )

        if not quiet:
            executed = f" | Executed: {trade_result.order_type}" if trade_executed else ""
            print(
                f"{symbol} [15m-entry] → {reported_action} | sources={analysis.sources}"
                f"{executed}\n"
            )

        return {
            "action": reported_action,
            "normalized_action": reported_normalized,
            "symbol": symbol,
            "rationale": analysis.rationale,
            "sources": list(analysis.sources or []),
            "confidence": analysis.confidence,
            "executed": trade_executed,
            "order_type": trade_result.order_type if trade_result else None,
            "trade_message": trade_result.message if trade_result else "",
            "has_position": has_position,
        }

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
                from services.cycle_notification_policy import cycle_notification_policy

                confidence = cycle_notification_policy.social_confidence_from_context(social_ctx)
                cycle_notification_policy.offer_hold_explanation(
                    symbol,
                    hold_why,
                    tech_line=explained.get("tech_line", ""),
                    confidence=confidence,
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
            "why_de": explained.get("why_de", ""),
        }

    def run_portfolio_dca_pass(
        self,
        coins: list[dict],
        price_map: dict[str, float],
        *,
        quiet: bool = False,
    ) -> dict:
        """Rank DCA targets portfolio-wide; optionally fund via rotation sell."""
        from risk.risk_manager import RiskManager

        port_cfg = portfolio_config({})
        volatile_dca = (self.config.raw.get("volatile_altcoin") or {}).get("dca") or {}
        port_cfg = {**port_cfg, **portfolio_config(volatile_dca)}
        if not port_cfg.get("enabled"):
            return {"skipped": True, "reason": "portfolio_disabled"}

        risk = RiskManager(self.config, self.market)
        cash = risk._available_usdt()
        plan = build_portfolio_dca_plan(coins, price_map, cash_available=cash, config_raw=self.config.raw)
        result = {"plan": plan.audit, "executed": False}

        if not plan.buy:
            return result

        live = str(port_cfg.get("mode", "shadow")).lower() == "live"
        if plan.shadow_only or not live:
            result["shadow"] = True
            log(
                f"DCA portfolio shadow: {plan.buy.symbol} ${plan.buy.usdt_needed:.0f} "
                f"score={plan.buy.score} funding={getattr(plan.funding_sell, 'symbol', None)}",
                "INFO",
            )
            return result

        self.trading.refresh()

        if plan.funding_sell:
            fs = plan.funding_sell
            pos = get_position(fs.symbol, fs.timeframe)
            amount = float(pos.get("amount", 0) or 0)
            if amount > 0:
                price = float(price_map.get(fs.symbol, 0) or 0)
                sell_order = TradeOrder(
                    type="SELL",
                    symbol=fs.symbol,
                    price=price,
                    amount=amount,
                    signal=SELL_FULL,
                    source=f"dca_fund_{fs.source}",
                )
                sell_result = self.trading.execute_order(
                    sell_order,
                    fs.timeframe,
                    source=sell_order.source,
                )
                result["funding_sell"] = {
                    "symbol": fs.symbol,
                    "executed": bool(sell_result.executed),
                    "message": sell_result.message,
                    "source": fs.source,
                }
                if sell_result.executed:
                    cash = risk._available_usdt()
                    log(
                        f"DCA fund-sell {fs.symbol} ({fs.source}) → cash for {plan.buy.symbol}",
                        "INFO",
                    )

        buy = plan.buy
        price = float(price_map.get(buy.symbol, 0) or 0)
        if price <= 0:
            return result

        buy_order = TradeOrder(
            type="BUY",
            symbol=buy.symbol,
            price=price,
            amount=0,
            usdt_amount=buy.usdt_needed,
            signal=BUY_DCA,
            source=buy.source,
        )
        buy_result = self.trading.execute_order(
            buy_order,
            buy.timeframe,
            source=buy.source,
            confidence=buy.score,
        )
        result["executed"] = bool(buy_result.executed)
        result["buy"] = {
            "symbol": buy.symbol,
            "usdt": buy.usdt_needed,
            "score": buy.score,
            "message": buy_result.message,
        }
        if buy_result.executed and self.notify_callback:
            coin = next((c for c in coins if c.get("symbol") == buy.symbol), {"symbol": buy.symbol})
            rationale = buy.candidate.rationale
            if plan.funding_sell:
                rationale = (
                    f"Portfolio-DCA: {plan.funding_sell.symbol} verkauft → "
                    f"{buy.symbol} aufgestockt ({rationale})"
                )
            self.notify_callback(
                "BUY_DCA",
                coin,
                price,
                0,
                0,
                0,
                "🟢",
                "DCA",
                executed=True,
                trade_message=buy_result.message,
                trade_result=buy_result,
                sources=[buy.source, "dca_portfolio"],
                timeframe=buy.timeframe,
                why_de=rationale,
            )
        if not quiet:
            print(
                f"Portfolio DCA: {buy.symbol} ${buy.usdt_needed:.0f} "
                f"(score {buy.score}) fund={getattr(plan.funding_sell, 'symbol', '-')}"
            )
        return result