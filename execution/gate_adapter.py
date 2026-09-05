import os
import uuid
from datetime import datetime

import ccxt

from core.config import BotConfig, get_bot_config
from core.costs import COST_MODEL_VERSION, CostModel, Fill, trade_cost_fields
from core.models import OrderStatus, TradeOrder, TradeResult
from data_manager import record_live_trade, uses_exchange_ledger
from execution.base import ExecutionAdapter
from logger import log
from services.portfolio_service import PortfolioService

_GATE_TESTNET_HOST = "https://fx-api-testnet.gateio.ws"


class GateExecutionAdapter(ExecutionAdapter):
    """Gate.io Spot execution via ccxt.

    ``mode`` is ``shadow`` | ``testnet`` | ``real``. Shadow runs precision,
    limits and balance checks then synthesises a fill — it never calls
    ``create_*_order``. Market metadata in shadow is best-effort and
    process-cached; missing markets never fail a shadow order.
    """

    _shadow_markets_cache: dict | None = None
    _shadow_markets_failed: bool = False
    _shadow_markets_warned: bool = False

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
        self._precision_unverified = False

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
            params["timeout"] = 4000
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

    def _warn_shadow_markets_unavailable(self) -> None:
        cls = type(self)
        if cls._shadow_markets_warned:
            return
        cls._shadow_markets_warned = True
        log("shadow: gate markets unavailable — precision/limits unverified", "WARNING")

    def _ensure_shadow_markets(self, exchange) -> bool:
        """Load Gate markets once per process. False → skip precision/limits."""
        cls = type(self)
        if cls._shadow_markets_failed:
            return False
        existing = getattr(exchange, "markets", None) if exchange is not None else None
        if isinstance(existing, dict) and existing:
            cls._shadow_markets_cache = existing
            return True
        if cls._shadow_markets_cache is not None:
            if exchange is not None:
                try:
                    setter = getattr(exchange, "set_markets", None)
                    if callable(setter):
                        setter(cls._shadow_markets_cache)
                    elif not isinstance(getattr(exchange, "markets", None), dict):
                        exchange.markets = cls._shadow_markets_cache
                except Exception:
                    pass
            return True
        if exchange is None:
            cls._shadow_markets_failed = True
            self._warn_shadow_markets_unavailable()
            return False
        try:
            loaded = exchange.load_markets()
            if not loaded:
                raise RuntimeError("empty markets")
            cls._shadow_markets_cache = loaded
            return True
        except Exception:
            cls._shadow_markets_failed = True
            self._warn_shadow_markets_unavailable()
            return False

    def _shadow_adjust_amount(self, exchange, symbol: str, amount: float) -> tuple[float, bool]:
        if not self._ensure_shadow_markets(exchange):
            return float(amount), False
        try:
            return float(exchange.amount_to_precision(symbol, amount)), True
        except Exception:
            return float(amount), False

    def _shadow_cap_sell(self, exchange, order: TradeOrder, amount: float) -> float:
        balance = self._fetch_base_balance(exchange, order.symbol)
        if balance > 0 and amount > balance:
            log(
                f"Sell amount capped: ledger {amount:.6f} > exchange {balance:.6f} "
                f"for {order.symbol}",
                "WARNING",
            )
            return balance
        return amount

    def execute(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        self._precision_unverified = False
        exchange = None
        try:
            exchange = self._get_exchange()
        except Exception as e:
            if self._adapter_mode != "shadow":
                log(f"Gate execution failed for {order.symbol}: {e}", "ERROR")
                return self._active_reconcile_result(order, str(e)[:120])
            log(f"Shadow exchange init failed: {e}", "WARNING")
        if not exchange and self._adapter_mode != "shadow":
            key_env = self.live_cfg.get("api_key_env", "GATE_API_KEY")
            secret_env = self.live_cfg.get("api_secret_env", "GATE_API_SECRET")
            return self._rejected_result(
                order,
                f"Gate API keys not configured ({key_env} / {secret_env})",
            )

        try:
            if order.type in ("SHORT", "COVER"):
                return self._rejected_result(
                    order, "shorts.allow_live=false — no Gate futures in v0"
                )
            if order.type == "BUY":
                return self._execute_buy(exchange, order, timeframe)
            if order.type == "SELL":
                return self._execute_sell(exchange, order, timeframe)
            return self._rejected_result(
                order, f"Unsupported Gate order type {order.type}"
            )
        except Exception as e:
            # Last resort: never mark failed. ACTIVE + needs_reconcile (#314).
            log(f"Gate execution failed for {order.symbol}: {e}", "ERROR")
            return self._active_reconcile_result(order, str(e)[:120])

    def _execute_buy(self, exchange, order: TradeOrder, timeframe: str) -> TradeResult:
        usdt = order.usdt_amount or self._max_usdt()
        balance = self._fetch_usdt_balance()
        if balance < usdt:
            return self._rejected_result(
                order, f"Insufficient USDT balance ({balance:.2f})"
            )

        amount = usdt / order.price if order.price > 0 else float(order.qty or 0)
        if self._adapter_mode == "shadow":
            amount, verified = self._shadow_adjust_amount(exchange, order.symbol, amount)
            self._precision_unverified = not verified
        else:
            amount = float(exchange.amount_to_precision(order.symbol, amount))
        if not order.qty:
            order.qty = amount

        params = self._client_order_params(order)
        create_attempted = False
        if self._adapter_mode == "shadow":
            raw = self._synthesize_shadow_raw(order, side="buy", amount=amount, usdt=usdt)
        else:
            cost = float(usdt)
            cost_fn = getattr(exchange, "cost_to_precision", None)
            if callable(cost_fn):
                try:
                    cost = float(cost_fn(order.symbol, cost))
                except Exception:
                    cost = float(usdt)
            try:
                create_attempted = True
                # Gate requires quote cost for market buys. Never flip
                # createMarketBuyOrderRequiresPrice — that reinterprets amount
                # as quote cost.
                raw = exchange.create_market_buy_order_with_cost(
                    order.symbol, cost, params
                )
            except Exception as e:
                return self._handle_create_exception(
                    e,
                    exchange,
                    order,
                    create_attempted=create_attempted,
                    timeframe=timeframe,
                    side="buy",
                    qty=amount,
                    usdt=usdt,
                )
        return self._finalize_exchange_order(
            exchange, order, raw, side="buy", qty=amount, timeframe=timeframe, usdt=usdt
        )

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
        amount = float(order.qty or 0)
        if amount <= 0:
            return self._rejected_result(order, "No amount to sell")

        if self._adapter_mode == "shadow":
            if self._ensure_shadow_markets(exchange):
                try:
                    amount, error = self._validate_sell_amount(exchange, order, amount)
                    if error:
                        return self._rejected_result(order, error)
                    self._precision_unverified = False
                except Exception:
                    amount = self._shadow_cap_sell(exchange, order, amount)
                    self._precision_unverified = True
            else:
                amount = self._shadow_cap_sell(exchange, order, amount)
                self._precision_unverified = True
        else:
            amount, error = self._validate_sell_amount(exchange, order, amount)
            if error:
                return self._rejected_result(order, error)

        params = self._client_order_params(order)
        create_attempted = False
        if self._adapter_mode == "shadow":
            raw = self._synthesize_shadow_raw(order, side="sell", amount=amount)
        else:
            try:
                create_attempted = True
                raw = exchange.create_market_sell_order(order.symbol, amount, params)
            except TypeError:
                try:
                    raw = exchange.create_market_sell_order(
                        order.symbol, amount, None, params
                    )
                except Exception as e:
                    return self._handle_create_exception(
                        e,
                        exchange,
                        order,
                        create_attempted=True,
                        timeframe=timeframe,
                        side="sell",
                        qty=amount,
                    )
            except Exception as e:
                return self._handle_create_exception(
                    e,
                    exchange,
                    order,
                    create_attempted=create_attempted,
                    timeframe=timeframe,
                    side="sell",
                    qty=amount,
                )
        return self._finalize_exchange_order(
            exchange, order, raw, side="sell", qty=amount, timeframe=timeframe
        )

    def _places_on_exchange(self) -> bool:
        return self._adapter_mode in ("real", "testnet")

    def _client_order_params(self, order: TradeOrder) -> dict:
        key = (order.client_order_id or order.idempotency_key or "").strip()
        if not key:
            key = str(uuid.uuid4())
        order.client_order_id = key
        if not order.idempotency_key:
            order.idempotency_key = key
        return {"text": f"t-{key}"}

    def _hard_reject_types(self) -> tuple:
        names = ("InsufficientFunds", "InvalidOrder", "BadSymbol")
        return tuple(cls for n in names if isinstance((cls := getattr(ccxt, n, None)), type))

    def _uncertain_types(self) -> tuple:
        names = ("RateLimitExceeded", "NetworkError", "RequestTimeout")
        return tuple(cls for n in names if isinstance((cls := getattr(ccxt, n, None)), type))

    def _handle_create_exception(
        self,
        exc: Exception,
        exchange,
        order: TradeOrder,
        *,
        create_attempted: bool,
        timeframe: str = "4h",
        side: str = "buy",
        qty: float = 0.0,
        usdt: float = 0.0,
    ) -> TradeResult:
        hard = self._hard_reject_types()
        uncertain = self._uncertain_types()
        if hard and isinstance(exc, hard):
            return self._rejected_result(order, str(exc)[:200] or exc.__class__.__name__)
        if create_attempted and uncertain and isinstance(exc, uncertain):
            found = self._recover_after_uncertain_create(exchange, order)
            if found is not None:
                return self._finalize_exchange_order(
                    exchange,
                    order,
                    found,
                    side=side,
                    qty=qty or float(order.qty or 0),
                    timeframe=timeframe,
                    usdt=usdt or order.usdt_amount,
                )
            return self._rejected_result(order, "not placed")
        raise exc

    def _order_matches_client_id(self, raw: dict, key: str) -> bool:
        if not key or not isinstance(raw, dict):
            return False
        text = f"t-{key}"
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        candidates = (
            raw.get("clientOrderId"),
            raw.get("clientOrderID"),
            raw.get("client_order_id"),
            raw.get("text"),
            info.get("text") if info else None,
            info.get("client_order_id") if info else None,
        )
        key_l = str(key).lower()
        text_l = text.lower()
        for c in candidates:
            if c is None:
                continue
            cs = str(c).lower()
            if cs in (key_l, text_l):
                return True
        return False

    def _recover_after_uncertain_create(self, exchange, order: TradeOrder) -> dict | None:
        """fetch_open_orders then fetch_order by client_order_id. Never resend."""
        key = order.client_order_id or order.idempotency_key
        if exchange is None:
            return None
        try:
            opens = exchange.fetch_open_orders(order.symbol) or []
        except Exception as e:
            log(f"fetch_open_orders after uncertain create failed: {e}", "WARNING")
            opens = []
        if isinstance(opens, list):
            for raw in opens:
                if isinstance(raw, dict) and self._order_matches_client_id(raw, key):
                    return raw
        text = f"t-{key}" if key else ""
        for ident, params in (
            (key, {}),
            (key, {"clientOrderId": key}),
            (text, {"text": text}),
            (key, {"text": text}),
        ):
            if not ident:
                continue
            try:
                fetched = exchange.fetch_order(ident, order.symbol, params) if params else exchange.fetch_order(ident, order.symbol)
            except TypeError:
                try:
                    fetched = exchange.fetch_order(ident, order.symbol)
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(fetched, dict) and fetched:
                return fetched
        return None

    def _ensure_filled(self, exchange, raw: dict, order: TradeOrder) -> dict:
        """One fetch_order if ``filled`` is missing. Never invent filled=qty."""
        if not isinstance(raw, dict):
            return {}
        if raw.get("filled") is not None:
            return raw
        oid = raw.get("id")
        if not oid or exchange is None:
            return raw
        try:
            fetched = exchange.fetch_order(oid, order.symbol)
        except Exception as e:
            log(f"fetch_order({oid}) after missing filled failed: {e}", "WARNING")
            return raw
        if not isinstance(fetched, dict):
            return raw
        merged = dict(raw)
        for k, v in fetched.items():
            if v is not None:
                merged[k] = v
        return merged

    def _vwap_from_my_trades(self, exchange, order: TradeOrder, raw: dict) -> float | None:
        if exchange is None or not isinstance(raw, dict):
            return None
        oid = str(raw.get("id") or "")
        since = raw.get("timestamp")
        try:
            since_i = int(since) if since is not None else None
        except (TypeError, ValueError):
            since_i = None
        try:
            trades = exchange.fetch_my_trades(order.symbol, since=since_i) or []
        except TypeError:
            try:
                trades = exchange.fetch_my_trades(order.symbol) or []
            except Exception as e:
                log(f"fetch_my_trades failed: {e}", "WARNING")
                return None
        except Exception as e:
            log(f"fetch_my_trades failed: {e}", "WARNING")
            return None
        matched = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("order") or t.get("orderId") or t.get("order_id") or "")
            if oid and tid == oid:
                matched.append(t)
        if not matched:
            return None
        notional = 0.0
        qty = 0.0
        for t in matched:
            p = float(t.get("price") or 0)
            a = float(t.get("amount") or 0)
            notional += p * a
            qty += a
        if qty <= 0:
            return None
        return notional / qty

    def _exchange_status_token(self, raw: dict) -> str:
        return str((raw or {}).get("status") or "").strip().lower()

    def _rejected_result(self, order: TradeOrder, message: str) -> TradeResult:
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message=message,
            order_id=order.order_id,
            order_status=OrderStatus.REJECTED,
            pending=False,
            needs_reconcile=False,
            order_exist_in_exchange=False,
        )

    def _active_reconcile_result(self, order: TradeOrder, message: str, *, exist: bool = False, raw: dict | None = None) -> TradeResult:
        oid = ""
        if isinstance(raw, dict):
            oid = str(raw.get("id") or "")
        exist = exist or (self._places_on_exchange() and bool(oid))
        order.status = OrderStatus.ACTIVE
        if oid:
            order.exchange_order_id = oid
        order.order_exist_in_exchange = exist
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message=message,
            order_id=order.order_id,
            exchange_order_id=oid,
            order_status=OrderStatus.ACTIVE,
            pending=True,
            needs_reconcile=True,
            order_exist_in_exchange=exist,
        )

    def _canceled_or_rejected_exchange(self, raw: dict) -> OrderStatus | None:
        token = self._exchange_status_token(raw)
        if token in ("canceled", "cancelled"):
            return OrderStatus.CANCELED
        if token == "rejected":
            return OrderStatus.REJECTED
        if token == "expired":
            return OrderStatus.CANCELED
        return None

    def _fill_or_unknown_fee(
        self, raw: dict, order: TradeOrder, *, side: str, fill_price: float, filled: float
    ) -> tuple[Fill, bool]:
        try:
            return self._fill_from_raw(raw, order, side=side), False
        except ValueError as e:
            log(
                f"fill_from_exchange unknown fee currency for {order.symbol}: {e}",
                "ERROR",
            )
            quote_gross = float(raw.get("cost") or 0) or fill_price * filled
            fill = Fill(
                side="sell" if str(side).lower() == "sell" else "buy",
                order_type="market",
                request_price=float(order.price or 0),
                fill_price=fill_price,
                qty_gross=filled,
                qty_net=filled,
                quote_gross=quote_gross,
                quote_net=quote_gross,
                fee_base=0.0,
                fee_quote=0.0,
                fee_usdt=0.0,
                slippage_usdt=abs(fill_price - float(order.price or 0)) * filled,
            )
            return fill, True

    def _finalize_exchange_order(
        self,
        exchange,
        order: TradeOrder,
        raw: dict,
        *,
        side: str,
        qty: float,
        timeframe: str,
        usdt: float = 0.0,
    ) -> TradeResult:
        raw = raw if isinstance(raw, dict) else {}
        terminal = self._canceled_or_rejected_exchange(raw)
        exist = self._places_on_exchange() and bool(raw.get("id") or terminal is None and raw)
        if terminal is OrderStatus.CANCELED or terminal is OrderStatus.REJECTED:
            order.status = terminal
            order.exchange_order_id = str(raw.get("id") or "")
            order.order_exist_in_exchange = self._places_on_exchange()
            return TradeResult(
                False,
                order.type,
                order.symbol,
                message=f"exchange {self._exchange_status_token(raw)}",
                order_id=order.order_id,
                exchange_order_id=order.exchange_order_id,
                order_status=terminal,
                order_exist_in_exchange=order.order_exist_in_exchange,
            )

        raw = self._ensure_filled(exchange, raw, order)
        filled_raw = raw.get("filled")
        if filled_raw is None:
            return self._active_reconcile_result(
                order, "filled missing after fetch_order", exist=exist, raw=raw
            )

        filled = float(filled_raw)
        requested = float(qty or order.qty or 0)
        ex_status = self._exchange_status_token(raw)

        need_average = False
        status: OrderStatus | None = None
        if 0 < filled < requested:
            status = OrderStatus.PARTIALLY_FILLED
            need_average = True
        elif filled >= requested and requested > 0 and ex_status in ("closed", "filled"):
            status = OrderStatus.EXECUTED
            need_average = True
        elif filled >= requested and requested > 0 and not ex_status:
            # Spec: full fill requires status == "closed" (shadow synthesises it).
            return self._active_reconcile_result(
                order, "full fill without exchange status", exist=exist, raw=raw
            )
        else:
            return self._active_reconcile_result(
                order, f"order still open (filled={filled}, status={ex_status or 'unset'})",
                exist=exist, raw=raw,
            )

        average = raw.get("average")
        if average is None and need_average:
            vwap = self._vwap_from_my_trades(exchange, order, raw)
            if vwap is None:
                return self._active_reconcile_result(
                    order, "average missing and VWAP reconstruct failed", exist=exist, raw=raw
                )
            raw = dict(raw)
            raw["average"] = vwap
            average = vwap
        fill_price = float(average if average is not None else 0)
        if fill_price <= 0:
            return self._active_reconcile_result(
                order, "average missing", exist=exist, raw=raw
            )

        fill, fee_unknown = self._fill_or_unknown_fee(
            raw, order, side=side, fill_price=fill_price, filled=filled
        )
        cost = float(raw.get("cost") or fill.quote_gross or fill_price * filled)
        order.filled_qty = filled
        order.status = status
        order.exchange_order_id = str(raw.get("id") or "")
        order.order_exist_in_exchange = self._places_on_exchange()

        sync_order = TradeOrder(
            order.type,
            order.symbol,
            fill_price,
            filled,
            usdt_amount=cost if order.type == "BUY" else order.usdt_amount,
            signal=order.signal,
            source=order.source,
            order_id=order.order_id,
            filled_qty=filled,
            status=status,
            client_order_id=order.client_order_id,
            idempotency_key=order.idempotency_key,
            exchange_order_id=order.exchange_order_id,
            order_exist_in_exchange=order.order_exist_in_exchange,
            entry_15m_vol_ratio=order.entry_15m_vol_ratio,
            leverage=order.leverage,
        )
        result = self._sync_local_ledger(
            sync_order,
            timeframe,
            exchange_order_id=order.exchange_order_id,
            usdt_received=cost if order.type == "SELL" else 0,
            fill=fill,
        )
        result.exchange_order_id = order.exchange_order_id
        result.fee = fill.fee_usdt if fill is not None else 0.0
        result.order_status = status
        result.filled_qty = filled
        result.amount = filled
        result.fee_unknown = fee_unknown
        result.needs_reconcile = fee_unknown
        result.pending = status is OrderStatus.PARTIALLY_FILLED
        result.order_exist_in_exchange = order.order_exist_in_exchange
        result.executed = True
        if self._adapter_mode == "shadow":
            result.message = f"shadow {order.type} filled {filled:.6f} @ {fill_price}"
            result.precision_unverified = self._precision_unverified
        else:
            from price_fetcher import format_usdt_price

            tag = "partial" if status is OrderStatus.PARTIALLY_FILLED else "filled"
            result.message = (
                f"Gate {order.type} {tag} {filled:.6f} @ {format_usdt_price(fill_price)}"
            )
        if fee_unknown:
            result.message = (result.message or "") + " (fee_unknown)"
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
        if self._adapter_mode == "shadow":
            rec["precision_unverified"] = bool(self._precision_unverified)
            local.precision_unverified = bool(self._precision_unverified)
        record_live_trade(rec)
        local.message = local.message or f"{self.mode} {order.type} synced"
        local.exchange_order_id = exchange_order_id
        return local