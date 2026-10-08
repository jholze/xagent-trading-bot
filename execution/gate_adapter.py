import os
import uuid
from datetime import datetime

import ccxt

from core.config import BotConfig, get_bot_config
from core.costs import COST_MODEL_VERSION, CostModel, Fill, trade_cost_fields
from core.models import OrderStatus, TradeOrder, TradeResult, trade_ctx_fields
from data_manager import record_live_trade, uses_exchange_ledger
from execution.base import ExecutionAdapter
from execution.order_fetch import call_fetch_order_retrying_not_found
from logger import log
from services.portfolio_service import PortfolioService

_GATE_TESTNET_HOST = "https://api-testnet.gateapi.io"

# Gate `text` / ccxt `clientOrderId`: charset [0-9A-Za-z_.-], prefixed `t-`,
# venue max 28 bytes *without* the prefix. ccxt 4.5.48 `create_order_request`
# checks `len(params.text) > 28` *before* prepending `t-` if missing. We send
# an already-prefixed value (`t-{payload}`), so the payload is capped at 26
# bytes and the raw param stays ≤28 — otherwise BadRequest fires and the
# order is never POSTed (#439).
_GATE_TEXT_PREFIX = "t-"
_GATE_TEXT_PARAM_MAX_BYTES = 28
_GATE_TEXT_PAYLOAD_MAX_BYTES = _GATE_TEXT_PARAM_MAX_BYTES - len(_GATE_TEXT_PREFIX)
_GATE_TEXT_ALLOWED = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_.-"
)

# R4 market cache on the existing adapter path. Tests install an override;
# production fills the cache from load_markets. No second market client.
_VENUE_CACHE: dict[str, dict] = {}
_VENUE_OVERRIDE: dict | None = None


