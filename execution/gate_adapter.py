import os
import uuid
from datetime import datetime

import ccxt

from core.config import BotConfig, get_bot_config
from core.costs import COST_MODEL_VERSION, CostModel, Fill, trade_cost_fields
from core.models import TradeOrder, TradeResult
from data_manager import record_live_trade, uses_exchange_ledger
from execution.base import ExecutionAdapter
from logger import log
from services.portfolio_service import PortfolioService

_GATE_TESTNET_HOST = "https://fx-api-testnet.gateio.ws"


class GateExecutionAdapter(ExecutionAdapter):
    """Gate.io Spot execution via ccxt.

    ``mode`` is ``shadow`` | ``testnet`` | ``real``. Shadow runs precision,
    limits and balance checks then synthesises a fill — it never calls
    ``create_*_order``.
    """

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        mode: str | None = None,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.live_cfg = self.config.live_config
        if mode is None:
            from core.execution_mode import resolve_execution_mode

            mode = resolve_execution_mode(self.config.raw).adapter_mode
        mode_n = str(mode).strip().lower()
        if mode_n not in ("shadow", "testnet", "real"):
            raise RuntimeError(
                f"Unknown GateExecutionAdapter mode={mode!r}; expected shadow|testnet|real"
            )
        self._adapter_mode = mode_n
        self._exchange = None
        self._last_api_error = ""

    @property
    def mode(self) -> str:
        return self._adapter_mode

    def _get_exchange(self):
        if self._exchange:
            return self._exchange
        api_key = os.getenv(self.live_cfg.get("api_key_env", "GATE_API_KEY"), "")
        secret_env = self.live_cfg.get("api_secret_env", "GATE_API_SECRET")
        api_secret = os.getenv(secret_env, "")
        params = {"enableRateLimit": True, "timeout": 20000}
        if self._adapter_mode == "shadow":
            if api_key and api_secret:
                params["apiKey"] = api_key
                params["secret"] = api_secret
            self._exchange = ccxt.gate(params)
            return self._exchange
        if not api_key or not api_secret:
            return None
        params["apiKey"] = api_key
        params["secret"] = api_secret
        self._exchange = ccxt.gate(params)
        if self._adapter_mode == "testnet":
            self._apply_testnet(self._exchange)
        return self._exchange

    @staticmethod
    def _apply_testnet(exchange) -> None:
        setter = getattr(exchange, "set_sandbox_mode", None)
        if callable(setter):
            setter(True)
            return
        urls = getattr(exchange, "urls", None)
        if isinstance(urls, dict):
            urls["api"] = _GATE_TESTNET_HOST

    def _max_usdt(self) -> float:
        return float(
            self.live_cfg.get("max_usdt_per_trade", self.config.max_usdt_per_trade)
        )

    def _fetch_usdt_balance(self) -> float:
        if self._adapter_mode == "shadow":
            return self._simulated_usdt_balance()
        exchange = self._get_exchange()
        if not exchange:
            return 0.0
        try:
            balance = exchange.fetch_balance()
            self._last_api_error = ""
            return float(
                balance.get("USDT", {}).get("free", 0)
                or balance.get("free", {}).get("USDT", 0)
                or 0
            )
        except Exception as e:
            self._last_api_error = str(e)
            log(f"Gate balance fetch failed: {e}", "WARNING")
            return 0.0

    def _simulated_usdt_balance(self) -> float:
        try:
            from data_manager import resolve_sim_cash_balance

            return float(resolve_sim_cash_balance(config=self.config.raw))
        except Exception as e:
            self._last_api_error = str(e)
            log(f"Shadow USDT balance from ledger failed: {e}", "WARNING")
            return 0.0

    def execute(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        exchange = self._get_exchange()
        if not exchange:
            key_env = self.live_cfg.get("api_key_env", "GATE_API_KEY")
            secret_env = self.live_cfg.get("api_secret_env", "GATE_API_SECRET")
            return TradeResult(
                executed=False,
                order_type=order.type,
                symbol=order.symbol,
                message=f"Gate API keys not configured ({key_env} / {secret_env})",
            )

        try:
            if order.type in ("SHORT", "COVER"):
                return TradeResult(
                    executed=False,
                    order_type=order.type,
                    symbol=order.symbol,
                    message="shorts.allow_live=false — no Gate futures in v0",
                )
            if order.type == "BUY":
                return self._execute_buy(exchange, order, timeframe)
            if order.type == "SELL":
                return self._execute_sell(exchange, order, timeframe)
            return TradeResult(
                executed=False,
                order_type=order.type,
                symbol=order.symbol,
                message=f"Unsupported Gate order type {order.type}",
            )
        except Exception as e:
            log(f"Gate execution failed for {order.symbol}: {e}", "ERROR")
            return TradeResult(
                executed=False,
                order_type=order.type,
                symbol=order.symbol,
                message=str(e)[:120],
            )

    def _execute_buy(self, exchange, order: TradeOrder, timeframe: str) -> TradeResult:
        usdt = order.usdt_amount or self._max_usdt()
        balance = self._fetch_usdt_balance()
        if balance < usdt:
            return TradeResult(
                False,
                "BUY",
                order.symbol,
                message=f"Insufficient USDT balance ({balance:.2f})",
            )

        amount = usdt / order.price if order.price > 0 else order.amount
        amount = float(exchange.amount_to_precision(order.symbol, amount))

        if self._adapter_mode == "shadow":
            raw = self._synthesize_shadow_raw(order, side="buy", amount=amount, usdt=usdt)
        else:
            raw = exchange.create_market_buy_order(order.symbol, amount)
        fill_price = float(raw.get("average") or raw.get("price") or order.price)
        filled = float(raw.get("filled") or amount)
        cost = float(raw.get("cost") or fill_price * filled)

        # Cost path (#301): parse fee currency via Fill. Fill-status / partials are #313.
        fill = self._fill_from_raw(raw, order, side="buy")
        result = self._sync_local_ledger(
            TradeOrder(
                "BUY", order.symbol, fill_price, filled, usdt_amount=cost,
                signal=order.signal, source=order.source, order_id=order.order_id,
            ),
            timeframe,
            exchange_order_id=str(raw.get("id", "")),
            fill=fill,
        )
        from price_fetcher import format_usdt_price

        result.message = f"Gate BUY filled {filled:.6f} @ {format_usdt_price(fill_price)}"
        result.exchange_order_id = str(raw.get("id", ""))
        result.fee = fill.fee_usdt if fill is not None else 0.0
        return result

    def _fetch_base_balance(self, exchange, symbol: str) -> float:
        if self._adapter_mode == "shadow":
            return self._simulated_base_balance(symbol)
        base = symbol.split("/")[0]
        try:
            balance = exchange.fetch_balance()
            return float(
                balance.get(base, {}).get("free", 0)
                or balance.get("free", {}).get(base, 0)
                or 0
            )
        except Exception as e:
            log(f"Gate {base} balance fetch failed: {e}", "WARNING")
            return 0.0

    def _simulated_base_balance(self, symbol: str) -> float:
        try:
            from strategies.positions import list_active_positions

            total = 0.0
            base = symbol.split("/")[0]
            for pos in list_active_positions():
                psym = str(pos.get("symbol") or "")
                if psym == symbol or psym == base:
                    total += float(pos.get("amount") or 0)
            return total
        except Exception as e:
            log(f"Shadow base balance from ledger failed for {symbol}: {e}", "WARNING")
            return 0.0

    def _validate_sell_amount(self, exchange, order: TradeOrder, amount: float) -> tuple:
        if amount <= 0:
            return 0.0, "No amount to sell"

        exchange_balance = self._fetch_base_balance(exchange, order.symbol)
        if exchange_balance > 0 and amount > exchange_balance:
            log(
                f"Sell amount capped: ledger {amount:.6f} > exchange {exchange_balance:.6f} "
                f"for {order.symbol}",
                "WARNING",
            )
            amount = exchange_balance

        try:
            markets = exchange.load_markets()
            market = markets.get(order.symbol) or {}
            min_amount = float(
                market.get("limits", {}).get("amount", {}).get("min", 0) or 0
            )
            min_cost = float(
                market.get("limits", {}).get("cost", {}).get("min", 0) or 0
            )
            amount = float(exchange.amount_to_precision(order.symbol, amount))
            if min_amount and amount < min_amount:
                return 0.0, f"Amount {amount:.6f} below Gate minimum ({min_amount})"
            if min_cost and order.price > 0 and amount * order.price < min_cost:
                return 0.0, f"Order value below Gate minimum (${min_cost:.2f})"
        except Exception as e:
            log(f"Gate market limits check failed for {order.symbol}: {e}", "WARNING")
            amount = float(exchange.amount_to_precision(order.symbol, amount))

        return amount, ""

    def _execute_sell(self, exchange, order: TradeOrder, timeframe: str) -> TradeResult:
        amount = order.amount
        if amount <= 0:
            return TradeResult(False, "SELL", order.symbol, message="No amount to sell")

        amount, error = self._validate_sell_amount(exchange, order, amount)
        if error:
            return TradeResult(False, "SELL", order.symbol, message=error)

        if self._adapter_mode == "shadow":
            raw = self._synthesize_shadow_raw(order, side="sell", amount=amount)
        else:
            raw = exchange.create_market_sell_order(order.symbol, amount)
        fill_price = float(raw.get("average") or raw.get("price") or order.price)
        filled = float(raw.get("filled") or amount)
        received = float(raw.get("cost") or fill_price * filled)

        # Cost path (#301): parse fee currency via Fill. Fill-status / partials are #313.
        fill = self._fill_from_raw(raw, order, side="sell")
        result = self._sync_local_ledger(
            TradeOrder(
                "SELL", order.symbol, fill_price, filled, signal=order.signal,
                source=order.source, order_id=order.order_id,
            ),
            timeframe,
            exchange_order_id=str(raw.get("id", "")),
            usdt_received=received,
            fill=fill,
        )
        from price_fetcher import format_usdt_price

        result.message = f"Gate SELL filled {filled:.6f} @ {format_usdt_price(fill_price)}"
        result.exchange_order_id = str(raw.get("id", ""))
        result.fee = fill.fee_usdt if fill is not None else 0.0
        return result

    def _synthesize_shadow_raw(
        self,
        order: TradeOrder,
        *,
        side: str,
        amount: float,
        usdt: float | None = None,
    ) -> dict:
        """CostModel fill → ccxt-shaped dict. No create_*_order."""
        cm = CostModel.from_config(self.config, symbol=order.symbol)
        if side == "buy":
            if usdt and usdt > 0:
                fill = cm.simulate_buy(float(order.price), usdt=float(usdt))
            else:
                fill = cm.simulate_buy(float(order.price), qty=float(amount))
        else:
            fill = cm.simulate_sell(float(order.price), float(amount))
        base, _, quote = str(order.symbol).partition("/")
        quote = quote or "USDT"
        if fill.fee_base:
            fee_cost, fee_ccy = fill.fee_base, base
        else:
            fee_cost, fee_ccy = fill.fee_quote, quote
        return {
            "id": f"shadow-{uuid.uuid4()}",
            "status": "closed",
            "average": fill.fill_price,
            "filled": fill.qty_gross,
            "cost": fill.quote_gross,
            "fee": {"cost": fee_cost, "currency": fee_ccy},
        }

    def _fill_from_raw(self, raw: dict, order: TradeOrder, *, side: str) -> Fill:
        """ccxt raw → Fill. Raises ValueError on unknown fee currency (never guess)."""
        base, _, quote = str(order.symbol).partition("/")
        cm = CostModel.from_config(self.config, symbol=order.symbol)
        return cm.fill_from_exchange(
            raw,
            side=side,  # type: ignore[arg-type]
            base=base,
            quote=quote or "USDT",
            request_price=float(order.price or 0),
            order_type="market",
        )

    def _sync_local_ledger(
        self,
        order: TradeOrder,
        timeframe: str,
        exchange_order_id: str = "",
        usdt_received: float = 0,
        fill: Fill | None = None,
    ) -> TradeResult:
        oid = order.order_id or None
        sync_virtual = not uses_exchange_ledger(self.config.trading_mode)
        # Dry-run / no exchange raw: simulate so ledger and P&L share one Fill.
        if fill is None and order.type in ("BUY", "SELL") and order.price > 0:
            cm = CostModel.from_config(self.config, symbol=order.symbol)
            if order.type == "BUY":
                usdt = order.usdt_amount or self._max_usdt()
                if usdt > 0:
                    fill = cm.simulate_buy(order.price, usdt=usdt)
            elif order.amount and order.amount > 0:
                fill = cm.simulate_sell(order.price, order.amount)
        if order.type == "BUY":
            local = self.portfolio.execute_buy(
                order.symbol,
                timeframe,
                order.price,
                order.usdt_amount,
                source=order.source,
                order_id=oid,
                sync_virtual_ledger=sync_virtual,
                entry_15m_vol_ratio=order.entry_15m_vol_ratio,
                fill=fill,
            )
        elif order.type == "SHORT":
            local = self.portfolio.execute_short(
                order.symbol,
                timeframe,
                order.price,
                order.usdt_amount,
                source=order.source,
                order_id=oid,
                leverage=getattr(order, "leverage", None),
                sync_virtual_ledger=sync_virtual,
            )
        elif order.type == "COVER":
            local = self.portfolio.execute_cover(
                order.symbol,
                timeframe,
                order.price,
                order.amount,
                source=order.source,
                order_id=oid,
                sync_virtual_ledger=sync_virtual,
            )
        elif order.type == "SELL":
            local = self.portfolio.execute_sell(
                order.symbol, timeframe, order.price, order.signal or "SELL", order.amount,
                source=order.source, order_id=oid, sync_virtual_ledger=sync_virtual,
                fill=fill,
            )
        else:
            return TradeResult(False, order.type, order.symbol, message=f"Unknown type {order.type}")

        rec = {
            "type": order.type,
            "symbol": order.symbol,
            "price": order.price,
            "amount": local.amount,
            "usdt_amount": local.usdt_amount or order.usdt_amount,
            "usdt_received": usdt_received or local.usdt_amount,
            "pnl": local.pnl,
            "exchange_order_id": exchange_order_id,
            "order_id": oid,
            "fee": fill.fee_usdt if fill is not None else 0.0,
            "source": order.source,
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
            "cost_model": COST_MODEL_VERSION,
        }
        if fill is not None:
            rec.update(trade_cost_fields(fill))
        record_live_trade(rec)
        local.message = local.message or f"{self.mode} {order.type} synced"
        local.exchange_order_id = exchange_order_id
        return local