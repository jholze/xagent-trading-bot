from datetime import datetime, timedelta

from core.config import BotConfig, get_bot_config
from core.models import RiskDecision, TradeOrder
from data_manager import load_trade_history, load_live_trade_history
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from strategies.positions import count_open_positions, get_position, list_active_positions


class RiskManager:
    """Central gate for trade sizing and portfolio limits."""

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        market_service: MarketService = None,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.market = market_service or MarketService()

    def evaluate(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "auto",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
    ) -> RiskDecision:
        if order.type == "SELL":
            blocked, reason = self._trade_cooldown_blocked(order, timeframe)
            if blocked:
                return RiskDecision(approved=False, message=reason, code="trade_cooldown")
            if order.amount <= 0:
                return RiskDecision(approved=False, message="No amount to sell", code="no_amount")
            return RiskDecision(approved=True, order=order, message="Sell approved")

        if order.price <= 0:
            return RiskDecision(approved=False, message="Invalid price")

        blocked, reason = self._trade_cooldown_blocked(order, timeframe)
        if blocked:
            return RiskDecision(approved=False, message=reason, code="trade_cooldown")

        pos = get_position(order.symbol, timeframe)
        has_position = float(pos.get("amount", 0)) > 0

        if not has_position and count_open_positions() >= self.config.max_open_positions:
            return RiskDecision(
                approved=False,
                message=f"Max open positions reached ({self.config.max_open_positions})",
                code="max_open_positions",
            )

        if self._daily_trades_count() >= self.config.max_daily_trades:
            return RiskDecision(
                approved=False,
                message=f"Daily trade limit reached ({self.config.max_daily_trades})",
                code="max_daily_trades",
            )

        base_usdt = order.usdt_amount or self._base_usdt_cap()
        if indicators is None:
            indicators = self.market.fetch_indicators(order.symbol, timeframe, order.price)

        sized, factors = self._dynamic_size(
            base_usdt,
            order,
            timeframe,
            source,
            trust_score,
            confidence,
            indicators,
        )

        equity = self._portfolio_equity(order.price)
        pos_value = float(pos.get("amount", 0)) * order.price
        max_position_value = equity * (self.config.max_position_percent / 100.0)
        room = max_position_value - pos_value

        if room <= 0:
            return RiskDecision(
                approved=False,
                message=f"Max position concentration ({self.config.max_position_percent}% of portfolio)",
                code="max_position_percent",
            )

        if sized > room:
            sized = room
            factors["concentration_capped"] = True

        balance = load_trade_history().get("virtual_balance", equity)
        if self.config.trading_mode == "paper" and sized > balance:
            sized = balance

        min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
        if sized < min_trade:
            return RiskDecision(
                approved=False,
                message=f"Adjusted size ${sized:.2f} below minimum (${min_trade:.0f})",
                code="size_too_small",
                size_multiplier=factors.get("total_multiplier", 1.0),
                drawdown_pct=factors.get("drawdown_pct", 0.0),
                atr_factor=factors.get("atr_factor", 1.0),
                trust_factor=factors.get("trust_factor", 1.0),
            )

        approved = TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=order.amount,
            usdt_amount=round(sized, 2),
            signal=order.signal,
            timestamp=order.timestamp,
        )
        return RiskDecision(
            approved=True,
            order=approved,
            message="Approved",
            size_multiplier=factors.get("total_multiplier", 1.0),
            drawdown_pct=factors.get("drawdown_pct", 0.0),
            atr_factor=factors.get("atr_factor", 1.0),
            trust_factor=factors.get("trust_factor", 1.0),
        )

    def status_summary(self, current_price: float = None) -> dict:
        history = load_trade_history()
        equity = self._portfolio_equity(current_price or 0)
        initial = self._initial_capital()
        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(self.config.risk_config.get("drawdown_throttle_pct", 10.0))
        return {
            "open_positions": count_open_positions(),
            "max_open_positions": self.config.max_open_positions,
            "daily_trades": self._daily_trades_count(),
            "max_daily_trades": self.config.max_daily_trades,
            "max_position_percent": self.config.max_position_percent,
            "base_usdt_per_trade": self._base_usdt_cap(),
            "portfolio_equity": round(equity, 2),
            "initial_capital": initial,
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_throttle_active": drawdown_pct >= throttle_at,
            "virtual_balance": history.get("virtual_balance", 0),
        }

    def _base_usdt_cap(self) -> float:
        if self.config.trading_mode == "live":
            return float(
                self.config.live_config.get("max_usdt_per_trade", self.config.max_usdt_per_trade)
            )
        if self.config.trading_mode == "gate_testnet":
            return float(
                self.config.gate_testnet_config.get(
                    "max_usdt_per_trade", self.config.max_usdt_per_trade
                )
            )
        return self.config.max_usdt_per_trade

    def _initial_capital(self) -> float:
        paper = self.config.paper_config.get("initial_capital_usdt")
        if paper:
            return float(paper)
        return float(self.config.raw.get("initial_capital_usdt", 5000))

    def _portfolio_equity(self, reference_price: float = 0) -> float:
        history = load_trade_history()
        balance = float(history.get("virtual_balance", self._initial_capital()))
        unrealized = 0.0
        for pos in list_active_positions():
            entry = pos.get("average_entry", 0) or pos.get("entry_price", 0)
            price = reference_price if reference_price > 0 else entry
            unrealized += (price - entry) * pos.get("amount", 0)
        return max(balance + unrealized, balance)

    def _equity_drawdown_pct(self, reference_price: float = 0) -> float:
        history = load_trade_history()
        balance = float(history.get("virtual_balance", self._initial_capital()))
        initial = self._initial_capital()
        peak = float(history.get("peak_equity", initial))
        peak = max(peak, balance, initial)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - balance) / peak * 100.0)

    def _dynamic_size(
        self,
        base_usdt: float,
        order: TradeOrder,
        timeframe: str,
        source: str,
        trust_score: float,
        confidence: float,
        indicators: dict,
    ) -> tuple[float, dict]:
        aggression = self.config.aggression_config
        risk = self.config.risk_config

        trust = trust_score if trust_score is not None else 70.0
        conf = confidence if confidence is not None else 50.0

        trust_delta = (trust - 70.0) / 10.0
        trust_factor = 1.0 + trust_delta * 0.1
        if source == "x" and trust < aggression.get("min_trust_for_live", 70):
            trust_factor *= 0.85

        conf_factor = 0.8 + (conf / 100.0) * 0.4

        atr_pct = float(indicators.get("atr_pct", risk.get("atr_reference_pct", 3.0)))
        ref_atr = float(risk.get("atr_reference_pct", 3.0))
        atr_factor = min(1.5, max(0.5, ref_atr / max(atr_pct, 0.5)))

        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0))
        dd_mult = float(risk.get("drawdown_size_multiplier", 0.5)) if drawdown_pct >= throttle_at else 1.0

        total = trust_factor * conf_factor * atr_factor * dd_mult
        max_mult = float(aggression.get("max_position_multiplier", 2.0))
        min_mult = float(risk.get("min_size_multiplier", 0.25))
        total = max(min_mult, min(max_mult, total))

        return base_usdt * total, {
            "trust_factor": round(trust_factor, 3),
            "conf_factor": round(conf_factor, 3),
            "atr_factor": round(atr_factor, 3),
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_multiplier": dd_mult,
            "total_multiplier": round(total, 3),
        }

    def _trade_cooldown_blocked(self, order: TradeOrder, timeframe: str) -> tuple:
        signal = order.signal or ""
        if order.type == "SELL" and signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL_FULL"):
            return False, ""
        if order.type == "SELL" and "FULL" in signal:
            return False, ""

        pos = get_position(order.symbol, timeframe)
        last_at = pos.get("last_trade_at")
        last_type = pos.get("last_trade_type")
        if not last_at or last_type != order.type:
            return False, ""

        try:
            last_ts = datetime.fromisoformat(str(last_at).replace("Z", ""))
        except Exception:
            return False, ""

        params = self.config.strategy_params(order.symbol, timeframe)
        if order.type == "BUY":
            min_hours = float(params.get("min_hours_between_buys", self.config.trade_cooldown_hours))
        else:
            min_hours = float(params.get("min_hours_between_sells", self.config.trade_cooldown_hours))

        elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        if elapsed < min_hours:
            return True, (
                f"Trade cooldown: {elapsed:.1f}h since last {order.type} "
                f"(min {min_hours:.1f}h)"
            )
        return False, ""

    def _daily_trades_count(self) -> int:
        cutoff = datetime.now() - timedelta(hours=24)
        count = 0
        for history_fn in (load_trade_history, load_live_trade_history):
            for trade in history_fn().get("trades", []):
                try:
                    ts = datetime.fromisoformat(trade.get("timestamp", "").replace("Z", ""))
                except Exception:
                    continue
                if ts >= cutoff:
                    count += 1
        return count