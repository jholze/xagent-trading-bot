import os
from datetime import datetime

import ccxt

from core.config import BotConfig, get_bot_config
from core.models import TradeOrder, TradeResult
from data_manager import record_live_trade
from execution.base import ExecutionAdapter
from logger import log
from services.portfolio_service import PortfolioService


class GateExecutionAdapter(ExecutionAdapter):
    """Gate.io execution via ccxt (mainnet or testnet)."""

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        testnet: bool = False,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.testnet = testnet
        self.live_cfg = (
            self.config.gate_testnet_config if testnet else self.config.live_config
        )
        self._exchange = None

    @property
    def mode(self) -> str:
        return "gate_testnet" if self.testnet else "live"

    def _get_exchange(self):
        if self._exchange:
            return self._exchange
        api_key = os.getenv(self.live_cfg.get("api_key_env", "GATE_API_KEY"), "")
        secret_env = self.live_cfg.get("api_secret_env", "GATE_API_SECRET")
        api_secret = os.getenv(secret_env, "")
        if not api_key or not api_secret:
            return None
        self._exchange = ccxt.gate({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 20000,
        })
        if self.testnet:
            self._exchange.set_sandbox_mode(True)
        return self._exchange

    def _max_usdt(self) -> float:
        return float(
            self.live_cfg.get("max_usdt_per_trade", self.config.max_usdt_per_trade)
        )

    def _fetch_usdt_balance(self) -> float:
        exchange = self._get_exchange()
        if not exchange:
            return 0.0
        try:
            balance = exchange.fetch_balance()
            return float(
                balance.get("USDT", {}).get("free", 0)
                or balance.get("free", {}).get("USDT", 0)
                or 0
            )
        except Exception as e:
            label = "Gate testnet" if self.testnet else "Gate"
            log(f"{label} balance fetch failed: {e}", "WARNING")
            return 0.0

    def execute(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        dry_default = not self.testnet
        if self.live_cfg.get("dry_run", dry_default):
            label = "Gate testnet" if self.testnet else "Live"
            log(
                f"[DRY RUN] {label} {order.type} {order.symbol} "
                f"amount={order.amount} @ {order.price}",
                "INFO",
            )
            result = self._sync_local_ledger(order, timeframe, exchange_order_id="dry_run")
            target = "Gate testnet" if self.testnet else "Gate.io"
            result.message = f"Dry run — order logged locally, not sent to {target}"
            return result

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
            if order.type == "BUY":
                return self._execute_buy(exchange, order, timeframe)
            return self._execute_sell(exchange, order, timeframe)
        except Exception as e:
            label = "Gate testnet" if self.testnet else "Gate"
            log(f"{label} execution failed for {order.symbol}: {e}", "ERROR")
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

        raw = exchange.create_market_buy_order(order.symbol, amount)
        fill_price = float(raw.get("average") or raw.get("price") or order.price)
        filled = float(raw.get("filled") or amount)
        cost = float(raw.get("cost") or fill_price * filled)

        result = self._sync_local_ledger(
            TradeOrder("BUY", order.symbol, fill_price, filled, usdt_amount=cost, signal=order.signal),
            timeframe,
            exchange_order_id=raw.get("id", ""),
        )
        prefix = "Gate testnet" if self.testnet else "Gate"
        result.message = f"{prefix} BUY filled {filled:.6f} @ ${fill_price:.4f}"
        return result

    def _fetch_base_balance(self, exchange, symbol: str) -> float:
        base = symbol.split("/")[0]
        try:
            balance = exchange.fetch_balance()
            return float(
                balance.get(base, {}).get("free", 0)
                or balance.get("free", {}).get(base, 0)
                or 0
            )
        except Exception as e:
            label = "Gate testnet" if self.testnet else "Gate"
            log(f"{label} {base} balance fetch failed: {e}", "WARNING")
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

        raw = exchange.create_market_sell_order(order.symbol, amount)
        fill_price = float(raw.get("average") or raw.get("price") or order.price)
        filled = float(raw.get("filled") or amount)
        received = float(raw.get("cost") or fill_price * filled)

        result = self._sync_local_ledger(
            TradeOrder("SELL", order.symbol, fill_price, filled, signal=order.signal),
            timeframe,
            exchange_order_id=raw.get("id", ""),
            usdt_received=received,
        )
        prefix = "Gate testnet" if self.testnet else "Gate"
        result.message = f"{prefix} SELL filled {filled:.6f} @ ${fill_price:.4f}"
        return result

    def _sync_local_ledger(
        self,
        order: TradeOrder,
        timeframe: str,
        exchange_order_id: str = "",
        usdt_received: float = 0,
    ) -> TradeResult:
        if order.type == "BUY":
            local = self.portfolio.execute_buy(
                order.symbol, timeframe, order.price, order.usdt_amount
            )
        else:
            local = self.portfolio.execute_sell(
                order.symbol, timeframe, order.price, order.signal or "SELL", order.amount
            )

        record_live_trade({
            "type": order.type,
            "symbol": order.symbol,
            "price": order.price,
            "amount": order.amount,
            "usdt_amount": order.usdt_amount,
            "usdt_received": usdt_received or local.usdt_amount,
            "pnl": local.pnl,
            "exchange_order_id": exchange_order_id,
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
        })
        local.message = local.message or f"{self.mode} {order.type} synced"
        return local