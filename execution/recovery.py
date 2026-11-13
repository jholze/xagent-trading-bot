"""Boot-time exchange recovery (#314 §5 / §7).

``services/ledger_sync.reconcile_*`` stays ledger-internal. This module is
the only place that asks the exchange what it actually holds.

Called once per tenant before its first trading cycle (hook:
``architecture_runtime.ensure_started``). Shadow never hits the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.costs import CostModel
from core.models import OrderStatus, TradeOrder, TradeResult
from core.operator_notify import notify_operator
from core.stablecoins import STABLECOIN_BASES
from core.tenant_context import tenant_context
from execution.gate_adapter import (
    _GATE_CLIENT_ID_DIGEST_CHARS,
    _GATE_CLIENT_ID_HEAD_CHARS,
    _GATE_TEXT_ALLOWED,
    _canceled_or_rejected_status,
    _ccxt_status_token,
    _clamp_gate_client_order_id,
    _legacy_gate_client_order_id,
    _finish_as_from_raw,
    _partial_remainder_cancel,
    _zero_fill_cancel_status,
)
from execution.order_fetch import call_fetch_order_retrying_not_found
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


def _clamped_client_lookup(token: str) -> str:
    """Current Gate payload for a stored key, or '' if we would mint a new id."""
    raw = str(token or "").strip()
    if not raw or not any(ch in _GATE_TEXT_ALLOWED for ch in raw):
        return ""
    return _clamp_gate_client_order_id(raw)


def _legacy_client_lookup(token: str) -> str:
    """Pre-#753 26-character cut for the same stored key, or ''."""
    raw = str(token or "").strip()
    if not raw or not any(ch in _GATE_TEXT_ALLOWED for ch in raw):
        return ""
    return _legacy_gate_client_order_id(raw)


def _is_head_digest_client_id(token: str) -> bool:
    """True for the post-#753 ``<11-char head>.<14-hex sha256>`` payload.

    The head may itself contain ``.`` (the Gate charset allows it), so the
    split is the last dot. The digest is exactly 14 hex characters.
    """
    text = str(token or "").strip()
    head, dot, digest = text.rpartition(".")
    if not dot:
        return False
    if len(head) != _GATE_CLIENT_ID_HEAD_CHARS or len(digest) != _GATE_CLIENT_ID_DIGEST_CHARS:
        return False
    return set(digest) <= set("0123456789abcdef")


def _row_may_use_legacy_client_id(order: dict) -> bool:
    """Legacy cut is only for rows that are not already stored as head.digest.

    A post-#753 row persists the current clamp in ``client_order_id``. Looking
    that row up by the shared 26-character cut can finalize a different older
    order (#753).
    """
    stored = str(order.get("client_order_id") or "").strip()
    return not _is_head_digest_client_id(stored)


def _legacy_only_ids(order: dict) -> set[str]:
    """Legacy cuts that differ from the current clamp. Empty for post-#753 rows."""
    if not _row_may_use_legacy_client_id(order):
        return set()
    out: set[str] = set()
    for raw in (order.get("client_order_id"), order.get("idempotency_key")):
        legacy = _legacy_client_lookup(raw)
        current = _clamped_client_lookup(raw)
        if legacy and legacy != current:
            out.add(legacy)
    return out


_LEGACY_QTY_ABS_TOL = 1e-8
_LEGACY_QTY_REL_TOL = 1e-6


