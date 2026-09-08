"""Boot-time exchange recovery (#314 §5 / §7).

``services/ledger_sync.reconcile_*`` stays ledger-internal. This module is
the only place that asks the exchange what it actually holds.

Called once per tenant before its first trading cycle (hook:
``architecture_runtime.ensure_started``). Shadow never hits the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.costs import CostModel
from core.models import OrderStatus, TradeOrder, TradeResult
from core.operator_notify import notify_operator
from core.stablecoins import STABLECOIN_BASES
from core.tenant_context import tenant_context
from logger import log
from strategies.positions import (
    DUST_AMOUNT_EPSILON,
    flush_positions,
    get_position,
    is_positions_state_unknown,
    list_active_positions,
)

_SHADOW_SKIP_LOGGED: set[str] = set()

_DEFAULT_TF = "4h"
_META_BALANCE_KEYS = frozenset(
    {"info", "free", "used", "total", "datetime", "timestamp", "debt"}
)


class RecoveryFailed(RuntimeError):
    """Exchange unreachable or swap margin is not isolated. Do not run the cycle."""


@dataclass
class RecoveryReport:
    skipped: bool = False
    reason: str = ""
    tenant_id: str = ""
    scope: str = ""
    divergences: list[dict] = field(default_factory=list)
    orders_resolved: list[dict] = field(default_factory=list)
    positions_without_stop: list[str] = field(default_factory=list)


def reset_recovery_log_for_tests() -> None:
    _SHADOW_SKIP_LOGGED.clear()


def reconcile_with_exchange(
    *,
    tenant_id: str,
    scope: str,
    adapter,
    config,
) -> RecoveryReport:
    """Compare ledger vs exchange; exchange wins. Shadow returns skipped."""
    tid = str(tenant_id or "").strip() or "default"
    sc = str(scope or "").strip() or "paper"
    report = RecoveryReport(tenant_id=tid, scope=sc)
    mode = _adapter_mode(adapter, config)
    if mode == "shadow":
        if tid not in _SHADOW_SKIP_LOGGED:
            _SHADOW_SKIP_LOGGED.add(tid)
            log(f"exchange recovery skipped (shadow) tenant={tid}", "INFO")
        report.skipped = True
        report.reason = "shadow"
        return report
    if mode not in ("real", "testnet"):
        raise RecoveryFailed(f"unknown adapter mode {mode!r}")

    with tenant_context(tid, scope=sc):
        return _reconcile_live(report, adapter=adapter, config=config, mode=mode)


def _reconcile_live(
    report: RecoveryReport,
    *,
    adapter,
    config,
    mode: str,
) -> RecoveryReport:
    tid, sc = report.tenant_id, report.scope
    exchange = _ccxt_client(adapter)
    if exchange is None:
        _abort(tid, sc, "exchange client unavailable")

    try:
        balance = _require_fetch(exchange, "fetch_balance", tid, sc)
        open_orders = _require_fetch(exchange, "fetch_open_orders", tid, sc)
        if open_orders is None:
            open_orders = []
        if not isinstance(open_orders, list):
            open_orders = list(open_orders) if open_orders else []
    except RecoveryFailed:
        raise

    orders = _load_orders(sc, tid)
    lots = list_active_positions(tenant_id=tid, scope=sc)
    swap = _tenant_uses_swap(config, orders, lots)

    exchange_positions: list[dict] = []
    if swap:
        exchange_positions = _fetch_positions(exchange, tid, sc)
        _assert_isolated_margin(
            exchange,
            _swap_symbols(orders, lots, exchange_positions),
            exchange_positions,
            tid,
            sc,
        )

    report.orders_resolved = _resolve_in_flight_orders(
        orders,
        exchange=exchange,
        adapter=adapter,
        config=config,
        scope=sc,
    )

    if is_positions_state_unknown(tenant_id=tid, scope=sc):
        # #318 owns the cycle skip. Do not invent a book from exchange balances.
        log(
            f"exchange recovery: positions state unknown for {tid}/{sc} "
            "— skipping position rewrite (#318)",
            "ERROR",
        )
    else:
        lots = list_active_positions(tenant_id=tid, scope=sc)
        report.divergences = _reconcile_positions(
            lots,
            balance=balance if isinstance(balance, dict) else {},
            exchange_positions=exchange_positions,
            exchange=exchange,
            swap=swap,
            config=config,
        )
        if report.divergences:
            _emit_divergences(tid, sc, report.divergences)

    lots = list_active_positions(tenant_id=tid, scope=sc)
    report.positions_without_stop = _positions_without_stop(lots, open_orders)
    return report


# ---------------------------------------------------------------------------
# Mode / client
# ---------------------------------------------------------------------------


def _raw_config(config) -> dict:
    if isinstance(config, dict):
        return config
    raw = getattr(config, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _adapter_mode(adapter, config) -> str:
    mode = getattr(adapter, "mode", None)
    if isinstance(mode, str) and mode.strip():
        token = mode.strip().lower()
        if token in ("shadow", "testnet", "real"):
            return token
    from core.execution_mode import resolve_execution_mode

    return resolve_execution_mode(_raw_config(config)).adapter_mode


def _ccxt_client(adapter):
    if adapter is None:
        return None
    cached = getattr(adapter, "_exchange", None)
    if cached is not None:
        return cached
    getter = getattr(adapter, "_get_exchange", None)
    if callable(getter):
        try:
            return getter()
        except Exception as e:
            if _is_unreachable(e):
                raise RecoveryFailed(f"exchange unreachable: {e}") from e
            raise RecoveryFailed(f"exchange client unavailable: {e}") from e
    if hasattr(adapter, "fetch_balance"):
        return adapter
    return None


def _is_unreachable(exc: BaseException) -> bool:
    try:
        import ccxt
    except ImportError:
        ccxt = None  # type: ignore[assignment]
    if ccxt is not None:
        for name in (
            "NetworkError",
            "ExchangeNotAvailable",
            "AuthenticationError",
            "RequestTimeout",
            "PermissionDenied",
        ):
            cls = getattr(ccxt, name, None)
            if isinstance(cls, type) and isinstance(exc, cls):
                return True
        auth = getattr(ccxt, "AuthenticationError", None)
        if auth is not None and isinstance(exc, auth):
            return True
    msg = str(exc).lower()
    needles = (
        "invalid api",
        "invalid key",
        "api key",
        "not login",
        "authentication",
        "signature",
        "401",
        "403",
        "exchange not available",
        "network",
        "timeout",
    )
    return any(n in msg for n in needles)


def _is_not_supported(exc: BaseException) -> bool:
    try:
        import ccxt
    except ImportError:
        return "not supported" in str(exc).lower()
    cls = getattr(ccxt, "NotSupported", None)
    if isinstance(cls, type) and isinstance(exc, cls):
        return True
    return "not supported" in str(exc).lower()


def _abort(tenant_id: str, scope: str, message: str) -> None:
    text = str(message)
    log(f"RecoveryFailed tenant={tenant_id} scope={scope}: {text}", "ERROR")
    try:
        notify_operator(
            f"🛑 <b>RecoveryFailed</b> tenant=<code>{tenant_id}</code> "
            f"scope=<code>{scope}</code>\n{text}"
        )
    except Exception as e:
        log(f"operator notify failed during RecoveryFailed: {e}", "WARNING")
    raise RecoveryFailed(text)


def _require_fetch(exchange, method: str, tenant_id: str, scope: str, *args, **kwargs):
    fn = getattr(exchange, method, None)
    if not callable(fn):
        _abort(tenant_id, scope, f"exchange.{method} is not available")
    try:
        return fn(*args, **kwargs)
    except TypeError:
        try:
            return fn(*args)
        except Exception as e:
            if _is_unreachable(e) or not _is_not_supported(e):
                _abort(tenant_id, scope, f"exchange unreachable ({method}): {e}")
            raise
    except Exception as e:
        if _is_not_supported(e) and method != "fetch_balance":
            return [] if method in ("fetch_open_orders", "fetch_positions") else None
        _abort(tenant_id, scope, f"exchange unreachable ({method}): {e}")


def _fetch_positions(exchange, tenant_id: str, scope: str) -> list[dict]:
    fn = getattr(exchange, "fetch_positions", None)
    if not callable(fn):
        return []
    try:
        raw = fn()
    except TypeError:
        try:
            raw = fn(None)
        except Exception as e:
            if _is_not_supported(e):
                return []
            _abort(tenant_id, scope, f"exchange unreachable (fetch_positions): {e}")
            return []
    except Exception as e:
        if _is_not_supported(e):
            return []
        _abort(tenant_id, scope, f"exchange unreachable (fetch_positions): {e}")
        return []
    if not raw:
        return []
    return [p for p in raw if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# §7 isolated margin — Gate ccxt 4.5.48
# ---------------------------------------------------------------------------
# fetch_margin_mode / fetch_position_mode: has[] False (NotSupported).
# fetch_leverage: spot (and unified) only — raises NotSupported for swap.
# fetch_position → privateFuturesGetSettlePositionsContract; parse_position
# sets marginMode from leverage ('0' → cross, else isolated). That is the
# Gate-specific endpoint 4.5.48 actually exposes for swap isolated-vs-cross.


def _tenant_uses_swap(config, orders: list[dict], lots: list[dict]) -> bool:
    from strategies.short_policy import shorts_allow_live, shorts_enabled

    raw = _raw_config(config)
    if shorts_enabled(raw) or shorts_allow_live(raw):
        return True
    for lot in lots:
        if str(lot.get("side") or "").lower() == "short":
            return True
    for order in orders:
        if str(order.get("side") or "").lower() in ("short", "cover"):
            return True
    return False


def _as_swap_symbol(symbol: str) -> str:
    s = str(symbol or "").strip()
    if not s:
        return s
    if ":" in s:
        return s
    if "/" in s:
        return f"{s}:USDT" if ":" not in s else s
    return s


def _spot_symbol(symbol: str) -> str:
    s = str(symbol or "").strip()
    if ":" in s:
        return s.split(":", 1)[0]
    return s


def _swap_symbols(
    orders: list[dict],
    lots: list[dict],
    exchange_positions: list[dict],
) -> list[str]:
    symbols: set[str] = set()
    for lot in lots:
        if str(lot.get("side") or "").lower() == "short":
            symbols.add(_as_swap_symbol(str(lot.get("symbol") or "")))
    for order in orders:
        if str(order.get("side") or "").lower() in ("short", "cover"):
            symbols.add(_as_swap_symbol(str(order.get("symbol") or "")))
    for pos in exchange_positions:
        qty = _position_contracts(pos)
        if qty > DUST_AMOUNT_EPSILON:
            sym = str(pos.get("symbol") or "")
            if sym:
                symbols.add(sym)
    return sorted(s for s in symbols if s)


def _margin_mode_of(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("marginMode", "margin_mode"):
        raw = payload.get(key)
        if raw:
            return str(raw).strip().lower()
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    for key in ("marginMode", "margin_mode", "mode"):
        raw = info.get(key)
        if raw:
            token = str(raw).strip().lower()
            if token in ("isolated", "cross"):
                return token
    leverage = payload.get("leverage")
    if leverage is not None and str(leverage).strip() != "":
        try:
            return "cross" if float(leverage) == 0 else "isolated"
        except (TypeError, ValueError):
            if str(leverage).strip() == "0":
                return "cross"
    return ""


def _read_margin_mode(exchange, symbol: str) -> str:
    """Prefer fetch_position (Gate swap). Fall back to list / fetch_margin_mode."""
    swap_sym = _as_swap_symbol(symbol)
    for ident in (swap_sym, symbol):
        fn = getattr(exchange, "fetch_position", None)
        if callable(fn):
            try:
                raw = fn(ident)
            except TypeError:
                try:
                    raw = fn(ident, {})
                except Exception:
                    raw = None
            except Exception as e:
                if _is_unreachable(e):
                    raise
                raw = None
            mode = _margin_mode_of(raw)
            if mode:
                return mode
    fn = getattr(exchange, "fetch_margin_mode", None)
    if callable(fn):
        try:
            raw = fn(swap_sym)
            mode = _margin_mode_of(raw)
            if mode:
                return mode
        except Exception as e:
            if _is_unreachable(e):
                raise
            if not _is_not_supported(e):
                log(f"fetch_margin_mode({swap_sym}) failed: {e}", "WARNING")
    fn = getattr(exchange, "fetch_leverage", None)
    if callable(fn):
        try:
            raw = fn(swap_sym)
            mode = _margin_mode_of(raw)
            if mode:
                return mode
        except Exception as e:
            if _is_unreachable(e):
                raise
    return ""


def _mode_from_positions(symbol: str, exchange_positions: list[dict]) -> str:
    spot = _spot_symbol(symbol)
    swap = _as_swap_symbol(symbol)
    for pos in exchange_positions:
        raw_sym = str(pos.get("symbol") or "")
        if raw_sym in (symbol, spot, swap) or _spot_symbol(raw_sym) == spot:
            mode = _margin_mode_of(pos)
            if mode:
                return mode
    return ""


def _assert_isolated_margin(
    exchange,
    symbols: list[str],
    exchange_positions: list[dict],
    tenant_id: str,
    scope: str,
) -> None:
    if not symbols:
        return
    seen: set[str] = set()
    for symbol in symbols:
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        mode = _mode_from_positions(symbol, exchange_positions)
        if not mode:
            try:
                mode = _read_margin_mode(exchange, symbol)
            except Exception as e:
                if _is_unreachable(e):
                    _abort(tenant_id, scope, f"exchange unreachable (margin mode): {e}")
                raise
        if not mode:
            _abort(
                tenant_id,
                scope,
                f"margin mode unknown for {symbol}; "
                "short_math.liquidation_price_isolated would be wrong",
            )
        if mode != "isolated":
            _abort(
                tenant_id,
                scope,
                f"margin mode {mode} for {symbol}; "
                "short_math.liquidation_price_isolated would be wrong",
            )


# ---------------------------------------------------------------------------
# In-flight orders
# ---------------------------------------------------------------------------


def _load_orders(scope: str, tenant_id: str) -> list[dict]:
    from data_manager import load_orders
    from storage.errors import LedgerUnavailable

    try:
        doc = load_orders(scope, tenant_id=tenant_id)
    except LedgerUnavailable as e:
        _abort(tenant_id, scope, f"ledger unavailable: {e}")
        return []
    except Exception as e:
        if isinstance(e, RecoveryFailed):
            raise
        _abort(tenant_id, scope, f"ledger unavailable: {e}")
        return []
    out = []
    for order in doc.get("orders") or []:
        if not isinstance(order, dict):
            continue
        oid = str(order.get("tenant_id") or tenant_id or "")
        if oid and oid != tenant_id:
            continue
        out.append(order)
    return out


def _needs_order_reconcile(order: dict) -> bool:
    if bool(order.get("needs_reconcile")):
        return True
    return OrderStatus.try_legacy(order.get("status")) is OrderStatus.ACTIVE


def _order_ids(order: dict) -> list[str]:
    execution = order.get("execution") if isinstance(order.get("execution"), dict) else {}
    ids = [
        order.get("exchange_order_id"),
        execution.get("exchange_order_id"),
        order.get("client_order_id"),
        order.get("idempotency_key"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        token = str(raw or "").strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _fetch_order_raw(exchange, order: dict):
    symbol = str(order.get("symbol") or "")
    last_err: Exception | None = None
    for ident in _order_ids(order):
        fn = getattr(exchange, "fetch_order", None)
        if not callable(fn):
            break
        for args, kwargs in (
            ((ident, symbol), {}),
            ((ident,), {}),
            ((ident, symbol), {"clientOrderId": ident}),
        ):
            try:
                raw = fn(*args, **kwargs) if kwargs else fn(*args)
            except TypeError:
                try:
                    raw = fn(ident, symbol)
                except Exception as e:
                    last_err = e
                    continue
            except Exception as e:
                if _is_unreachable(e):
                    raise
                last_err = e
                continue
            if isinstance(raw, dict) and raw:
                return raw
    if last_err is not None and _is_unreachable(last_err):
        raise last_err
    return None


def _trade_order_from_record(rec: dict) -> TradeOrder:
    side = str(rec.get("side") or "buy").strip().lower()
    typ = {"buy": "BUY", "sell": "SELL", "short": "SHORT", "cover": "COVER"}.get(
        side, "BUY"
    )
    req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
    execution = rec.get("execution") if isinstance(rec.get("execution"), dict) else {}
    qty = float(rec.get("qty") or req.get("amount") or 0) or 0.0
    price = float(req.get("price") or execution.get("price") or rec.get("price") or 0)
    return TradeOrder(
        typ,
        str(rec.get("symbol") or ""),
        price,
        qty,
        usdt_amount=float(req.get("usdt") or execution.get("usdt") or 0) or 0.0,
        signal=str(rec.get("signal") or ""),
        source=str(rec.get("source") or "auto"),
        order_id=str(rec.get("id") or ""),
        client_order_id=str(rec.get("client_order_id") or rec.get("idempotency_key") or ""),
        idempotency_key=str(rec.get("idempotency_key") or rec.get("client_order_id") or ""),
        exchange_order_id=str(
            rec.get("exchange_order_id") or execution.get("exchange_order_id") or ""
        ),
        filled_qty=float(rec.get("filled_qty") or 0) or 0.0,
        status=OrderStatus.try_legacy(rec.get("status")) or OrderStatus.ACTIVE,
    )


def _link_result(scope: str, order_id: str, result: TradeResult, trade_order: TradeOrder) -> None:
    from services.order_service import OrderService

    svc = OrderService(scope)
    svc.link_execution_result(order_id, result, trade_order)
    # Terminal resolve always clears the in-flight flag.
    st = getattr(result, "order_status", None)
    if st in (
        OrderStatus.EXECUTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    ):
        data = svc._load()
        rec = svc._find(data, order_id=order_id)
        if rec is not None and rec.get("needs_reconcile"):
            rec["needs_reconcile"] = False
            svc._save(data)


def _resolve_in_flight_orders(
    orders: list[dict],
    *,
    exchange,
    adapter,
    config,
    scope: str,
) -> list[dict]:
    resolved: list[dict] = []
    finalize = getattr(adapter, "_finalize_exchange_order", None)
    use_adapter = callable(finalize) and type(adapter).__name__ == "GateExecutionAdapter"
    for rec in orders:
        if not _needs_order_reconcile(rec):
            continue
        order_id = str(rec.get("id") or "")
        symbol = str(rec.get("symbol") or "")
        try:
            raw = _fetch_order_raw(exchange, rec)
        except Exception as e:
            if _is_unreachable(e):
                _abort(
                    rec.get("tenant_id") or "",
                    scope,
                    f"exchange unreachable (fetch_order): {e}",
                )
            log(f"fetch_order failed for {order_id} {symbol}: {e}", "WARNING")
            continue
        if not isinstance(raw, dict) or not raw:
            log(
                f"in-flight order {order_id} {symbol} not found on exchange — left ACTIVE",
                "WARNING",
            )
            continue
        trade_order = _trade_order_from_record(rec)
        side = trade_order.type.lower() if trade_order.type in ("BUY", "SELL") else "buy"
        tf = str(rec.get("timeframe") or _DEFAULT_TF)
        qty = float(trade_order.qty or 0)
        try:
            if use_adapter:
                result = finalize(
                    exchange,
                    trade_order,
                    raw,
                    side=side,
                    qty=qty,
                    timeframe=tf,
                    usdt=trade_order.usdt_amount,
                )
            else:
                result = _apply_raw_without_adapter(
                    raw, trade_order, adapter=adapter, config=config, timeframe=tf
                )
        except Exception as e:
            if _is_unreachable(e):
                _abort("", scope, f"exchange unreachable (resolve order): {e}")
            log(f"resolve in-flight {order_id} {symbol} failed: {e}", "ERROR")
            continue
        if order_id:
            try:
                _link_result(scope, order_id, result, trade_order)
            except Exception as e:
                log(f"ledger link after recovery fill failed for {order_id}: {e}", "ERROR")
        resolved.append(
            {
                "id": order_id,
                "symbol": symbol,
                "status": getattr(result.order_status, "value", result.order_status),
            }
        )
    return resolved


def _apply_raw_without_adapter(
    raw: dict,
    order: TradeOrder,
    *,
    adapter,
    config,
    timeframe: str,
) -> TradeResult:
    token = str(raw.get("status") or "").strip().lower()
    if token in ("canceled", "cancelled", "expired"):
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message=f"exchange {token}",
            order_id=order.order_id,
            exchange_order_id=str(raw.get("id") or ""),
            order_status=OrderStatus.CANCELED,
            needs_reconcile=False,
            order_exist_in_exchange=True,
        )
    if token == "rejected":
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message="exchange rejected",
            order_id=order.order_id,
            exchange_order_id=str(raw.get("id") or ""),
            order_status=OrderStatus.REJECTED,
            needs_reconcile=False,
        )
    filled = raw.get("filled")
    if filled is None:
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message="filled missing",
            order_id=order.order_id,
            order_status=OrderStatus.ACTIVE,
            pending=True,
            needs_reconcile=True,
        )
    filled_f = float(filled)
    requested = float(order.qty or 0)
    if token in ("open", "new") and (requested <= 0 or filled_f < requested):
        if filled_f <= 0:
            return TradeResult(
                False,
                order.type,
                order.symbol,
                message="still open",
                order_id=order.order_id,
                order_status=OrderStatus.ACTIVE,
                pending=True,
                needs_reconcile=True,
            )
    status = OrderStatus.EXECUTED
    if requested > 0 and 0 < filled_f < requested:
        status = OrderStatus.PARTIALLY_FILLED
    side: str = "sell" if order.type == "SELL" else "buy"
    base, _, quote = str(order.symbol).partition("/")
    quote = quote.split(":")[0] or "USDT"
    cm = CostModel.from_config(config, symbol=order.symbol)
    fill = cm.fill_from_exchange(
        raw,
        side=side,  # type: ignore[arg-type]
        base=base,
        quote=quote,
        request_price=float(order.price or 0),
    )
    portfolio = getattr(adapter, "portfolio", None)
    if portfolio is not None and order.type == "BUY":
        portfolio.execute_buy(
            order.symbol,
            timeframe,
            fill.fill_price,
            fill.quote_net,
            source=order.source or "auto",
            order_id=order.order_id or None,
            sync_virtual_ledger=False,
            fill=fill,
        )
    elif portfolio is not None and order.type == "SELL":
        portfolio.execute_sell(
            order.symbol,
            timeframe,
            fill.fill_price,
            order.signal or "SELL",
            fill.qty_net,
            source=order.source or "auto",
            order_id=order.order_id or None,
            sync_virtual_ledger=False,
            fill=fill,
        )
    return TradeResult(
        True,
        order.type,
        order.symbol,
        amount=fill.qty_net,
        price=fill.fill_price,
        usdt_amount=fill.quote_net,
        order_id=order.order_id,
        exchange_order_id=str(raw.get("id") or ""),
        fee=fill.fee_usdt,
        order_status=status,
        filled_qty=filled_f,
        needs_reconcile=False,
        order_exist_in_exchange=True,
    )


# ---------------------------------------------------------------------------
# Positions vs exchange
# ---------------------------------------------------------------------------


def _base_asset(symbol: str) -> str:
    s = _spot_symbol(symbol)
    if "/" in s:
        return s.split("/", 1)[0].upper()
    if "_" in s:
        return s.split("_", 1)[0].upper()
    return s.upper()


def _spot_holdings(balance: dict) -> dict[str, float]:
    holdings: dict[str, float] = {}
    total = balance.get("total") if isinstance(balance.get("total"), dict) else {}
    for asset, qty in total.items():
        try:
            amount = float(qty or 0)
        except (TypeError, ValueError):
            continue
        name = str(asset or "").upper()
        if not name or name in STABLECOIN_BASES:
            continue
        if amount > DUST_AMOUNT_EPSILON:
            holdings[name] = amount
    if holdings:
        return holdings
    for asset, blob in balance.items():
        if str(asset) in _META_BALANCE_KEYS or not isinstance(blob, dict):
            continue
        name = str(asset or "").upper()
        if name in STABLECOIN_BASES:
            continue
        try:
            amount = float(blob.get("total") or blob.get("free") or 0)
        except (TypeError, ValueError):
            continue
        if amount > DUST_AMOUNT_EPSILON:
            holdings[name] = amount
    return holdings


def _position_contracts(pos: dict) -> float:
    for key in ("contracts", "amount", "contractSize"):
        raw = pos.get(key)
        if raw is None:
            continue
        try:
            qty = abs(float(raw))
        except (TypeError, ValueError):
            continue
        if key == "contractSize":
            continue
        size = pos.get("contractSize")
        try:
            cs = float(size) if size not in (None, "", 0) else 1.0
        except (TypeError, ValueError):
            cs = 1.0
        if key == "contracts" and cs and cs != 1.0:
            return qty * cs
        if qty:
            return qty
    info = pos.get("info") if isinstance(pos.get("info"), dict) else {}
    try:
        return abs(float(info.get("size") or 0))
    except (TypeError, ValueError):
        return 0.0


def _swap_holdings(exchange_positions: list[dict]) -> dict[str, dict]:
    """spot-symbol → {amount, entry, margin_mode, raw_symbol}."""
    out: dict[str, dict] = {}
    for pos in exchange_positions:
        qty = _position_contracts(pos)
        if qty <= DUST_AMOUNT_EPSILON:
            continue
        raw_sym = str(pos.get("symbol") or "")
        spot = _spot_symbol(raw_sym)
        entry = pos.get("entryPrice") or pos.get("entry_price") or 0
        try:
            entry_f = float(entry or 0)
        except (TypeError, ValueError):
            entry_f = 0.0
        out[spot] = {
            "amount": qty,
            "entry": entry_f,
            "margin_mode": _margin_mode_of(pos),
            "symbol": raw_sym or spot,
        }
    return out


def _lot_symbol(lot: dict) -> str:
    return str(lot.get("symbol") or "")


def _lot_tf(lot: dict) -> str:
    return str(lot.get("timeframe") or _DEFAULT_TF)


def _lot_amount(lot: dict) -> float:
    try:
        return float(lot.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _primary_lot(lots: list[dict], *, symbol: str | None = None, base: str | None = None) -> dict | None:
    matched = []
    for lot in lots:
        if symbol and _lot_symbol(lot) != symbol and _spot_symbol(_lot_symbol(lot)) != symbol:
            continue
        if base and _base_asset(_lot_symbol(lot)) != base:
            continue
        if symbol or base:
            matched.append(lot)
    if not matched:
        return None
    matched.sort(key=lambda l: _lot_amount(l), reverse=True)
    return matched[0]


def _vwap_from_my_trades(exchange, symbol: str) -> float | None:
    fn = getattr(exchange, "fetch_my_trades", None)
    if not callable(fn):
        return None
    trades = None
    try:
        trades = fn(symbol)
    except TypeError:
        try:
            trades = fn(symbol, None)
        except Exception as e:
            if _is_unreachable(e):
                raise
            log(f"fetch_my_trades({symbol}) failed: {e}", "WARNING")
            return None
    except Exception as e:
        if _is_unreachable(e):
            raise
        log(f"fetch_my_trades({symbol}) failed: {e}", "WARNING")
        return None
    if not trades:
        return None
    notional = 0.0
    qty = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            p = float(t.get("price") or 0)
            a = float(t.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if p > 0 and a > 0:
            notional += p * a
            qty += a
    if qty <= 0:
        return None
    return notional / qty


def _set_lot_amount(
    symbol: str,
    timeframe: str,
    amount: float,
    *,
    entry: float | None = None,
    keep_entry: bool = True,
    entry_unknown: bool = False,
) -> None:
    pos = get_position(symbol, timeframe)
    existing_entry = float(pos.get("average_entry") or pos.get("entry_price") or 0)
    pos["amount"] = Decimal(str(amount))
    if amount > DUST_AMOUNT_EPSILON:
        peak = float(pos.get("peak_amount") or 0)
        if peak < float(amount):
            pos["peak_amount"] = float(amount)
    if keep_entry and existing_entry > 0:
        pass
    elif entry is not None and float(entry) > 0:
        pos["average_entry"] = float(entry)
        if not pos.get("last_buy_price"):
            pos["last_buy_price"] = float(entry)
    if entry_unknown:
        pos["entry_unknown"] = True
        if not pos.get("entry_source"):
            pos["entry_source"] = "recovery_unknown"
    if not pos.get("last_action"):
        pos["last_action"] = "RECOVERY"
    flush_positions(force=True)


def _zero_lots(lots: list[dict], *, base: str | None = None, symbol: str | None = None) -> None:
    for lot in lots:
        if base and _base_asset(_lot_symbol(lot)) != base:
            continue
        if symbol and _spot_symbol(_lot_symbol(lot)) != symbol and _lot_symbol(lot) != symbol:
            continue
        _set_lot_amount(_lot_symbol(lot), _lot_tf(lot), 0.0, keep_entry=True)


def _reconcile_positions(
    lots: list[dict],
    *,
    balance: dict,
    exchange_positions: list[dict],
    exchange,
    swap: bool,
    config,
) -> list[dict]:
    divergences: list[dict] = []
    if swap and exchange_positions:
        holdings = _swap_holdings(exchange_positions)
        ledger_by_spot: dict[str, float] = {}
        for lot in lots:
            spot = _spot_symbol(_lot_symbol(lot))
            ledger_by_spot[spot] = ledger_by_spot.get(spot, 0.0) + _lot_amount(lot)
        keys = set(ledger_by_spot) | set(holdings)
        for spot in sorted(keys):
            ledger_qty = float(ledger_by_spot.get(spot, 0.0))
            ex = holdings.get(spot) or {"amount": 0.0, "entry": 0.0}
            ex_qty = float(ex.get("amount") or 0.0)
            if abs(ledger_qty - ex_qty) <= DUST_AMOUNT_EPSILON:
                continue
            if ex_qty <= DUST_AMOUNT_EPSILON:
                _zero_lots(lots, symbol=spot)
                divergences.append(
                    {"symbol": spot, "ledger": ledger_qty, "exchange": 0.0}
                )
                continue
            primary = _primary_lot(lots, symbol=spot)
            symbol = _lot_symbol(primary) if primary else spot
            tf = _lot_tf(primary) if primary else _DEFAULT_TF
            keep = bool(primary and float(primary.get("average_entry") or 0) > 0)
            entry = float(ex.get("entry") or 0) if not keep else None
            unknown = False
            if not keep and (entry is None or entry <= 0):
                try:
                    vwap = _vwap_from_my_trades(exchange, symbol) or _vwap_from_my_trades(
                        exchange, _as_swap_symbol(symbol)
                    )
                except Exception as e:
                    if _is_unreachable(e):
                        raise
                    vwap = None
                if vwap:
                    entry = vwap
                else:
                    unknown = True
            _set_lot_amount(
                symbol,
                tf,
                ex_qty,
                entry=entry,
                keep_entry=keep,
                entry_unknown=unknown,
            )
            divergences.append(
                {"symbol": symbol, "ledger": ledger_qty, "exchange": ex_qty}
            )
        return divergences

    holdings = _spot_holdings(balance)
    ledger_by_base: dict[str, float] = {}
    for lot in lots:
        base = _base_asset(_lot_symbol(lot))
        if not base or base in STABLECOIN_BASES:
            continue
        ledger_by_base[base] = ledger_by_base.get(base, 0.0) + _lot_amount(lot)
    keys = set(ledger_by_base) | set(holdings)
    for base in sorted(keys):
        ledger_qty = float(ledger_by_base.get(base, 0.0))
        ex_qty = float(holdings.get(base, 0.0))
        if ex_qty <= DUST_AMOUNT_EPSILON:
            ex_qty = 0.0
        if abs(ledger_qty - ex_qty) <= DUST_AMOUNT_EPSILON:
            continue
        primary = _primary_lot(lots, base=base)
        symbol = _lot_symbol(primary) if primary else f"{base}/USDT"
        tf = _lot_tf(primary) if primary else _DEFAULT_TF
        if ex_qty <= DUST_AMOUNT_EPSILON:
            _zero_lots(lots, base=base)
            divergences.append(
                {"symbol": symbol, "ledger": ledger_qty, "exchange": 0.0}
            )
            continue
        keep = bool(primary and float(primary.get("average_entry") or 0) > 0)
        entry = None
        unknown = False
        if not keep:
            try:
                vwap = _vwap_from_my_trades(exchange, symbol)
            except Exception as e:
                if _is_unreachable(e):
                    raise
                vwap = None
            if vwap:
                entry = vwap
            else:
                unknown = True
        _set_lot_amount(
            symbol,
            tf,
            ex_qty,
            entry=entry,
            keep_entry=keep,
            entry_unknown=unknown,
        )
        divergences.append(
            {"symbol": symbol, "ledger": ledger_qty, "exchange": ex_qty}
        )
    return divergences


def _emit_divergences(tenant_id: str, scope: str, divergences: list[dict]) -> None:
    lines = []
    for d in divergences:
        line = (
            f"{d.get('symbol')}: ledger {d.get('ledger')} → exchange {d.get('exchange')}"
        )
        log(f"divergence {line}", "ERROR")
        lines.append(line)
    body = (
        f"⚠️ <b>Exchange recovery divergence</b> "
        f"tenant=<code>{tenant_id}</code> scope=<code>{scope}</code>\n"
        + "\n".join(f"• {ln}" for ln in lines)
    )
    try:
        notify_operator(body)
    except Exception as e:
        log(f"operator notify failed for divergences: {e}", "WARNING")


def _order_is_stop(order: dict, symbol: str) -> bool:
    if not isinstance(order, dict):
        return False
    o_sym = str(order.get("symbol") or "")
    if o_sym and _spot_symbol(o_sym) != _spot_symbol(symbol) and o_sym != symbol:
        return False
    typ = str(order.get("type") or "").lower()
    if "stop" in typ:
        return True
    if order.get("stopPrice") or order.get("stop_price") or order.get("triggerPrice"):
        return True
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    if info.get("stop_price") or info.get("stopPrice") or info.get("triggerPrice"):
        return True
    return False


def _positions_without_stop(lots: list[dict], open_orders: list) -> list[str]:
    out: list[str] = []
    opens = [o for o in (open_orders or []) if isinstance(o, dict)]
    for lot in lots:
        if _lot_amount(lot) <= DUST_AMOUNT_EPSILON:
            continue
        symbol = _lot_symbol(lot)
        tf = _lot_tf(lot)
        if any(_order_is_stop(o, symbol) for o in opens):
            continue
        out.append(f"{symbol} {tf}".strip())
    return out