def price_precision_places(raw) -> int | None:
    """Decimal places of the pair price. Tick sizes below 1 use the tick."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number >= 1:
        return int(number) if number == int(number) and number <= 18 else 0
    text = f"{number:.12f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _amount_step(market: dict) -> float | None:
    prec = (market.get("precision") or {}).get("amount")
    if isinstance(prec, int) and not isinstance(prec, bool) and prec >= 0:
        return 10 ** (-prec) if prec else 1.0
    try:
        step = float(prec)
    except (TypeError, ValueError):
        step = 0.0
    if step > 0:
        return step
    try:
        min_amt = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)
    except (TypeError, ValueError):
        min_amt = 0.0
    return min_amt if min_amt > 0 else None


def venue_limits_from_market(market: dict | None) -> dict:
    if not isinstance(market, dict) or not market:
        return {"known": False, "min_cost": None, "amount_step": None, "price_places": None}
    try:
        min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0)
    except (TypeError, ValueError):
        min_cost = 0.0
    return {
        "known": min_cost > 0,
        "min_cost": min_cost if min_cost > 0 else None,
        "amount_step": _amount_step(market),
        "price_places": price_precision_places((market.get("precision") or {}).get("price")),
    }


def remember_venue_markets(markets: dict | None) -> None:
    if not isinstance(markets, dict):
        return
    for symbol, market in markets.items():
        if isinstance(market, dict):
            _VENUE_CACHE[str(symbol)] = venue_limits_from_market(market)


def set_venue_limits_override(limits: dict | None) -> None:
    global _VENUE_OVERRIDE
    _VENUE_OVERRIDE = None if limits is None else dict(limits)


def venue_limits_for(symbol: str) -> dict:
    if _VENUE_OVERRIDE is not None:
        return dict(_VENUE_OVERRIDE)
    cached = _VENUE_CACHE.get(str(symbol or ""))
    if cached is not None:
        return dict(cached)
    return {"known": False, "min_cost": None, "amount_step": None, "price_places": None}


def round_usdt_to_price_precision(value: float, limits: dict | None) -> float:
    """Round a USDT loss to the pair price precision. Fallback is order-path cents."""
    places = None if not isinstance(limits, dict) else limits.get("price_places")
    if places is None:
        return round(float(value), 2)
    return round(float(value), int(places))


def _clamp_gate_client_order_id(key: str) -> str:
    """Deterministic Gate clientOrderId payload (no `t-` prefix).

    Empty / all-illegal → ccxt-style uuid16 (16 hex). Over-long keys
    drop hyphens then truncate so a retry of the same intent stays
    stable. Already-legal short keys (``abc-key``) are a no-op.
    """
    raw = (key or "").strip()
    if not raw:
        return uuid.uuid4().hex[:16]
    legal = "".join(ch for ch in raw if ch in _GATE_TEXT_ALLOWED)
    if not legal:
        return uuid.uuid4().hex[:16]
    encoded = legal.encode("utf-8")
    if len(encoded) > _GATE_TEXT_PAYLOAD_MAX_BYTES:
        compact = legal.replace("-", "")
        encoded = compact.encode("utf-8")
        legal = compact
    if len(encoded) > _GATE_TEXT_PAYLOAD_MAX_BYTES:
        # Charset is ASCII, so a byte cut is a character cut.
        legal = encoded[:_GATE_TEXT_PAYLOAD_MAX_BYTES].decode("ascii")
    return legal


_CANCEL_STATUS = frozenset({"canceled", "cancelled"})
_CANCEL_FINISH_AS = frozenset({"cancelled", "canceled"})
# Zero-fill only. Not part of ``_CANCEL_FINISH_AS``: ``ioc`` with filled > 0
# stays EXECUTED (#340 / #466). ``unified_check_failed`` is not in this set.
_ZERO_FILL_CANCEL_FINISH_AS = frozenset(
    {
        "ioc",
        "stp",
        "poc",
        "fok",
        "trader_not_enough",
        "depth_not_enough",
        "small",
        "liquidate_cancelled",
    }
)
# Partial remainder only. Not part of ``_CANCEL_FINISH_AS``: a full fill
# (filled >= requested) with these values stays EXECUTED. ``ioc`` is excluded
# so a positive ioc fill stays EXECUTED (#340 / #466, #551).
_PARTIAL_REMAINDER_FINISH_AS = frozenset({"stp", "poc", "fok"})

# POST /spot/batch_orders: at most 4 distinct pairs, 10 orders per pair, spot only.
GATE_SPOT_BATCH_MAX_PAIRS = 4
GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR = 10


class BatchMarketSellNoAck(Exception):
    """POST /spot/batch_orders did not return a per-order ACK list.

    The batch may or may not have been accepted. Callers must not invent
    fills and must not submit another batch for this fire.
    """


def _ccxt_status_token(raw: dict) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("status") or "").strip().lower()


def _finish_as_from_raw(raw: dict) -> str:
    """Case-insensitive Gate ``finish_as`` from ccxt nested ``info``. Empty if absent."""
    if not isinstance(raw, dict):
        return ""
    info = raw.get("info")
    if not isinstance(info, dict):
        return ""
    return str(info.get("finish_as") or "").strip().lower()


def _canceled_or_rejected_status(raw: dict) -> OrderStatus | None:
    """Map ccxt ``status`` and optional Gate ``finish_as`` to a terminal status.

    Missing ``finish_as`` is the status-only path: it is never treated as canceled.
    """
    token = _ccxt_status_token(raw)
    finish_as = _finish_as_from_raw(raw)
    if token in _CANCEL_STATUS or finish_as in _CANCEL_FINISH_AS:
        return OrderStatus.CANCELED
    if token == "rejected":
        return OrderStatus.REJECTED
    if token == "expired":
        return OrderStatus.CANCELED
    return None


def _zero_fill_cancel_status(raw: dict, filled: float) -> OrderStatus | None:
    """CANCELED when ``filled <= 0`` and Gate ``finish_as`` is a zero-fill cancel.

    ``ioc`` cancels only at zero fill. A positive fill whose ``finish_as`` is
    ``filled`` or ``ioc`` is not a cancel (#340 / #466). Missing ``finish_as``
    returns None so the status-only path is unchanged.
    """
    if float(filled) > 0:
        return None
    if _finish_as_from_raw(raw) in _ZERO_FILL_CANCEL_FINISH_AS:
        return OrderStatus.CANCELED
    return None


def _partial_remainder_cancel(
    raw: dict, filled: float, requested: float, status_token: str
) -> bool:
    """True when a closed order filled part of the request and canceled the rest.

    ``finish_as`` in stp/poc/fok and ``0 < filled < requested``. A full fill
    stays on the EXECUTED path. ``ioc`` is not a remainder cancel (#340 / #466).
    """
    token = str(status_token or "").strip().lower()
    if token not in ("closed", "filled"):
        return False
    filled_f = float(filled)
    requested_f = float(requested)
    if not (requested_f > 0 and 0 < filled_f < requested_f):
        return False
    return _finish_as_from_raw(raw) in _PARTIAL_REMAINDER_FINISH_AS


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
    def _rewrite_api_leaves(node, replacement: str):
        if isinstance(node, dict):
            return {
                k: GateExecutionAdapter._rewrite_api_leaves(v, replacement)
                for k, v in node.items()
            }
        return replacement

    @staticmethod
    def _apply_testnet(exchange) -> None:
        setter = getattr(exchange, "set_sandbox_mode", None)
        if callable(setter):
            setter(True)
            return
        urls = getattr(exchange, "urls", None)
        if not isinstance(urls, dict):
            return
        api = urls.get("api")
        if isinstance(api, dict):
            urls["api"] = GateExecutionAdapter._rewrite_api_leaves(
                api, f"{_GATE_TESTNET_HOST}/api/v4"
            )
        else:
            urls["api"] = _GATE_TESTNET_HOST

    def _testnet_futures_allowed(self) -> tuple[bool, str]:
        """Fail-closed: testnet mode + enabled=true + current tenant named in the list."""
        if self._adapter_mode != "testnet":
            return False, f"adapter mode is {self._adapter_mode!r}, not testnet"
        from core.tenant_context import current_tenant_context, resolve_tenant_id
        from strategies.short_policy import shorts_config

        if current_tenant_context() is None:
            return False, "no tenant context"
        cfg = shorts_config(self.config.raw)
        block = cfg.get("testnet_futures")
        if not isinstance(block, dict):
            return False, "shorts.testnet_futures missing or not a dict"
        if block.get("enabled") is not True:
            return False, "shorts.testnet_futures.enabled=false"
        tenants = block.get("tenants")
        if not isinstance(tenants, list):
            return False, "shorts.testnet_futures.tenants is not a list"
        if not tenants:
            return False, "shorts.testnet_futures.tenants is empty"
        tenant_id = resolve_tenant_id()
        if tenant_id not in tenants:
            return False, (
                f"tenant {tenant_id!r} not in shorts.testnet_futures.tenants"
            )
        return True, ""

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
            remember_venue_markets(existing)
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
            remember_venue_markets(loaded)
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
                if self._adapter_mode == "shadow":
                    if order.type == "SHORT":
                        return self._execute_short_shadow(exchange, order, timeframe)
                    return self._execute_cover_shadow(exchange, order, timeframe)
                if self._adapter_mode != "testnet":
                    return self._rejected_result(
                        order, "shorts.allow_live=false — no Gate futures in v0"
                    )
                allowed, reason = self._testnet_futures_allowed()
                if not allowed:
                    return self._rejected_result(
                        order, f"testnet futures disabled: {reason}"
                    )
                if order.type == "SHORT":
                    return self._execute_short(exchange, order, timeframe)
                return self._execute_cover(exchange, order, timeframe)
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

    def _reject_sell_below_venue_min(self, order: TradeOrder, timeframe: str, error: str) -> TradeResult:
        """A stop or full sell the venue refuses for size enters the below-min state once."""
        text = str(error or "")
        signal = str(getattr(order, "signal", "") or "")
        if "minimum" in text.lower() and ("STOP" in signal or "FULL" in signal):
            from core.tenant_context import resolve_tenant_id
            from strategies.positions import BELOW_VENUE_MINIMUM, mark_exchange_min_reject

            if mark_exchange_min_reject(order.symbol, timeframe):
                log(
                    f"{BELOW_VENUE_MINIMUM} tenant={resolve_tenant_id()} symbol={order.symbol}",
                    "WARNING",
                )
        return self._rejected_result(order, error)

    def _execute_sell(self, exchange, order: TradeOrder, timeframe: str) -> TradeResult:
        amount = float(order.qty or 0)
        if amount <= 0:
            return self._rejected_result(order, "No amount to sell")

        if self._adapter_mode == "shadow":
            if self._ensure_shadow_markets(exchange):
                try:
                    amount, error = self._validate_sell_amount(exchange, order, amount)
                    if error:
                        return self._reject_sell_below_venue_min(order, timeframe, error)
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
                return self._reject_sell_below_venue_min(order, timeframe, error)

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

    # Swap (testnet SHORT/COVER) unit invariant:
    # ``filled`` / ``amount`` / ``remaining`` from ccxt swap orders are CONTRACTS
    # until ``_swap_fill_to_base`` runs; only ``filled`` is normalised to base
    # units; ``_finalize_exchange_order`` must never receive a swap raw whose
    # ``filled`` is None (B5).
    @staticmethod
    def _swap_symbol(order: TradeOrder) -> str:
        """USDT-perp unified symbol. ``order.symbol`` stays the SPOT pair."""
        base, _, _ = str(order.symbol).partition("/")
        return f"{base}/USDT:USDT"

    def _load_swap_market(self, exchange, swap_symbol: str) -> tuple[dict, str]:
        markets = None
        try:
            loaded = exchange.load_markets()
            if isinstance(loaded, dict):
                markets = loaded
        except Exception as e:
            log(f"Gate load_markets failed for {swap_symbol}: {e}", "WARNING")
            existing = getattr(exchange, "markets", None)
            markets = existing if isinstance(existing, dict) else None
        market = markets.get(swap_symbol) if isinstance(markets, dict) else None
        if not isinstance(market, dict) or not market:
            getter = getattr(exchange, "market", None)
            if callable(getter):
                try:
                    got = getter(swap_symbol)
                    if isinstance(got, dict) and got:
                        market = got
                except Exception as e:
                    log(f"Gate market({swap_symbol}) failed: {e}", "WARNING")
        if not isinstance(market, dict) or not market:
            return {}, f"swap market {swap_symbol} not found"
        return market, ""

    def _swap_contract_size(self, market: dict, swap_symbol: str) -> float:
        raw = market.get("contractSize") if isinstance(market, dict) else None
        try:
            size = float(raw)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            log(
                f"swap {swap_symbol} contractSize missing/invalid ({raw!r}) — using 1",
                "WARNING",
            )
            return 1.0
        return size

    def _base_to_contracts(
        self, exchange, swap_symbol: str, base_qty: float, contract_size: float
    ) -> tuple[float, str]:
        # Gate USDT-perp ``amount`` is CONTRACTS, not base units.
        # contracts = base_qty / contractSize (ccxt gate.parse_market sets
        # contractSize from quanto_multiplier; linear USDT perps are often 1).
        # Ledger / positions keep base units on the SPOT symbol.
        try:
            contracts = float(base_qty) / float(contract_size)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            return 0.0, f"contract conversion failed: {e}"
        if contracts <= 0:
            return 0.0, "contracts <= 0 after conversion"
        try:
            contracts = float(exchange.amount_to_precision(swap_symbol, contracts))
        except Exception as e:
            log(
                f"Gate amount_to_precision failed for {swap_symbol}: {e}",
                "WARNING",
            )
            return 0.0, f"amount_to_precision failed: {e}"
        if contracts <= 0:
            return 0.0, "contracts <= 0 after precision"
        return contracts, ""

    def _clamp_short_leverage(self, order: TradeOrder) -> tuple[float, str]:
        from strategies.short_math import clamp_leverage
        from strategies.short_policy import shorts_config

        cfg = shorts_config(self.config.raw)
        try:
            cap = min(float(cfg.get("leverage_cap") or 2), 2.0)
        except (TypeError, ValueError):
            cap = 2.0
        try:
            default = float(cfg.get("leverage_default") or 2)
        except (TypeError, ValueError):
            default = 2.0
        lev = clamp_leverage(order.leverage or default, cap=cap)
        if lev > cap:
            return 0.0, f"leverage {lev} exceeds cap {cap}"
        order.leverage = lev
        return lev, ""

    def _set_isolated_leverage(
        self,
        exchange,
        order: TradeOrder,
        swap_symbol: str,
        lev: float,
        *,
        timeframe: str,
        side: str,
        qty: float,
    ) -> TradeResult | None:
        """ISOLATED margin via ccxt.gate.set_leverage (marginMode != cross).

        Failure before create is a clean reject — never ACTIVE/needs_reconcile.
        """
        try:
            exchange.set_leverage(
                int(lev), swap_symbol, {"marginMode": "isolated"}
            )
        except Exception as e:
            log(
                f"Gate set_leverage({int(lev)}, {swap_symbol}) failed: {e}",
                "ERROR",
            )
            try:
                return self._handle_create_exception(
                    e,
                    exchange,
                    order,
                    create_attempted=False,
                    timeframe=timeframe,
                    side=side,
                    qty=qty,
                    usdt=order.usdt_amount,
                )
            except Exception as classified:
                log(
                    f"Gate set_leverage unclassified for {swap_symbol}: {classified}",
                    "ERROR",
                )
                return self._rejected_result(
                    order,
                    str(classified)[:200] or classified.__class__.__name__,
                )
        return None

    def _swap_usdt_free(self, exchange) -> tuple[float | None, str]:
        try:
            balance = exchange.fetch_balance({"type": "swap"})
        except Exception as e:
            log(f"Gate futures USDT balance fetch failed: {e}", "WARNING")
            return None, f"futures USDT balance fetch failed: {e}"
        if not isinstance(balance, dict):
            return None, "futures USDT balance missing"
        usdt = balance.get("USDT")
        free_map = balance.get("free")
        raw = None
        if isinstance(usdt, dict):
            raw = usdt.get("free")
        if raw in (None, 0, 0.0, "") and isinstance(free_map, dict):
            raw = free_map.get("USDT")
        try:
            return float(raw or 0), ""
        except (TypeError, ValueError) as e:
            log(f"Gate futures USDT balance parse failed: {e}", "WARNING")
            return None, f"futures USDT balance parse failed: {e}"

    def _swap_position_base_qty(
        self, exchange, swap_symbol: str, contract_size: float
    ) -> float | None:
        """Exchange short size in BASE units, or None if unknown."""
        fetch = getattr(exchange, "fetch_positions", None)
        if not callable(fetch):
            return None
        try:
            try:
                positions = fetch([swap_symbol])
            except TypeError:
                positions = fetch()
        except Exception as e:
            log(
                f"Gate fetch_positions({swap_symbol}) failed: {e}",
                "WARNING",
            )
            return None
        if not isinstance(positions, list):
            return None
        matched = False
        total_base = 0.0
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            psym = str(pos.get("symbol") or "")
            if psym != swap_symbol:
                continue
            side = str(pos.get("side") or "").strip().lower()
            if side != "short":
                continue
            matched = True
            raw_contracts = pos.get("contracts")
            if raw_contracts is None:
                raw_contracts = pos.get("amount")
            try:
                contracts = abs(float(raw_contracts or 0))
            except (TypeError, ValueError):
                contracts = 0.0
            try:
                cs = float(pos.get("contractSize") or contract_size or 1) or 1.0
            except (TypeError, ValueError):
                cs = contract_size or 1.0
            total_base += contracts * cs
        if not matched:
            return 0.0
        return total_base

    def _swap_fill_to_base(
        self,
        exchange,
        order: TradeOrder,
        raw: dict,
        swap_symbol: str,
        contract_size: float,
    ) -> dict:
        """Convert ccxt swap ``filled`` from CONTRACTS to base units.

        If ``filled`` is missing, fetch the swap order on ``swap_symbol`` (never
        the spot pair) and convert. Conversion happens here — not in
        ``_ensure_filled``. Callers must not pass a still-None ``filled`` into
        ``_finalize_exchange_order`` (B5): that would merge unconverted contracts.
        """
        raw = dict(raw) if isinstance(raw, dict) else {}
        if raw.get("filled") is None:
            oid = raw.get("id")
            if oid and exchange is not None:
                try:
                    fetched = exchange.fetch_order(oid, swap_symbol)
                except Exception as e:
                    log(
                        f"fetch_order({oid}) on {swap_symbol} after missing filled "
                        f"failed: {e}",
                        "WARNING",
                    )
                    fetched = None
                if isinstance(fetched, dict):
                    for k, v in fetched.items():
                        if v is not None:
                            raw[k] = v
        filled = raw.get("filled")
        if filled is not None:
            try:
                raw["filled"] = float(filled) * float(contract_size)
            except (TypeError, ValueError) as e:
                log(
                    f"swap filled convert failed for {swap_symbol}: {e}",
                    "ERROR",
                )
                raw.pop("filled", None)
        return raw

    def _fee_usable(self, raw: dict) -> bool:
        fee = raw.get("fee") if isinstance(raw, dict) else None
        if not isinstance(fee, dict):
            return False
        try:
            cost = float(fee.get("cost") or 0)
        except (TypeError, ValueError):
            return False
        ccy = str(fee.get("currency") or "").strip()
        return cost != 0 and bool(ccy)

    def _fees_from_my_trades(
        self, exchange, order: TradeOrder, raw: dict, lookup_symbol: str
    ) -> dict | None:
        matched = self._fetch_matched_my_trades(
            exchange, order, raw, lookup_symbol=lookup_symbol
        )
        if not matched:
            return None
        costs_by_ccy: dict[str, float] = {}
        for t in matched:
            fee = t.get("fee") if isinstance(t.get("fee"), dict) else None
            if not isinstance(fee, dict):
                continue
            try:
                cost = float(fee.get("cost") or 0)
            except (TypeError, ValueError) as e:
                log(f"swap trade fee parse failed: {e}", "WARNING")
                continue
            ccy = str(fee.get("currency") or "").strip().upper()
            if not ccy:
                continue
            costs_by_ccy[ccy] = costs_by_ccy.get(ccy, 0.0) + cost
        if not costs_by_ccy:
            return None
        if len(costs_by_ccy) != 1:
            log(
                f"swap fee currencies mixed {sorted(costs_by_ccy)} for {lookup_symbol}",
                "WARNING",
            )
            return None
        ccy, cost = next(iter(costs_by_ccy.items()))
        if cost == 0:
            return None
        return {"cost": cost, "currency": ccy}

    def _hydrate_swap_fee(
        self, exchange, order: TradeOrder, raw: dict, swap_symbol: str
    ) -> dict:
        raw = dict(raw) if isinstance(raw, dict) else {}
        if self._fee_usable(raw):
            return raw
        trades_fee = self._fees_from_my_trades(exchange, order, raw, swap_symbol)
        if trades_fee is not None:
            raw["fee"] = trades_fee
            return raw
        log(
            f"swap fee missing after fetch_my_trades for {swap_symbol} — "
            "marking fee_unknown",
            "WARNING",
        )
        raw["_fee_unknown"] = True
        return raw

    def _prepare_swap_raw(
        self,
        exchange,
        order: TradeOrder,
        raw: dict,
        swap_symbol: str,
        contract_size: float,
    ) -> dict:
        raw = self._swap_fill_to_base(
            exchange, order, raw, swap_symbol, contract_size
        )
        return self._hydrate_swap_fee(exchange, order, raw, swap_symbol)

    def _swap_filled_or_reconcile(self, order: TradeOrder, raw: dict) -> TradeResult | None:
        """Fail-closed: never finalize a swap raw whose ``filled`` is still None."""
        if isinstance(raw, dict) and raw.get("filled") is not None:
            return None
        return self._active_reconcile_result(
            order,
            "swap filled unavailable — needs reconcile",
            exist=True,
            raw=raw if isinstance(raw, dict) else None,
        )

    def _place_swap_order(
        self,
        exchange,
        order: TradeOrder,
        *,
        swap_symbol: str,
        side: str,
        contracts: float,
        base_qty: float,
        contract_size: float,
        timeframe: str,
        params: dict,
        create_raised: list | None = None,
    ) -> TradeResult:
        create_attempted = False
        try:
            create_attempted = True
            raw = exchange.create_order(
                swap_symbol, "market", side, contracts, None, params
            )
        except Exception as e:
            if create_raised is not None:
                create_raised.append(True)
            return self._handle_create_exception(
                e,
                exchange,
                order,
                create_attempted=create_attempted,
                timeframe=timeframe,
                side=side,
                qty=base_qty,
                usdt=order.usdt_amount,
                lookup_symbol=swap_symbol,
                contract_size=contract_size,
            )
        raw = self._prepare_swap_raw(
            exchange, order, raw, swap_symbol, contract_size
        )
        missing = self._swap_filled_or_reconcile(order, raw)
        if missing is not None:
            return missing
        return self._finalize_exchange_order(
            exchange,
            order,
            raw,
            side=side,
            qty=base_qty,
            timeframe=timeframe,
            usdt=order.usdt_amount,
            lookup_symbol=swap_symbol,
        )

    def _persist_short_stop_state(
        self,
        symbol: str,
        timeframe: str,
        *,
        stop_id: str | None,
        failed_reason: str | None = None,
    ) -> None:
        try:
            from strategies.positions import flush_positions, set_position_field

            set_position_field(symbol, timeframe, "exchange_stop_order_id", stop_id)
            set_position_field(
                symbol, timeframe, "stop_placement_failed", failed_reason
            )
            flush_positions(force=True)
        except Exception as e:
            log(
                f"persist short stop state failed for {symbol} {timeframe}: {e}",
                "WARNING",
            )

    def _place_reduce_only_stop(
        self,
        exchange,
        order: TradeOrder,
        result: TradeResult,
        *,
        swap_symbol: str,
        contract_size: float,
        lev: float,
        timeframe: str,
    ) -> None:
        """Attach a reduce-only exchange stop after a filled testnet SHORT.

        Failure never rejects the already-filled short, never places a naked
        market close, and never writes an order row / QUEUED / needs_reconcile.
        """
        from strategies.positions import get_position
        from strategies.short_math import stop_price
        from strategies.short_policy import resolve_short_params

        filled_base = float(result.amount or 0) or float(result.filled_qty or 0)
        fill_price = float(result.price or 0)
        if fill_price <= 0:
            lot = get_position(order.symbol, timeframe)
            fill_price = float(lot.get("average_entry") or 0)
        if filled_base <= 0 or fill_price <= 0:
            reason = "no fill price/qty for reduce-only stop"
            log(
                f"Gate reduce-only stop skipped for {order.symbol}: {reason}",
                "WARNING",
            )
            self._persist_short_stop_state(
                order.symbol, timeframe, stop_id=None, failed_reason=reason
            )
            return

        lot = get_position(order.symbol, timeframe)
        params = resolve_short_params(
            symbol=order.symbol,
            lot=lot if isinstance(lot, dict) else None,
            config_raw=self.config.raw,
        )
        stop_margin = float(params.get("stop_margin_pct") or 0.12)
        trigger = stop_price("short", fill_price, stop_margin, lev)
        if trigger <= 0:
            reason = f"stop_price computed non-positive ({trigger})"
            log(
                f"Gate reduce-only stop skipped for {order.symbol}: {reason}",
                "WARNING",
            )
            self._persist_short_stop_state(
                order.symbol, timeframe, stop_id=None, failed_reason=reason
            )
            return
        try:
            prec = getattr(exchange, "price_to_precision", None)
            if callable(prec):
                trigger = float(prec(swap_symbol, trigger))
        except Exception as e:
            log(
                f"Gate price_to_precision failed for stop {swap_symbol}: {e}",
                "WARNING",
            )

        contracts, conv_err = self._base_to_contracts(
            exchange, swap_symbol, filled_base, contract_size
        )
        if conv_err:
            reason = f"stop contract conversion failed: {conv_err}"
            log(
                f"Gate reduce-only stop skipped for {order.symbol}: {reason}",
                "WARNING",
            )
            self._persist_short_stop_state(
                order.symbol, timeframe, stop_id=None, failed_reason=reason
            )
            return

        # ccxt-gate type is only 'limit'|'market'; trigger* params make it a stop.
        # Gate client `text` is ≤28 bytes (`t-` + 16 hex).
        stop_params = {
            "reduceOnly": True,
            "stopPrice": trigger,
            "triggerPrice": trigger,
            "stopLossPrice": trigger,
            "text": f"t-{uuid.uuid4().hex[:16]}",
        }
        try:
            raw = exchange.create_order(
                swap_symbol,
                "market",
                "buy",
                contracts,
                None,
                stop_params,
            )
        except Exception as e:
            reason = str(e)[:200] or e.__class__.__name__
            log(
                f"Gate reduce-only stop rejected for {order.symbol}: {reason}",
                "WARNING",
            )
            self._persist_short_stop_state(
                order.symbol, timeframe, stop_id=None, failed_reason=reason
            )
            return

        oid = ""
        if isinstance(raw, dict):
            oid = str(raw.get("id") or "")
            info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
            if not oid:
                oid = str(info.get("id") or info.get("order_id") or "")
        if not oid:
            reason = "stop create returned no id"
            log(
                f"Gate reduce-only stop rejected for {order.symbol}: {reason}",
                "WARNING",
            )
            self._persist_short_stop_state(
                order.symbol, timeframe, stop_id=None, failed_reason=reason
            )
            return
        self._persist_short_stop_state(
            order.symbol, timeframe, stop_id=oid, failed_reason=None
        )

    def _cancel_reduce_only_stop(
        self,
        exchange,
        order: TradeOrder,
        *,
        swap_symbol: str,
        stop_oid: str,
        timeframe: str,
    ) -> None:
        if not stop_oid:
            return
        try:
            cancel = getattr(exchange, "cancel_order", None)
            if not callable(cancel):
                raise RuntimeError("exchange has no cancel_order")
            # Price-triggered orders live on the price-orders endpoint.
            cancel(str(stop_oid), swap_symbol, {"trigger": True})
        except Exception as e:
            log(
                f"Gate cancel reduce-only stop {stop_oid} for {order.symbol} failed: {e}",
                "WARNING",
            )
            return
        self._persist_short_stop_state(
            order.symbol, timeframe, stop_id=None, failed_reason=None
        )

    def _lot_exchange_stop_id(self, symbol: str, timeframe: str) -> str:
        try:
            from strategies.positions import get_position

            pos = get_position(symbol, timeframe)
        except Exception as e:
            log(
                f"read exchange_stop_order_id failed for {symbol} {timeframe}: {e}",
                "WARNING",
            )
            return ""
        if not isinstance(pos, dict):
            return ""
        return str(pos.get("exchange_stop_order_id") or "").strip()

    def _execute_short(self, exchange, order: TradeOrder, timeframe: str) -> TradeResult:
        from strategies.short_math import margin_usdt

        swap_symbol = self._swap_symbol(order)
        lev, lev_err = self._clamp_short_leverage(order)
        if lev_err:
            return self._rejected_result(order, lev_err)

        base_qty = float(order.qty or 0)
        if base_qty <= 0 and order.price > 0 and float(order.usdt_amount or 0) > 0:
            base_qty = float(order.usdt_amount) / float(order.price)
        if base_qty <= 0:
            return self._rejected_result(order, "No amount to short")
        if not order.qty:
            order.qty = base_qty

        market, market_err = self._load_swap_market(exchange, swap_symbol)
        if market_err:
            return self._rejected_result(order, market_err)
        contract_size = self._swap_contract_size(market, swap_symbol)
        contracts, conv_err = self._base_to_contracts(
            exchange, swap_symbol, base_qty, contract_size
        )
        if conv_err:
            return self._rejected_result(order, conv_err)
        min_amount = float(
            (market.get("limits") or {}).get("amount", {}).get("min", 0) or 0
        )
        if min_amount and contracts < min_amount:
            return self._rejected_result(
                order,
                f"Amount {contracts:.6f} contracts below Gate minimum ({min_amount})",
            )

        required = margin_usdt(base_qty, float(order.price or 0), lev)
        free, bal_err = self._swap_usdt_free(exchange)
        if bal_err:
            return self._rejected_result(order, bal_err)
        if free is None or free < required:
            shown = 0.0 if free is None else free
            return self._rejected_result(
                order,
                f"Insufficient futures USDT margin ({shown:.2f} < {required:.2f})",
            )

        leverage_fail = self._set_isolated_leverage(
            exchange,
            order,
            swap_symbol,
            lev,
            timeframe=timeframe,
            side="sell",
            qty=base_qty,
        )
        if leverage_fail is not None:
            return leverage_fail

        params = self._client_order_params(order)
        create_raised: list = []
        result = self._place_swap_order(
            exchange,
            order,
            swap_symbol=swap_symbol,
            side="sell",
            contracts=contracts,
            base_qty=base_qty,
            contract_size=contract_size,
            timeframe=timeframe,
            params=params,
            create_raised=create_raised,
        )
        # Recovered-uncertain (create_order raised) and ACTIVE/needs_reconcile
        # (executed=False) never attach a stop. Full and partial fills do.
        if create_raised:
            return result
        if result.executed and result.order_status in (
            OrderStatus.EXECUTED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            try:
                self._place_reduce_only_stop(
                    exchange,
                    order,
                    result,
                    swap_symbol=swap_symbol,
                    contract_size=contract_size,
                    lev=lev,
                    timeframe=timeframe,
                )
            except Exception as e:
                reason = str(e)[:200] or e.__class__.__name__
                log(
                    f"Gate reduce-only stop failed for {order.symbol}: {reason}",
                    "WARNING",
                )
                self._persist_short_stop_state(
                    order.symbol,
                    timeframe,
                    stop_id=None,
                    failed_reason=reason,
                )
        return result

    def _execute_cover(self, exchange, order: TradeOrder, timeframe: str) -> TradeResult:
        swap_symbol = self._swap_symbol(order)
        stop_oid = self._lot_exchange_stop_id(order.symbol, timeframe)
        base_qty = float(order.qty or 0)
        if base_qty <= 0:
            return self._rejected_result(order, "No amount to cover")

        market, market_err = self._load_swap_market(exchange, swap_symbol)
        if market_err:
            return self._rejected_result(order, market_err)
        contract_size = self._swap_contract_size(market, swap_symbol)

        exchange_base = self._swap_position_base_qty(
            exchange, swap_symbol, contract_size
        )
        if exchange_base is not None and exchange_base > 0 and base_qty > exchange_base:
            log(
                f"Cover amount capped: ledger {base_qty:.6f} > exchange "
                f"{exchange_base:.6f} for {order.symbol}",
                "WARNING",
            )
            base_qty = exchange_base
            order.qty = base_qty
        elif exchange_base is not None and exchange_base <= 0:
            return self._active_reconcile_result(
                order,
                "no swap position to cover — ledger/exchange mismatch",
                exist=False,
            )

        contracts, conv_err = self._base_to_contracts(
            exchange, swap_symbol, base_qty, contract_size
        )
        if conv_err:
            return self._rejected_result(order, conv_err)
        min_amount = float(
            (market.get("limits") or {}).get("amount", {}).get("min", 0) or 0
        )
        if min_amount and contracts < min_amount:
            return self._rejected_result(
                order,
                f"Amount {contracts:.6f} contracts below Gate minimum ({min_amount})",
            )

        params = self._client_order_params(order)
        params["reduceOnly"] = True
        result = self._place_swap_order(
            exchange,
            order,
            swap_symbol=swap_symbol,
            side="buy",
            contracts=contracts,
            base_qty=base_qty,
            contract_size=contract_size,
            timeframe=timeframe,
            params=params,
        )
        if result.executed and stop_oid:
            self._cancel_reduce_only_stop(
                exchange,
                order,
                swap_symbol=swap_symbol,
                stop_oid=stop_oid,
                timeframe=timeframe,
            )
        return result

    def _execute_short_shadow(
        self, exchange, order: TradeOrder, timeframe: str
    ) -> TradeResult:
        """Synthesise a SHORT fill. Never calls create_* / set_leverage / swap APIs."""
        from strategies.positions import get_position
        from strategies.short_math import is_short, margin_usdt

        lev, lev_err = self._clamp_short_leverage(order)
        if lev_err:
            return self._rejected_result(order, lev_err)

        base_qty = float(order.qty or 0)
        if base_qty <= 0 and order.price > 0 and float(order.usdt_amount or 0) > 0:
            base_qty = float(order.usdt_amount) / float(order.price)
        if base_qty <= 0:
            return self._rejected_result(order, "No amount to short")
        order.qty = base_qty

        pos = get_position(order.symbol, timeframe)
        from strategies.positions import is_open_position

        if is_open_position(pos) and not is_short(pos):
            return self._rejected_result(order, "one-way: close long before short")

        required = margin_usdt(base_qty, float(order.price or 0), lev)
        balance = self._fetch_usdt_balance()
        if balance < required:
            return self._rejected_result(
                order,
                f"Insufficient USDT margin ({balance:.2f} < {required:.2f})",
            )

        base_qty, verified = self._shadow_adjust_amount(
            exchange, order.symbol, base_qty
        )
        self._precision_unverified = not verified
        order.qty = base_qty

        raw = self._synthesize_shadow_raw(order, side="sell", amount=base_qty)
        return self._finalize_exchange_order(
            exchange, order, raw, side="sell", qty=base_qty, timeframe=timeframe
        )

    def _execute_cover_shadow(
        self, exchange, order: TradeOrder, timeframe: str
    ) -> TradeResult:
        """Synthesise a COVER fill. Never calls create_* / set_leverage / swap APIs."""
        from strategies.positions import get_position
        from strategies.short_math import is_short

        base_qty = float(order.qty or 0)
        pos = get_position(order.symbol, timeframe)
        lot_amt = float(pos.get("amount") or 0)
        if not is_short(pos) or lot_amt <= 0:
            return self._rejected_result(order, "No short to cover")
        if base_qty <= 0:
            base_qty = lot_amt
        elif base_qty > lot_amt:
            log(
                f"Cover amount capped: ledger {base_qty:.6f} > lot "
                f"{lot_amt:.6f} for {order.symbol}",
                "WARNING",
            )
            base_qty = lot_amt
        order.qty = base_qty

        base_qty, verified = self._shadow_adjust_amount(
            exchange, order.symbol, base_qty
        )
        self._precision_unverified = not verified
        order.qty = base_qty

        raw = self._synthesize_shadow_raw(order, side="buy", amount=base_qty)
        return self._finalize_exchange_order(
            exchange, order, raw, side="buy", qty=base_qty, timeframe=timeframe
        )

    def _places_on_exchange(self) -> bool:
        return self._adapter_mode in ("real", "testnet")

    @staticmethod
    def _clamp_gate_client_order_id(key: str) -> str:
        return _clamp_gate_client_order_id(key)

    def _client_order_params(self, order: TradeOrder) -> dict:
        key = (order.client_order_id or order.idempotency_key or "").strip()
        key = _clamp_gate_client_order_id(key)
        order.client_order_id = key
        if not order.idempotency_key:
            order.idempotency_key = key
        return {"text": f"{_GATE_TEXT_PREFIX}{key}"}

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
        lookup_symbol: str | None = None,
        contract_size: float | None = None,
    ) -> TradeResult:
        hard = self._hard_reject_types()
        uncertain = self._uncertain_types()
        if hard and isinstance(exc, hard):
            return self._rejected_result(order, str(exc)[:200] or exc.__class__.__name__)
        if create_attempted and uncertain and isinstance(exc, uncertain):
            found, looked = self._recover_after_uncertain_create(
                exchange, order, lookup_symbol=lookup_symbol
            )
            if found is not None:
                if lookup_symbol and contract_size is not None:
                    found = self._prepare_swap_raw(
                        exchange, order, found, lookup_symbol, contract_size
                    )
                    missing = self._swap_filled_or_reconcile(order, found)
                    if missing is not None:
                        return missing
                return self._finalize_exchange_order(
                    exchange,
                    order,
                    found,
                    side=side,
                    qty=qty or float(order.qty or 0),
                    timeframe=timeframe,
                    usdt=usdt or order.usdt_amount,
                    lookup_symbol=lookup_symbol,
                )
            if looked:
                return self._rejected_result(order, "not placed")
            # Lookups never returned: do not claim "not placed", do not resend.
            return self._active_reconcile_result(
                order,
                "create uncertain, exchange unreachable — needs reconcile",
                exist=False,
            )
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

    def _recover_after_uncertain_create(
        self, exchange, order: TradeOrder, lookup_symbol: str | None = None
    ) -> tuple[dict | None, bool]:
        """fetch_open_orders then fetch_order by client_order_id. Never resend.

        Returns ``(raw, looked)``. ``looked`` is True iff at least one lookup
        returned without raising (including empty/None). False when
        ``exchange is None`` or every call raised.
        """
        key = order.client_order_id or order.idempotency_key
        if exchange is None:
            return None, False
        symbol = lookup_symbol or order.symbol
        looked = False
        try:
            opens = exchange.fetch_open_orders(symbol) or []
            looked = True
        except Exception as e:
            log(f"fetch_open_orders after uncertain create failed: {e}", "WARNING")
            opens = []
        if isinstance(opens, list):
            for raw in opens:
                if isinstance(raw, dict) and self._order_matches_client_id(raw, key):
                    return raw, True
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
                if params:
                    fetched = call_fetch_order_retrying_not_found(
                        exchange.fetch_order, ident, symbol, params
                    )
                else:
                    fetched = call_fetch_order_retrying_not_found(
                        exchange.fetch_order, ident, symbol
                    )
            except TypeError:
                try:
                    fetched = call_fetch_order_retrying_not_found(
                        exchange.fetch_order, ident, symbol
                    )
                except Exception as e:
                    log(
                        f"fetch_order({ident!r}) after uncertain create failed: {e}",
                        "WARNING",
                    )
                    continue
            except Exception as e:
                log(
                    f"fetch_order({ident!r}) after uncertain create failed: {e}",
                    "WARNING",
                )
                continue
            looked = True
            if isinstance(fetched, dict) and fetched:
                return fetched, True
        return None, looked

    def _ensure_filled(
        self,
        exchange,
        raw: dict,
        order: TradeOrder,
        lookup_symbol: str | None = None,
    ) -> dict:
        """One fetch_order if ``filled`` is missing. Never invent filled=qty."""
        if not isinstance(raw, dict):
            return {}
        if raw.get("filled") is not None:
            return raw
        oid = raw.get("id")
        if not oid or exchange is None:
            return raw
        symbol = lookup_symbol or order.symbol
        try:
            fetched = exchange.fetch_order(oid, symbol)
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

    def _fetch_matched_my_trades(
        self,
        exchange,
        order: TradeOrder,
        raw: dict,
        lookup_symbol: str | None = None,
    ) -> list[dict]:
        if exchange is None or not isinstance(raw, dict):
            return []
        oid = str(raw.get("id") or "")
        since = raw.get("timestamp")
        try:
            since_i = int(since) if since is not None else None
        except (TypeError, ValueError):
            since_i = None
        symbol = lookup_symbol or order.symbol
        try:
            trades = exchange.fetch_my_trades(symbol, since=since_i) or []
        except TypeError:
            try:
                trades = exchange.fetch_my_trades(symbol) or []
            except Exception as e:
                log(f"fetch_my_trades failed: {e}", "WARNING")
                return []
        except Exception as e:
            log(f"fetch_my_trades failed: {e}", "WARNING")
            return []
        if not isinstance(trades, list):
            return []
        matched = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("order") or t.get("orderId") or t.get("order_id") or "")
            if oid and tid == oid:
                matched.append(t)
        return matched

    def _vwap_from_my_trades(
        self,
        exchange,
        order: TradeOrder,
        raw: dict,
        lookup_symbol: str | None = None,
    ) -> float | None:
        matched = self._fetch_matched_my_trades(
            exchange, order, raw, lookup_symbol=lookup_symbol
        )
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
        return _ccxt_status_token(raw)

    def _gate_finish_as(self, raw: dict) -> str:
        return _finish_as_from_raw(raw)

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
        return _canceled_or_rejected_status(raw)

    def _terminal_no_fill_result(
        self, order: TradeOrder, raw: dict, terminal: OrderStatus
    ) -> TradeResult:
        token = self._exchange_status_token(raw)
        finish_as = self._gate_finish_as(raw)
        if finish_as:
            message = f"exchange {token or 'unset'} finish_as={finish_as}"
        else:
            message = f"exchange {token}"
        order.status = terminal
        order.exchange_order_id = str(raw.get("id") or "")
        order.order_exist_in_exchange = self._places_on_exchange()
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message=message,
            order_id=order.order_id,
            exchange_order_id=order.exchange_order_id,
            order_status=terminal,
            pending=False,
            needs_reconcile=False,
            order_exist_in_exchange=order.order_exist_in_exchange,
        )

    def _fill_or_unknown_fee(
        self,
        raw: dict,
        order: TradeOrder,
        *,
        side: str,
        fill_price: float,
        filled: float,
        force_unknown: bool = False,
    ) -> tuple[Fill, bool]:
        if not force_unknown:
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
        lookup_symbol: str | None = None,
    ) -> TradeResult:
        raw = raw if isinstance(raw, dict) else {}
        terminal = self._canceled_or_rejected_exchange(raw)
        exist = self._places_on_exchange() and bool(raw.get("id") or terminal is None and raw)
        if terminal is OrderStatus.REJECTED:
            return self._terminal_no_fill_result(order, raw, terminal)

        # Cancel with a remainder fill must not return before _ensure_filled /
        # portfolio. Zero-fill cancel (status or finish_as) stays CANCELED.
        raw = self._ensure_filled(exchange, raw, order, lookup_symbol=lookup_symbol)
        terminal = self._canceled_or_rejected_exchange(raw)
        filled_raw = raw.get("filled")
        if filled_raw is None:
            if terminal is OrderStatus.CANCELED or terminal is OrderStatus.REJECTED:
                return self._terminal_no_fill_result(order, raw, terminal)
            return self._active_reconcile_result(
                order, "filled missing after fetch_order", exist=exist, raw=raw
            )

        filled = float(filled_raw)
        requested = float(qty or order.qty or 0)
        ex_status = self._exchange_status_token(raw)
        finish_as = self._gate_finish_as(raw)

        if terminal is OrderStatus.CANCELED and filled <= 0:
            return self._terminal_no_fill_result(order, raw, terminal)
        # Shared with recovery: zero-fill finish_as the status token does not
        # name (ioc only at filled <= 0). Do not book the fill (#549).
        if _zero_fill_cancel_status(raw, filled) is OrderStatus.CANCELED:
            return self._terminal_no_fill_result(order, raw, OrderStatus.CANCELED)

        closed = ex_status in ("closed", "filled")
        # stp/poc/fok with a real partial fill use the remainder-canceled
        # stamp. Full fills stay EXECUTED. ioc is not included (#551).
        remainder_canceled = (
            terminal is OrderStatus.CANCELED and filled > 0
        ) or _partial_remainder_cancel(raw, filled, requested, ex_status)

        need_average = False
        status: OrderStatus | None = None
        if remainder_canceled:
            # Same portfolio path as PARTIALLY_FILLED; stamped CANCELED after book.
            status = OrderStatus.PARTIALLY_FILLED
            need_average = True
        elif finish_as == "open" and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
            need_average = True
        elif closed and filled > 0:
            # USDT market buys estimate qty from the request price; the
            # actual fill is smaller after spread/slippage. Closed is final.
            # finish_as in (filled, ioc) keeps this EXECUTED path (#340).
            status = OrderStatus.EXECUTED
            need_average = True
        elif 0 < filled < requested:
            status = OrderStatus.PARTIALLY_FILLED
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
            vwap = self._vwap_from_my_trades(
                exchange, order, raw, lookup_symbol=lookup_symbol
            )
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
        if (
            status is OrderStatus.EXECUTED
            and requested > 0
            and filled < requested * 0.95
        ):
            log(
                f"closed short-fill {order.symbol}: requested={requested:.6f} "
                f"filled={filled:.6f} fill_price={fill_price}",
                "INFO",
            )

        force_unknown = False
        if isinstance(raw, dict) and raw.get("_fee_unknown"):
            force_unknown = True
            raw = dict(raw)
            raw.pop("_fee_unknown", None)
        fill, fee_unknown = self._fill_or_unknown_fee(
            raw,
            order,
            side=side,
            fill_price=fill_price,
            filled=filled,
            force_unknown=force_unknown,
        )
        cost = float(raw.get("cost") or fill.quote_gross or fill_price * filled)
        order.filled_qty = filled
        order.status = status
        order.exchange_order_id = str(raw.get("id") or "")
        order.order_exist_in_exchange = self._places_on_exchange()

        if order.type == "BUY":
            sync_usdt = cost
        elif order.type == "SHORT":
            # execute_short sizes as notional/price; pass fill notional so
            # the ledger lot equals filled base (full and partial).
            sync_usdt = fill_price * filled
        else:
            sync_usdt = order.usdt_amount
        sync_order = TradeOrder(
            order.type,
            order.symbol,
            fill_price,
            filled,
            usdt_amount=sync_usdt,
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
            exit_source=getattr(order, "exit_source", "") or "",
            ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
            ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
            ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
            ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
        )
        result = self._sync_local_ledger(
            sync_order,
            timeframe,
            exchange_order_id=order.exchange_order_id,
            usdt_received=cost if order.type == "SELL" else 0,
            fill=fill,
            fee_unknown=fee_unknown,
        )
        result.exchange_order_id = order.exchange_order_id
        result.fee = fill.fee_usdt if fill is not None else 0.0
        result.order_status = status
        result.filled_qty = filled  # GROSS: ccxt filled (exchange reconciliation)
        result.amount = fill.qty_net  # NET: qty owned; matches the position book
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
        if remainder_canceled:
            order.status = OrderStatus.CANCELED
            result.order_status = OrderStatus.CANCELED
            result.pending = False
            if not fee_unknown:
                result.needs_reconcile = False
            from price_fetcher import format_usdt_price

            result.message = (
                f"Gate {order.type} canceled remainder filled {filled:.6f} "
                f"@ {format_usdt_price(fill_price)}"
            )
            if fee_unknown:
                result.message = (result.message or "") + " (fee_unknown)"
        elif fee_unknown:
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
        fee_unknown: bool = False,
    ) -> TradeResult:
        oid = order.order_id or None
        sync_virtual = not uses_exchange_ledger(self.config.trading_mode)
        ctx = trade_ctx_fields(order)
        pre_lot = None
        if order.type in ("SHORT", "COVER"):
            from strategies.positions import get_position

            pre_lot = get_position(order.symbol, timeframe)
        # Dry-run / no exchange raw: simulate so ledger and P&L share one Fill.
        if fill is None and order.type in ("BUY", "SELL") and order.price > 0:
            cm = CostModel.from_config(self.config, symbol=order.symbol)
            if order.type == "BUY":
                usdt = order.usdt_amount or self._max_usdt()
                if usdt > 0:
                    fill = cm.simulate_buy(order.price, usdt=usdt)
            elif order.amount and order.amount > 0:
                fill = cm.simulate_sell(order.price, order.amount)
        row_order_type = None
        if fee_unknown and order.type == "BUY" and oid:
            try:
                from data_manager import resolve_ledger_scope
                from services.order_service import OrderService

                stored = OrderService(resolve_ledger_scope()).get_by_id(oid)
                if isinstance(stored, dict) and stored.get("order_type"):
                    row_order_type = str(stored.get("order_type"))
            except Exception:
                row_order_type = None
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
                ctx=ctx,
                fee_unknown=fee_unknown,
                order_type=row_order_type,
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
                ctx=ctx,
                exit_source=getattr(order, "exit_source", "") or None,
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
                ctx=ctx,
            )
        elif order.type == "SELL":
            local = self.portfolio.execute_sell(
                order.symbol, timeframe, order.price, order.signal or "SELL", order.amount,
                source=order.source, order_id=oid, sync_virtual_ledger=sync_virtual,
                fill=fill, ctx=ctx, fee_unknown=fee_unknown,
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
            **ctx,
        }
        if fill is not None:
            rec.update(trade_cost_fields(fill))
        if order.type in ("SHORT", "COVER"):
            from strategies.positions import get_position
            from strategies.short_math import margin_usdt

            if order.type == "SHORT":
                lot = get_position(order.symbol, timeframe)
                lev = float(lot.get("leverage") or 0) or float(
                    getattr(order, "leverage", 0) or 0
                )
                rec["leverage"] = lev
                rec["margin_usdt"] = margin_usdt(
                    float(local.amount or 0), float(order.price or 0), lev
                )
            else:
                lot = pre_lot if isinstance(pre_lot, dict) else {}
                lev = float(lot.get("leverage") or 0) or 2.0
                entry = float(lot.get("average_entry") or 0) or float(
                    order.price or 0
                )
                rec["leverage"] = lev
                rec["margin_usdt"] = margin_usdt(
                    float(local.amount or 0), entry, lev
                )
                rec["funding_usdt"] = local.funding_usdt
                rec["funding_unknown"] = bool(local.funding_unknown) or not bool(
                    local.executed
                )
        if self._adapter_mode == "shadow":
            rec["precision_unverified"] = bool(self._precision_unverified)
            local.precision_unverified = bool(self._precision_unverified)
        record_live_trade(rec)
        local.message = local.message or f"{self.mode} {order.type} synced"
        local.exchange_order_id = exchange_order_id
        return local

    def batch_market_sell(self, orders: list[dict] | None) -> list[dict]:
        """Submit one spot chunk of market sells. POST /spot/batch_orders.

        One call, at most 4 distinct pairs and 10 orders per pair. ``text``
        is required on every order. Spot only — margin and COVER are rejected
        before any HTTP. Returns one row per exchange ACK with ``succeeded``,
        ``label``, and ``message``. Does not write the ledger and does not
        flatten a position; the caller attributes a fill only after a
        per-order ACK.

        Shadow returns the same per-lot shape and does not call the exchange.
        A transport, auth, or non-list response raises ``BatchMarketSellNoAck``
        (logged). There is no invented ACK list.
        """
        try:
            payload = _build_spot_batch_market_sells(list(orders or []))
        except ValueError as exc:
            log(f"liq_cascade batch_market_sell rejected chunk: {exc}", "ERROR")
            raise
        if not payload:
            return []
        if self._adapter_mode == "shadow":
            # Original orders carry the caller price. The wire body does not.
            sources = list(orders or [])
            return [
                _shadow_batch_ack(wire, sources[i] if i < len(sources) else None)
                for i, wire in enumerate(payload)
            ]
        exchange = self._get_exchange()
        if exchange is None:
            log(
                "liq_cascade batch_market_sell: no exchange client — no ack",
                "ERROR",
            )
            raise BatchMarketSellNoAck("no exchange client")
        submit = getattr(exchange, "privateSpotPostBatchOrders", None)
        if not callable(submit):
            log(
                "liq_cascade batch_market_sell: privateSpotPostBatchOrders missing",
                "ERROR",
            )
            raise BatchMarketSellNoAck("batch endpoint missing")
        try:
            raw = submit(payload)
        except Exception as exc:
            log(
                "liq_cascade batch_market_sell transport/auth failure "
                f"orders={len(payload)}: {exc.__class__.__name__}: {exc}",
                "ERROR",
            )
            raise BatchMarketSellNoAck(
                f"batch submit failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        if not isinstance(raw, list):
            log(
                "liq_cascade batch_market_sell non-list response "
                f"type={type(raw).__name__} — no per-order ack",
                "ERROR",
            )
            raise BatchMarketSellNoAck(
                f"batch response was {type(raw).__name__}, not a per-order list"
            )
        return [_normalize_batch_ack(row) for row in raw]


def _spot_currency_pair(symbol: str) -> str:
    sym = str(symbol or "").strip()
    if not sym or ":" in sym:
        raise ValueError(f"not a spot pair: {sym!r}")
    if "/" in sym:
        base, _, quote = sym.partition("/")
        if not base or not quote or "/" in quote:
            raise ValueError(f"not a spot pair: {sym!r}")
        return f"{base}_{quote}"
    if "_" not in sym:
        raise ValueError(f"not a spot pair: {sym!r}")
    return sym


def _require_batch_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw.startswith(_GATE_TEXT_PREFIX):
        raise ValueError("batch order text must start with t-")
    payload = raw[len(_GATE_TEXT_PREFIX) :]
    if not payload or len(payload.encode("utf-8")) > _GATE_TEXT_PARAM_MAX_BYTES:
        raise ValueError("batch order text payload must be 1..28 bytes")
    if any(ch not in _GATE_TEXT_ALLOWED for ch in payload):
        raise ValueError("batch order text has illegal characters")
    return raw


def _gate_amount_string(amount: float) -> str:
    from decimal import Decimal

    text = format(Decimal(str(amount)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _build_spot_batch_market_sells(orders: list) -> list[dict]:
    """Wire body for one POST /spot/batch_orders chunk. No HTTP."""
    if not isinstance(orders, list):
        raise ValueError("batch market sell orders must be a list")
    payload: list[dict] = []
    per_pair: dict[str, int] = {}
    for idx, order in enumerate(orders):
        if not isinstance(order, dict):
            raise ValueError(f"batch order {idx} is not an object")
        account = str(order.get("account") or "spot").strip().lower()
        if account != "spot":
            raise ValueError(
                f"batch order {idx} account={account!r}; spot batch cannot mix margin"
            )
        side = str(order.get("side") or "sell").strip().lower()
        if side != "sell":
            raise ValueError(
                f"batch order {idx} side={side!r}; batch method is market sells only"
            )
        order_type = str(order.get("type") or "market").strip().lower()
        if order_type != "market":
            raise ValueError(
                f"batch order {idx} type={order_type!r}; batch method is market sells only"
            )
        pair = _spot_currency_pair(
            str(order.get("symbol") or order.get("currency_pair") or "")
        )
        text = _require_batch_text(str(order.get("text") or ""))
        try:
            amount = float(order.get("amount"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"batch order {idx} amount is invalid") from exc
        if amount <= 0:
            raise ValueError(f"batch order {idx} amount must be positive")
        per_pair[pair] = per_pair.get(pair, 0) + 1
        if per_pair[pair] > GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR:
            raise ValueError(
                f"batch chunk has more than {GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR} "
                f"orders for {pair}"
            )
        payload.append(
            {
                "text": text,
                "currency_pair": pair,
                "type": "market",
                "account": "spot",
                "side": "sell",
                "amount": _gate_amount_string(amount),
                "time_in_force": "ioc",
            }
        )
    if len(per_pair) > GATE_SPOT_BATCH_MAX_PAIRS:
        raise ValueError(
            f"batch chunk has {len(per_pair)} currency pairs; "
            f"max is {GATE_SPOT_BATCH_MAX_PAIRS}"
        )
    return payload


def _batch_succeeded_flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "1"):
            return True
        if token in ("false", "0"):
            return False
    return None


def _batch_optional_float(row: dict, *keys):
    """First present numeric field. ``None`` when every key is missing or blank.

    ``0`` is a real value (a zero fill). It is not treated as missing.
    """
    for key in keys:
        if key not in row:
            continue
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _batch_finish_as(row: dict) -> str:
    """Gate ``finish_as`` on a flat batch row, or ccxt nested ``info``."""
    if not isinstance(row, dict):
        return ""
    direct = str(row.get("finish_as") or "").strip().lower()
    if direct:
        return direct
    info = row.get("info")
    if isinstance(info, dict):
        return str(info.get("finish_as") or "").strip().lower()
    return ""


def _normalize_batch_ack(row) -> dict:
    """Per-order ACK. Keeps the fill fields the sequential adapter classifies on.

    ``succeeded`` alone is not a size or a price. ``filled_amount``, ``left``,
    ``finish_as``, ``avg_deal_price``, and ``fill_price`` stay on the row so
    the caller can apply ``_zero_fill_cancel_status`` and
    ``_partial_remainder_cancel`` instead of booking the snapshot.
    """
    if not isinstance(row, dict):
        return {
            "succeeded": None,
            "label": "",
            "message": "ack row was not an object",
            "text": "",
            "currency_pair": "",
            "id": "",
            "status": "",
            "finish_as": "",
            "filled_amount": None,
            "left": None,
            "avg_deal_price": None,
            "fill_price": None,
        }
    avg = _batch_optional_float(row, "avg_deal_price", "average")
    fill_price = _batch_optional_float(row, "fill_price")
    if fill_price is None:
        fill_price = avg
    if avg is None:
        avg = fill_price
    return {
        "succeeded": _batch_succeeded_flag(row.get("succeeded")),
        "label": str(row.get("label") or ""),
        "message": str(row.get("message") or ""),
        "text": str(row.get("text") or ""),
        "currency_pair": str(row.get("currency_pair") or ""),
        "id": str(row.get("id") or row.get("order_id") or ""),
        "status": str(row.get("status") or ""),
        "finish_as": _batch_finish_as(row),
        "filled_amount": _batch_optional_float(row, "filled_amount", "filled"),
        "left": _batch_optional_float(row, "left"),
        "avg_deal_price": avg,
        "fill_price": fill_price,
    }


def _shadow_batch_ack(row: dict, requested: dict | None = None) -> dict:
    """Paper ACK. Same keys as a Gate batch row. No create_order.

    A shadow fill reports the requested amount and the caller price. It does
    not invent a price when the caller did not pass one.
    """
    src = requested if isinstance(requested, dict) else {}
    amount = row.get("amount")
    if amount in (None, ""):
        amount = src.get("amount")
    price = src.get("price")
    if price in (None, ""):
        price = row.get("price")
    if price in (None, ""):
        price = None
    return {
        "succeeded": True,
        "label": "",
        "message": "",
        "text": str(row.get("text") or ""),
        "currency_pair": str(row.get("currency_pair") or ""),
        "id": f"shadow-{uuid.uuid4().hex[:12]}",
        "status": "closed",
        "account": "spot",
        "side": "sell",
        "type": "market",
        "finish_as": "filled",
        "filled_amount": amount,
        "left": "0",
        "avg_deal_price": price,
        "fill_price": price,
    }