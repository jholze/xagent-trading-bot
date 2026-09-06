"""Single-source trade cost model (fees + adversarial slippage).

Gate spot ``feeSide: get``: buy fee in base (fewer coins), sell fee in quote
(less USDT). See ``docs/umbau/costmodel-design.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

from logger import log

COST_MODEL_VERSION = "2026-09-v1"
Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
FeeSide = Literal["base", "quote"]

_MISSING_BLOCK_WARNED: set[str] = set()
_SOURCE_INFO_LOGGED: set[str] = set()
_EXCHANGE_FEE_CACHE: dict[tuple[str, str, str], CostParams | None] = {}

_VIP0_SPOT = {
    "fee_maker_pct": 0.2,
    "fee_taker_pct": 0.2,
    "slippage_pct": 0.15,
    "fee_side_buy": "base",
    "fee_side_sell": "quote",
}
_VIP0_SWAP = {
    "fee_maker_pct": 0.02,
    "fee_taker_pct": 0.05,
    "slippage_pct": 0.10,
    "fee_side_buy": "quote",
    "fee_side_sell": "quote",
}


@dataclass(frozen=True)
class CostParams:
    fee_maker_pct: float  # % of notional
    fee_taker_pct: float
    slippage_pct: float  # % of price, always adversarial
    fee_side_buy: FeeSide = "base"  # Gate spot 'get'
    fee_side_sell: FeeSide = "quote"
    funding_rate_8h: float | None = None  # swap only
    source: str = "config"  # "config" | "exchange"


@dataclass(frozen=True)
class Fill:
    side: Side
    order_type: OrderType
    request_price: float
    fill_price: float  # after slippage
    qty_gross: float  # base, before fee
    qty_net: float  # base actually credited (buy) / debited (sell)
    quote_gross: float  # fill_price · qty_gross
    quote_net: float  # buy: USDT spent; sell: USDT credited (− fee_quote)
    fee_base: float
    fee_quote: float
    fee_usdt: float  # fee_base · fill_price + fee_quote — reporting only
    slippage_usdt: float  # |fill_price − request_price| · qty_gross
    cost_model_version: str = COST_MODEL_VERSION


def _config_dict(config: object | None) -> dict:
    if config is None:
        return {}
    if isinstance(config, dict):
        return config
    raw = getattr(config, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _vip0_block(market: str) -> dict:
    return dict(_VIP0_SWAP if str(market).lower() == "swap" else _VIP0_SPOT)


def _as_fee_side(value: object, default: FeeSide) -> FeeSide:
    raw = str(value or default).strip().lower()
    return "quote" if raw == "quote" else "base"


def _params_from_block(block: dict, *, market: str, source: str) -> CostParams:
    defaults = _vip0_block(market)
    funding = block.get("funding_rate_8h", defaults.get("funding_rate_8h"))
    try:
        funding_f = float(funding) if funding is not None else None
    except (TypeError, ValueError):
        funding_f = None
    return CostParams(
        fee_maker_pct=float(block.get("fee_maker_pct", defaults["fee_maker_pct"])),
        fee_taker_pct=float(block.get("fee_taker_pct", defaults["fee_taker_pct"])),
        slippage_pct=float(block.get("slippage_pct", defaults["slippage_pct"])),
        fee_side_buy=_as_fee_side(block.get("fee_side_buy"), defaults["fee_side_buy"]),
        fee_side_sell=_as_fee_side(block.get("fee_side_sell"), defaults["fee_side_sell"]),
        funding_rate_8h=funding_f,
        source=source,
    )


def _tier_for_symbol(symbol: str, config: dict) -> str | None:
    """Map a symbol onto costs.slippage_by_tier keys (volatile / mid / stable)."""
    try:
        from intelligence.strategy_backtest import classify_coin

        coin_class = classify_coin(symbol)
        if coin_class == "meme":
            return "volatile"
        if coin_class == "large_cap":
            return "stable"
        if coin_class == "mid_cap":
            return "mid"
    except Exception:
        pass
    try:
        from intelligence.volatility_classifier import volatility_tier

        vol_cfg = config.get("volatile_altcoin") or {}
        return volatility_tier({"symbol": symbol}, atr_pct=0.0, volatile_config=vol_cfg)
    except Exception:
        return None


def _looks_live(raw: dict) -> bool:
    if os.environ.get("PYTEST_RUNNING"):
        return False
    live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
    if live.get("dry_run", True):
        return False
    mode = str(raw.get("trading_mode") or "")
    if mode != "live" or not raw.get("live_confirmed"):
        return False
    key_env = str(live.get("api_key_env") or "GATE_API_KEY")
    secret_env = str(live.get("api_secret_env") or "GATE_API_SECRET")
    return bool(os.getenv(key_env) and os.getenv(secret_env))


def _try_fetch_exchange_fees(
    *,
    exchange: str,
    market: str,
    symbol: str | None,
    raw: dict,
    base: CostParams,
) -> CostParams | None:
    """Best-effort live fee overlay. Never raises; never hits the network under pytest."""
    cache_key = (exchange, market, symbol or "*")
    if cache_key in _EXCHANGE_FEE_CACHE:
        return _EXCHANGE_FEE_CACHE[cache_key]
    if not _looks_live(raw):
        _EXCHANGE_FEE_CACHE[cache_key] = None
        return None
    try:
        import ccxt

        live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
        key_env = str(live.get("api_key_env") or "GATE_API_KEY")
        secret_env = str(live.get("api_secret_env") or "GATE_API_SECRET")
        ex_cls = getattr(ccxt, exchange, None)
        if ex_cls is None:
            _EXCHANGE_FEE_CACHE[cache_key] = None
            return None
        client = ex_cls(
            {
                "apiKey": os.getenv(key_env, ""),
                "secret": os.getenv(secret_env, ""),
                "enableRateLimit": True,
                "timeout": 8000,
            }
        )
        fees = None
        if symbol and hasattr(client, "fetch_trading_fee"):
            try:
                fees = client.fetch_trading_fee(symbol)
            except Exception:
                fees = None
        if fees is None and hasattr(client, "fetch_trading_fees"):
            all_fees = client.fetch_trading_fees()
            if symbol and isinstance(all_fees, dict):
                fees = all_fees.get(symbol) or all_fees.get(symbol.replace("/", "_"))
            elif isinstance(all_fees, dict):
                trading = all_fees.get("trading") if isinstance(all_fees.get("trading"), dict) else all_fees
                fees = trading
        if not isinstance(fees, dict):
            _EXCHANGE_FEE_CACHE[cache_key] = None
            return None
        maker = fees.get("maker")
        taker = fees.get("taker")
        if maker is None and taker is None:
            _EXCHANGE_FEE_CACHE[cache_key] = None
            return None
        # ccxt maker/taker are fractions (0.002 == 0.2 %).
        maker_pct = float(maker if maker is not None else taker) * 100.0
        taker_pct = float(taker if taker is not None else maker) * 100.0
        overlay = replace(
            base,
            fee_maker_pct=maker_pct,
            fee_taker_pct=taker_pct,
            source="exchange",
        )
        _EXCHANGE_FEE_CACHE[cache_key] = overlay
        return overlay
    except Exception as exc:
        log(f"CostModel: fetchTradingFees failed ({exc}); using config", "WARNING")
        _EXCHANGE_FEE_CACHE[cache_key] = None
        return None


def _normalize_order_type(order_type: str | None) -> OrderType:
    ot = str(order_type or "market").strip().lower()
    if ot in ("limit", "maker"):
        return "limit"
    return "market"


def _fill(
    *,
    side: Side,
    order_type: OrderType,
    request_price: float,
    fill_price: float,
    qty_gross: float,
    qty_net: float,
    quote_net: float,
    fee_base: float,
    fee_quote: float,
) -> Fill:
    quote_gross = fill_price * qty_gross
    return Fill(
        side=side,
        order_type=order_type,
        request_price=request_price,
        fill_price=fill_price,
        qty_gross=qty_gross,
        qty_net=qty_net,
        quote_gross=quote_gross,
        quote_net=quote_net,
        fee_base=fee_base,
        fee_quote=fee_quote,
        fee_usdt=fee_base * fill_price + fee_quote,
        slippage_usdt=abs(fill_price - request_price) * qty_gross,
        cost_model_version=COST_MODEL_VERSION,
    )


class CostModel:
    def __init__(self, params: CostParams, *, exchange: str = "gate", market: str = "spot"):
        self.params = params
        self.exchange = exchange
        self.market = market

    @classmethod
    def from_config(
        cls,
        config: dict | None,
        *,
        exchange: str = "gate",
        market: str = "spot",
        symbol: str | None = None,
    ) -> "CostModel":
        """Read config["costs"][exchange][market]. Missing block → VIP-0 + WARNING once.

        ``symbol`` selects slippage from ``costs.slippage_by_tier`` via volatility
        tier; otherwise the market's ``slippage_pct`` is used.
        """
        raw = _config_dict(config)
        costs = raw.get("costs") if isinstance(raw.get("costs"), dict) else {}
        exchange_key = str(exchange or "gate")
        market_key = str(market or "spot")
        ex_block = costs.get(exchange_key) if isinstance(costs.get(exchange_key), dict) else {}
        missing = market_key not in ex_block
        block = ex_block.get(market_key) if isinstance(ex_block.get(market_key), dict) else {}
        if missing:
            warn_key = f"{exchange_key}/{market_key}"
            if warn_key not in _MISSING_BLOCK_WARNED:
                _MISSING_BLOCK_WARNED.add(warn_key)
                log(
                    f"CostModel: config['costs'][{exchange_key}][{market_key}] missing "
                    f"— using VIP-0 defaults",
                    "WARNING",
                )
            params = _params_from_block({}, market=market_key, source="config")
        else:
            params = _params_from_block(block, market=market_key, source="config")

        fee_source = str(costs.get("fee_source") or "auto").strip().lower()
        if fee_source == "auto":
            fetched = _try_fetch_exchange_fees(
                exchange=exchange_key,
                market=market_key,
                symbol=symbol,
                raw=raw,
                base=params,
            )
            if fetched is not None:
                params = fetched

        by_tier = costs.get("slippage_by_tier") if isinstance(costs.get("slippage_by_tier"), dict) else {}
        if symbol and by_tier:
            tier = _tier_for_symbol(symbol, raw)
            if tier and tier in by_tier:
                try:
                    params = replace(params, slippage_pct=float(by_tier[tier]))
                except (TypeError, ValueError):
                    pass

        src_key = f"{params.source}:{exchange_key}/{market_key}"
        if src_key not in _SOURCE_INFO_LOGGED:
            _SOURCE_INFO_LOGGED.add(src_key)
            log(
                f"CostModel: fee source={params.source} ({exchange_key}/{market_key})",
                "INFO",
            )
        return cls(params, exchange=exchange_key, market=market_key)

    def fee_pct(self, order_type: OrderType = "market") -> float:
        """Percent of notional. market/taker → taker, limit/maker → maker."""
        ot = _normalize_order_type(order_type)
        if ot == "limit":
            return float(self.params.fee_maker_pct)
        return float(self.params.fee_taker_pct)

    def round_trip_pct(self, order_type: OrderType = "market") -> float:
        """2·fee + 2·slippage, as percent."""
        return 2.0 * self.fee_pct(order_type) + 2.0 * float(self.params.slippage_pct)

    def _fee_frac(self, order_type: OrderType) -> float:
        return self.fee_pct(order_type) / 100.0

    def _slip_frac(self) -> float:
        return float(self.params.slippage_pct) / 100.0

    def simulate_buy(
        self,
        price: float,
        *,
        usdt: float | None = None,
        qty: float | None = None,
        order_type: OrderType = "market",
    ) -> Fill:
        if (usdt is None) == (qty is None):
            raise ValueError("simulate_buy requires exactly one of usdt or qty")
        if price <= 0:
            raise ValueError("simulate_buy requires price > 0")
        ot = _normalize_order_type(order_type)
        fee = self._fee_frac(ot)
        if fee >= 1.0:
            raise ValueError("fee_pct must be < 100")
        fill_price = float(price) * (1.0 + self._slip_frac())
        side_buy = self.params.fee_side_buy

        if usdt is not None:
            if usdt <= 0:
                raise ValueError("simulate_buy requires usdt > 0")
            qty_gross = float(usdt) / fill_price
            spent = float(usdt)
        else:
            if qty <= 0:
                raise ValueError("simulate_buy requires qty > 0")
            if side_buy == "quote":
                qty_gross = float(qty)
            else:
                qty_gross = float(qty) / (1.0 - fee)
            spent = qty_gross * fill_price

        if side_buy == "quote":
            fee_base = 0.0
            fee_quote = spent * fee
            qty_net = qty_gross
            quote_net = spent + fee_quote
        else:
            fee_base = qty_gross * fee
            fee_quote = 0.0
            qty_net = qty_gross - fee_base
            quote_net = spent

        return _fill(
            side="buy",
            order_type=ot,
            request_price=float(price),
            fill_price=fill_price,
            qty_gross=qty_gross,
            qty_net=qty_net,
            quote_net=quote_net,
            fee_base=fee_base,
            fee_quote=fee_quote,
        )

    def simulate_sell(
        self,
        price: float,
        qty: float,
        *,
        order_type: OrderType = "market",
    ) -> Fill:
        if price <= 0:
            raise ValueError("simulate_sell requires price > 0")
        if qty <= 0:
            raise ValueError("simulate_sell requires qty > 0")
        ot = _normalize_order_type(order_type)
        fee = self._fee_frac(ot)
        fill_price = float(price) * (1.0 - self._slip_frac())
        qty_gross = float(qty)
        quote_gross = qty_gross * fill_price
        if self.params.fee_side_sell == "base":
            fee_base = qty_gross * fee
            fee_quote = 0.0
            qty_net = qty_gross  # sold qty; fee_base is extra base taken on 'get' sell (rare)
            quote_net = quote_gross
        else:
            fee_base = 0.0
            fee_quote = quote_gross * fee
            qty_net = qty_gross
            quote_net = quote_gross - fee_quote
        return _fill(
            side="sell",
            order_type=ot,
            request_price=float(price),
            fill_price=fill_price,
            qty_gross=qty_gross,
            qty_net=qty_net,
            quote_net=quote_net,
            fee_base=fee_base,
            fee_quote=fee_quote,
        )

    def fill_from_exchange(
        self,
        raw: dict,
        *,
        side: Side,
        base: str,
        quote: str,
        request_price: float,
        order_type: OrderType = "market",
    ) -> Fill:
        """ccxt order dict → Fill. Unknown fee currency raises ValueError (never guess)."""
        if not isinstance(raw, dict):
            raise ValueError("fill_from_exchange requires a ccxt order dict")
        ot = _normalize_order_type(order_type)
        fill_price = float(raw.get("average") or raw.get("price") or request_price or 0)
        qty_gross = float(raw.get("filled") or 0)
        cost = float(raw.get("cost") or 0)
        quote_gross = cost if cost > 0 else fill_price * qty_gross

        fee_info = raw.get("fee") or {}
        fee_base = 0.0
        fee_quote = 0.0
        if isinstance(fee_info, dict):
            fee_cost = float(fee_info.get("cost") or 0)
            fee_ccy = str(fee_info.get("currency") or "").strip()
            if fee_cost != 0:
                ccy = fee_ccy.upper()
                base_u = str(base or "").upper()
                quote_u = str(quote or "").upper()
                if not ccy:
                    raise ValueError(
                        f"Unknown fee currency {fee_ccy!r} (base={base} quote={quote})"
                    )
                if ccy == base_u:
                    fee_base = fee_cost
                elif ccy == quote_u:
                    fee_quote = fee_cost
                else:
                    raise ValueError(
                        f"Unknown fee currency {fee_ccy!r} (base={base} quote={quote})"
                    )
        elif fee_info:
            raise ValueError(
                f"Unknown fee currency {fee_info!r} (base={base} quote={quote})"
            )

        side_n: Side = "sell" if str(side).lower() == "sell" else "buy"
        if side_n == "buy":
            qty_net = qty_gross - fee_base
            quote_net = quote_gross + fee_quote
        else:
            qty_net = qty_gross
            quote_net = quote_gross - fee_quote

        return Fill(
            side=side_n,
            order_type=ot,
            request_price=float(request_price),
            fill_price=fill_price,
            qty_gross=qty_gross,
            qty_net=qty_net,
            quote_gross=quote_gross,
            quote_net=quote_net,
            fee_base=fee_base,
            fee_quote=fee_quote,
            fee_usdt=fee_base * fill_price + fee_quote,
            slippage_usdt=abs(fill_price - float(request_price)) * qty_gross,
            cost_model_version=COST_MODEL_VERSION,
        )

    @staticmethod
    def realized_pnl(*, qty_sold: float, avg_entry_net: float, sell: Fill) -> float:
        """sell.quote_net − qty_sold · avg_entry_net. Constructively Σpnl == ΔCash − Δcost basis."""
        return float(sell.quote_net) - float(qty_sold) * float(avg_entry_net)


def trade_cost_fields(fill: Fill) -> dict:
    """Fields every new trade record must carry."""
    return {
        "fee_base": fill.fee_base,
        "fee_quote": fill.fee_quote,
        "fee_usdt": fill.fee_usdt,
        "slippage_usdt": fill.slippage_usdt,
        "cost_model": fill.cost_model_version,
    }
