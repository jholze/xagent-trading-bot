from datetime import datetime, timedelta

from core.config import BotConfig, get_bot_config
from core.models import RiskDecision, TradeOrder
from data_manager import (
    is_dry_run_enhanced,
    is_live_dry_run,
    load_live_trade_history,
    load_trade_history,
    simulated_balance_usdt,
    uses_exchange_ledger,
)
from services.gate_balance import fetch_portfolio_equity, fetch_usdt_balance
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from strategies.positions import count_open_positions, get_position, list_active_positions


def _is_emergency_sell(signal: str) -> bool:
    signal = signal or ""
    return signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL_FULL") or "STOP" in signal


def _is_partial_sell(signal: str) -> bool:
    signal = signal or ""
    if _is_emergency_sell(signal):
        return False
    if "FULL" in signal:
        return False
    return "PARTIAL" in signal or signal in ("SELL", "SELL_10", "SELL_20", "SELL_30", "SELL_TP")


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
            blocked, reason = self._trade_cooldown_blocked(order, timeframe, source=source)
            if blocked:
                return RiskDecision(approved=False, message=reason, code="trade_cooldown")
            order = self._resolve_sell_order(order, timeframe, source)
            if order.amount <= 0:
                return RiskDecision(approved=False, message="No amount to sell", code="no_amount")
            partial_block, partial_reason = self._partial_sell_blocked(order, timeframe, source)
            if partial_block:
                return RiskDecision(approved=False, message=partial_reason, code="partial_sell_guard")
            max_daily_sells = self._effective_max_daily_sells()
            daily_sells = self._daily_sells_count()
            if max_daily_sells > 0 and daily_sells >= max_daily_sells:
                return RiskDecision(
                    approved=False,
                    message=f"Daily sell limit reached ({daily_sells}/{max_daily_sells})",
                    code="max_daily_sells",
                )
            return RiskDecision(approved=True, order=order, message="Sell approved")

        if order.price <= 0:
            return RiskDecision(approved=False, message="Invalid price")

        blocked, reason = self._trade_cooldown_blocked(order, timeframe, source=source)
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

        daily_buys = self._daily_buys_count()
        max_daily_buys = self._effective_max_daily_buys()
        if max_daily_buys > 0 and daily_buys >= max_daily_buys:
            return RiskDecision(
                approved=False,
                message=f"Daily buy limit reached ({daily_buys}/{max_daily_buys})",
                code="max_daily_trades",
            )

        base_usdt = order.usdt_amount or self._base_usdt_cap()
        if source == "manual":
            # Telegram /buy amounts are explicit user intent — don't shrink via auto-trade multipliers.
            sized = base_usdt
            factors = {
                "trust_factor": 1.0,
                "conf_factor": 1.0,
                "atr_factor": 1.0,
                "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                "drawdown_multiplier": 1.0,
                "total_multiplier": 1.0,
            }
        else:
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

        equity = self._portfolio_equity(order.price, order.symbol)
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

        balance = self._available_usdt(equity)
        if sized > balance:
            sized = balance
            factors["balance_capped"] = True

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

        resolved_source = order.source if order.source not in ("", "auto") else source
        approved = TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=order.amount,
            usdt_amount=round(sized, 2),
            signal=order.signal,
            source=resolved_source,
            order_id=order.order_id,
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
        history = self._primary_history()
        equity = self._portfolio_equity(current_price or 0)
        initial = self._initial_capital()
        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(self.config.risk_config.get("drawdown_throttle_pct", 10.0))
        cash = self._available_usdt(equity)
        return {
            "open_positions": count_open_positions(),
            "max_open_positions": self.config.max_open_positions,
            "daily_trades": self._daily_trades_count(),
            "daily_buys": self._daily_buys_count(),
            "daily_sells": self._daily_sells_count(),
            "max_daily_trades": self._effective_max_daily_buys(),
            "max_daily_buys": self._effective_max_daily_buys(),
            "max_daily_sells": self._effective_max_daily_sells(),
            "max_position_percent": self.config.max_position_percent,
            "base_usdt_per_trade": self._base_usdt_cap(),
            "portfolio_equity": round(equity, 2),
            "initial_capital": initial,
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_throttle_active": drawdown_pct >= throttle_at,
            "virtual_balance": cash,
            "ledger_source": self._ledger_source_label(),
        }

    def _ledger_source_label(self) -> str:
        if is_live_dry_run(self.config.raw):
            return "simulated"
        if uses_exchange_ledger(self.config.trading_mode):
            return "gate"
        return "paper"

    def _effective_max_daily_buys(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_buys") is not None:
                return int(defaults["max_daily_buys"])
            if defaults.get("max_daily_trades") is not None:
                return int(defaults["max_daily_trades"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_buys") is not None:
            return int(risk_cfg["max_daily_buys"])
        return self.config.max_daily_trades

    def _effective_max_daily_sells(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_sells") is not None:
                return int(defaults["max_daily_sells"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_sells") is not None:
            return int(risk_cfg["max_daily_sells"])
        return int(self.config.raw.get("max_daily_sells", 0))

    def _effective_max_daily_trades(self) -> int:
        """Backward-compatible alias for buy limit."""
        return self._effective_max_daily_buys()

    def _base_usdt_cap(self) -> float:
        if self.config.trading_mode == "live":
            return float(
                self.config.live_config.get("max_usdt_per_trade", self.config.max_usdt_per_trade)
            )
        return self.config.max_usdt_per_trade

    def _initial_capital(self) -> float:
        paper = self.config.paper_config.get("initial_capital_usdt")
        if paper:
            return float(paper)
        return float(self.config.raw.get("initial_capital_usdt", 5000))

    def _primary_history(self) -> dict:
        if uses_exchange_ledger(self.config.trading_mode):
            return load_live_trade_history()
        return load_trade_history()

    def _available_usdt(self, fallback: float = 0) -> float:
        if is_live_dry_run(self.config.raw):
            history = load_live_trade_history()
            return float(history.get("virtual_balance", simulated_balance_usdt(self.config.raw)))
        if uses_exchange_ledger(self.config.trading_mode):
            return fetch_usdt_balance(self.config)
        return float(load_trade_history().get("virtual_balance", fallback))

    def _dry_run_reference_prices(self, reference_price: float = 0, symbol: str = None) -> dict:
        ref_prices = {}
        for pos in list_active_positions():
            sym = pos["symbol"] if "/" in pos["symbol"] else f"{pos['symbol']}/USDT"
            ref_prices[sym] = float(pos.get("average_entry", pos.get("entry_price", 0)) or 0)
        if reference_price > 0 and symbol:
            ref_prices[symbol] = reference_price
        return ref_prices

    def _portfolio_equity(self, reference_price: float = 0, symbol: str = None) -> float:
        if is_live_dry_run(self.config.raw):
            return fetch_portfolio_equity(
                self.config,
                reference_prices=self._dry_run_reference_prices(reference_price, symbol),
            )
        if uses_exchange_ledger(self.config.trading_mode):
            return fetch_portfolio_equity(self.config)
        history = load_trade_history()
        balance = float(history.get("virtual_balance", self._initial_capital()))
        unrealized = 0.0
        for pos in list_active_positions():
            entry = pos.get("average_entry", 0) or pos.get("entry_price", 0)
            price = reference_price if reference_price > 0 else entry
            unrealized += (price - entry) * pos.get("amount", 0)
        return max(balance + unrealized, balance)

    def _equity_drawdown_pct(self, reference_price: float = 0, symbol: str = None) -> float:
        if is_live_dry_run(self.config.raw):
            history = load_live_trade_history()
            initial = simulated_balance_usdt(self.config.raw)
            equity = self._portfolio_equity(reference_price, symbol)
        else:
            history = load_trade_history()
            initial = self._initial_capital()
            equity = self._portfolio_equity(reference_price, symbol)
        peak = float(history.get("peak_equity", initial))
        peak = max(peak, equity, initial)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

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

    def _partial_sell_limits(self, symbol: str, timeframe: str) -> dict:
        params = self.config.strategy_params(symbol, timeframe)
        cmc_cfg = self.config.cmc_config
        risk_cfg = self.config.risk_config
        return {
            "min_position_usdt": float(
                params.get("min_position_usdt_for_partial_sell")
                or params.get("min_position_usdt_for_social_sell")
                or risk_cfg.get("min_position_usdt_for_partial_sell")
                or cmc_cfg.get("min_position_usdt_for_social_sell", 25)
            ),
            "min_notional_usdt": float(
                params.get("min_sell_notional_usdt")
                or risk_cfg.get("min_sell_notional_usdt")
                or cmc_cfg.get("min_sell_notional_usdt", 15)
            ),
            "max_sold_percent": float(
                params.get("block_partial_sell_if_sold_percent_above")
                or params.get("block_social_sell_if_sold_percent_above")
                or risk_cfg.get("block_partial_sell_if_sold_percent_above")
                or cmc_cfg.get("block_social_sell_if_sold_percent_above", 0.75)
            ),
            "dust_sweep_max_position_usdt": float(
                risk_cfg.get("dust_sweep_max_position_usdt", 15)
            ),
            "dust_sweep_sold_percent_min": float(
                risk_cfg.get("dust_sweep_sold_percent_min", 0.70)
            ),
            "dust_sweep_min_remainder_usdt": float(
                risk_cfg.get("dust_sweep_min_remainder_usdt", 10)
            ),
        }

    def _resolve_sell_order(self, order: TradeOrder, timeframe: str, source: str) -> TradeOrder:
        """Upgrade partial sells to full close when the lot is dust or nearly exited."""
        if source == "manual" or _is_emergency_sell(order.signal):
            return order
        if not _is_partial_sell(order.signal) or order.price <= 0:
            return order

        pos = get_position(order.symbol, timeframe)
        amount = float(pos.get("amount", 0))
        if amount <= 0:
            return order

        pos_value = amount * order.price
        sold_pct = float(pos.get("sold_percent", 0))
        limits = self._partial_sell_limits(order.symbol, timeframe)
        notional = float(order.amount) * order.price
        remainder = pos_value - notional

        sweep = (
            pos_value <= limits["dust_sweep_max_position_usdt"]
            or (
                sold_pct >= limits["dust_sweep_sold_percent_min"]
                and pos_value <= limits["min_position_usdt"]
            )
            or (0 < remainder < limits["dust_sweep_min_remainder_usdt"])
        )
        if not sweep:
            return order

        return TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=amount,
            signal="SELL_FULL",
            source=order.source,
            order_id=order.order_id,
            timestamp=order.timestamp,
        )

    def _partial_sell_blocked(self, order: TradeOrder, timeframe: str, source: str) -> tuple[bool, str]:
        if source == "manual" or _is_emergency_sell(order.signal):
            return False, ""
        if not _is_partial_sell(order.signal) or order.price <= 0:
            return False, ""

        limits = self._partial_sell_limits(order.symbol, timeframe)
        pos = get_position(order.symbol, timeframe)
        pos_value = float(pos.get("amount", 0)) * order.price
        notional = float(order.amount) * order.price
        sold_pct = float(pos.get("sold_percent", 0))

        if pos_value < limits["min_position_usdt"]:
            return True, (
                f"Partial sell blocked: position ${pos_value:.2f} "
                f"below minimum ${limits['min_position_usdt']:.0f} "
                f"(use full close or manual sell)"
            )
        if notional < limits["min_notional_usdt"]:
            return True, (
                f"Partial sell blocked: notional ${notional:.2f} "
                f"below minimum ${limits['min_notional_usdt']:.0f}"
            )
        if sold_pct >= limits["max_sold_percent"]:
            return True, (
                f"Partial sell blocked: already sold {sold_pct * 100:.0f}% of position "
                f"(max {limits['max_sold_percent'] * 100:.0f}%)"
            )
        return False, ""

    def _social_sell_blocked(self, order: TradeOrder, timeframe: str, source: str) -> tuple[bool, str]:
        """Backward-compatible alias for tests and callers."""
        return self._partial_sell_blocked(order, timeframe, source)

    def _trade_cooldown_blocked(self, order: TradeOrder, timeframe: str, source: str = "auto") -> tuple:
        if source == "manual":
            return False, ""
        signal = order.signal or ""
        if order.type == "SELL" and signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL_FULL"):
            return False, ""
        if order.type == "SELL" and "FULL" in signal:
            return False, ""

        pos = get_position(order.symbol, timeframe)
        params = self.config.strategy_params(order.symbol, timeframe)
        defaults = self.config.dry_run_defaults if is_dry_run_enhanced(self.config.raw) else {}
        cmc_cfg = self.config.cmc_config

        if order.type == "SELL" and source == "cmc":
            last_cmc = pos.get("last_cmc_sell_at")
            if last_cmc:
                try:
                    last_ts = datetime.fromisoformat(str(last_cmc).replace("Z", ""))
                    min_hours = float(
                        params.get("cmc_min_hours_between_sells")
                        or defaults.get("cmc_min_hours_between_sells")
                        or cmc_cfg.get("cmc_min_hours_between_sells", 6)
                    )
                    elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
                    if elapsed < min_hours:
                        return True, (
                            f"CMC sell cooldown: {elapsed:.1f}h since last CMC sell "
                            f"(min {min_hours:.1f}h)"
                        )
                except Exception:
                    pass

        last_at = pos.get("last_trade_at")
        last_type = pos.get("last_trade_type")
        if not last_at:
            return False, ""

        try:
            last_ts = datetime.fromisoformat(str(last_at).replace("Z", ""))
        except Exception:
            return False, ""

        if order.type == "BUY" and last_type == "SELL":
            blocked, reason = self._rebuy_after_sell_blocked(
                order, timeframe, source, last_ts, params, defaults
            )
            if blocked:
                return True, reason

        if last_type != order.type:
            return False, ""

        if order.type == "BUY":
            min_hours = float(
                params.get("min_hours_between_buys")
                or defaults.get("min_hours_between_buys")
                or defaults.get("trade_cooldown_hours")
                or self.config.trade_cooldown_hours
            )
        else:
            min_hours = float(
                params.get("min_hours_between_sells")
                or defaults.get("min_hours_between_sells")
                or defaults.get("trade_cooldown_hours")
                or self.config.trade_cooldown_hours
            )

        elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        if elapsed < min_hours:
            return True, (
                f"Trade cooldown: {elapsed:.1f}h since last {order.type} "
                f"(min {min_hours:.1f}h)"
            )
        return False, ""

    def _rebuy_after_sell_blocked(
        self,
        order: TradeOrder,
        timeframe: str,
        source: str,
        last_ts: datetime,
        params: dict,
        defaults: dict,
    ) -> tuple[bool, str]:
        if source == "manual":
            return False, ""
        min_hours = float(
            params.get("min_hours_after_sell_before_rebuy")
            or defaults.get("min_hours_after_sell_before_rebuy")
            or self.config.min_hours_after_sell_before_rebuy
        )
        if min_hours <= 0:
            return False, ""
        elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        if elapsed < min_hours:
            return True, (
                f"Rebuy cooldown: {elapsed:.1f}h since last SELL "
                f"(min {min_hours:.1f}h after sell)"
            )
        return False, ""

    @staticmethod
    def _order_side(order: dict) -> str:
        side = str(order.get("side") or order.get("type") or "").lower()
        if side in ("buy", "sell"):
            return side
        return ""

    def _daily_trades_count(self, side: str | None = None) -> int:
        """Count filled ledger orders in the last 24h (optional filter: buy/sell)."""
        from data_manager import load_orders, resolve_ledger_scope

        cutoff = datetime.now() - timedelta(hours=24)
        scope = resolve_ledger_scope(self.config.trading_mode)
        count = 0
        want = (side or "").lower() or None
        for order in load_orders(scope).get("orders", []):
            if order.get("status") != "filled":
                continue
            order_side = self._order_side(order)
            if want and order_side != want:
                continue
            ts_raw = (
                order.get("timestamps", {}).get("filled")
                or order.get("timestamps", {}).get("created")
                or ""
            )
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
            except Exception:
                continue
            if ts >= cutoff:
                count += 1
        return count

    def _daily_buys_count(self) -> int:
        return self._daily_trades_count("buy")

    def _daily_sells_count(self) -> int:
        return self._daily_trades_count("sell")