def _norm_recovery_side(value: object) -> str:
    side = str(value or "").strip().lower()
    if side in ("buy", "cover"):
        return "buy"
    if side in ("sell", "short"):
        return "sell"
    return ""


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms(value: object) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1e12:
            return num
        if num > 1e9:
            return num * 1000.0
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _epoch_ms(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def _record_created_ms(order: dict) -> float | None:
    stamps = order.get("timestamps") if isinstance(order.get("timestamps"), dict) else {}
    for value in (stamps.get("created"), order.get("created_at"), order.get("timestamp")):
        ms = _epoch_ms(value)
        if ms is not None:
            return ms
    return None


def _quote_usdt(order: dict) -> float | None:
    req = order.get("request") if isinstance(order.get("request"), dict) else {}
    execution = order.get("execution") if isinstance(order.get("execution"), dict) else {}
    for value in (
        req.get("usdt"),
        order.get("usdt_amount"),
        execution.get("usdt"),
        order.get("usdt"),
    ):
        num = _as_float(value)
        if num is not None and num > 0:
            return num
    return None


def _record_base_qty(order: dict) -> float | None:
    req = order.get("request") if isinstance(order.get("request"), dict) else {}
    for value in (order.get("qty"), req.get("amount")):
        num = _as_float(value)
        if num is not None:
            return num
    return None


def _qty_close(left: float, right: float) -> bool:
    tol = max(_LEGACY_QTY_ABS_TOL, abs(right) * _LEGACY_QTY_REL_TOL)
    return abs(left - right) <= tol


def _row_is_swap(order: dict) -> bool:
    """Swap vs spot comes from the row, not from whether a perp market exists.

    ``order.symbol`` stays the spot pair for both. A short or cover is a swap
    row. A symbol that already names the swap market is one too.
    """
    side = str(order.get("side") or "").strip().lower()
    if side in ("short", "cover"):
        return True
    symbol = str(order.get("symbol") or "")
    return ":" in symbol


def _swap_contract_size(exchange, order: dict) -> float | None:
    """ccxt ``contractSize`` for a swap row. Spot rows are never looked up."""
    if not _row_is_swap(order):
        return None
    market_fn = getattr(exchange, "market", None) if exchange is not None else None
    if not callable(market_fn):
        return None
    swap_symbol = _as_swap_symbol(str(order.get("symbol") or ""))
    try:
        market = market_fn(swap_symbol)
    except Exception:
        return None
    if not isinstance(market, dict):
        return None
    size = _as_float(market.get("contractSize"))
    if size is None or size <= 0:
        return None
    return size


def _legacy_size_matches(order: dict, raw: dict, exchange) -> bool:
    """Compare the legacy hit in the unit the venue actually used.

    Spot market buys store quote USDT in ``amount`` and ``cost``
    (``create_market_buy_order_with_cost``). ``filled`` is base qty.
    Only a swap row (side short/cover, or a swap symbol) uses
    ``amount * contractSize``. A spot row never applies ``contractSize``,
    even when that coin also has a perp.
    """
    rec_qty = _record_base_qty(order)
    filled = _as_float(raw.get("filled"))
    amount = _as_float(raw.get("amount"))
    cost = _as_float(raw.get("cost"))
    if _row_is_swap(order):
        contract_size = _swap_contract_size(exchange, order)
        if contract_size is None or amount is None or rec_qty is None:
            return False
        return _qty_close(amount * contract_size, rec_qty)
    if filled is not None and rec_qty is not None and _qty_close(filled, rec_qty):
        return True
    if _norm_recovery_side(order.get("side")) == "buy":
        quote = _quote_usdt(order)
        if quote is not None:
            if cost is not None and _qty_close(cost, quote):
                return True
            if amount is not None and _qty_close(amount, quote):
                return True
    if amount is not None and rec_qty is not None and _qty_close(amount, rec_qty):
        return True
    return False


def _legacy_match_belongs(order: dict, raw: dict, exchange=None) -> bool:
    """A legacy-cut hit must be this row, not an older order that shares the cut."""
    if _norm_recovery_side(raw.get("side")) != _norm_recovery_side(order.get("side")):
        return False
    if not _legacy_size_matches(order, raw, exchange):
        return False
    rec_ms = _record_created_ms(order)
    if rec_ms is None:
        return True
    raw_ms = _epoch_ms(raw.get("timestamp"))
    if raw_ms is None:
        raw_ms = _epoch_ms(raw.get("datetime"))
    if raw_ms is None:
        return False
    return raw_ms + 1.0 >= rec_ms


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
    # Historical rows stored the unclamped key. Try the current clamp, and the
    # pre-#753 26-character cut only when this row is not already head.digest.
    allow_legacy = _row_may_use_legacy_client_id(order)
    for raw in (order.get("client_order_id"), order.get("idempotency_key")):
        candidates = [_clamped_client_lookup(raw)]
        if allow_legacy:
            candidates.append(_legacy_client_lookup(raw))
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _fetch_order_raw(exchange, order: dict):
    symbol = str(order.get("symbol") or "")
    last_err: Exception | None = None
    legacy_only = _legacy_only_ids(order)
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
                raw = call_fetch_order_retrying_not_found(fn, *args, **kwargs)
            except TypeError:
                try:
                    raw = call_fetch_order_retrying_not_found(fn, ident, symbol)
                except Exception as e:
                    if _is_unreachable(e):
                        raise
                    log(f"fetch_order({ident!r}) failed: {e}", "WARNING")
                    last_err = e
                    continue
            except Exception as e:
                if _is_unreachable(e):
                    raise
                log(f"fetch_order({ident!r}) failed: {e}", "WARNING")
                last_err = e
                continue
            if isinstance(raw, dict) and raw:
                if ident in legacy_only and not _legacy_match_belongs(order, raw, exchange):
                    log(
                        f"legacy clientOrderId {ident!r} for {symbol} "
                        "does not match this row; not adopted",
                        "WARNING",
                    )
                    continue
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
    # Terminal resolve clears the in-flight flag, except while the fee is
    # still unknown or only an estimate (#679 R4).
    st = getattr(result, "order_status", None)
    if st in (
        OrderStatus.EXECUTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    ):
        data = svc._load()
        rec = svc._find(data, order_id=order_id)
        if rec is None:
            return
        fee_unresolved = bool(getattr(result, "fee_unknown", False)) or bool(
            getattr(result, "fee_estimated", False)
        )
        if not fee_unresolved:
            exe = rec.get("execution") if isinstance(rec.get("execution"), dict) else {}
            fee_unresolved = bool(rec.get("fee_unknown") or rec.get("fee_estimated")) or bool(
                exe.get("fee_unknown") or exe.get("fee_estimated")
            )
        from storage.order_ledger_v2 import _order_pnl_or_missing, order_is_pnl_tracked

        _pnl_value, pnl_missing = _order_pnl_or_missing(rec)
        # A flagged row with no pnl stays selectable. Clearing the flag here
        # when only the fee is settled leaves a permanent entry block.
        tracked_missing = order_is_pnl_tracked(rec) and pnl_missing
        if fee_unresolved or tracked_missing:
            rec["needs_reconcile"] = True
            if getattr(result, "fee_unknown", False) or rec.get("fee_unknown"):
                rec["fee_unknown"] = True
            if getattr(result, "fee_estimated", False) or rec.get("fee_estimated"):
                rec["fee_estimated"] = True
            svc._save(data)
            return
        if rec.get("needs_reconcile"):
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
    raw = raw if isinstance(raw, dict) else {}
    token = _ccxt_status_token(raw)
    finish_as = _finish_as_from_raw(raw)
    terminal = _canceled_or_rejected_status(raw)
    if terminal is OrderStatus.REJECTED:
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
        if terminal is OrderStatus.CANCELED:
            return TradeResult(
                False,
                order.type,
                order.symbol,
                message=f"exchange {token or finish_as}",
                order_id=order.order_id,
                exchange_order_id=str(raw.get("id") or ""),
                order_status=OrderStatus.CANCELED,
                needs_reconcile=False,
                order_exist_in_exchange=True,
            )
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
    # Same zero-fill rule as GateExecutionAdapter._finalize_exchange_order.
    # Missing finish_as stays the status-only path and is not a cancel.
    if (terminal is OrderStatus.CANCELED and filled_f <= 0) or (
        _zero_fill_cancel_status(raw, filled_f) is OrderStatus.CANCELED
    ):
        msg = f"exchange {token or 'unset'}"
        if finish_as:
            msg += f" finish_as={finish_as}"
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message=msg,
            order_id=order.order_id,
            exchange_order_id=str(raw.get("id") or ""),
            order_status=OrderStatus.CANCELED,
            needs_reconcile=False,
            order_exist_in_exchange=True,
        )
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
    if filled_f <= 0:
        # Missing finish_as is not a cancel. Match the Gate adapter: do not
        # book a zero qty, leave needs_reconcile (#551).
        return TradeResult(
            False,
            order.type,
            order.symbol,
            message=(
                f"zero fill not booked (filled={filled_f}, "
                f"status={token or 'unset'})"
            ),
            order_id=order.order_id,
            exchange_order_id=str(raw.get("id") or ""),
            order_status=OrderStatus.ACTIVE,
            pending=True,
            needs_reconcile=True,
        )
    remainder_canceled = (
        terminal is OrderStatus.CANCELED and filled_f > 0
    ) or _partial_remainder_cancel(raw, filled_f, requested, token)
    status = OrderStatus.EXECUTED
    if finish_as == "open" and filled_f > 0:
        status = OrderStatus.PARTIALLY_FILLED
    elif (
        requested > 0
        and 0 < filled_f < requested
        and token not in ("closed", "filled")
    ):
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
    if remainder_canceled:
        status = OrderStatus.CANCELED
    # This path does not compute short/cover pnl. Leave it None so the order
    # row is missing, not a stored 0.0. Sells keep the dataclass default.
    missing_pnl = None if order.type in ("SHORT", "COVER") else 0.0
    return TradeResult(
        True,
        order.type,
        order.symbol,
        amount=fill.qty_net,
        price=fill.fill_price,
        usdt_amount=fill.quote_net,
        pnl=missing_pnl,
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
