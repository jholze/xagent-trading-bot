"""Execute trail exits from WS path via TradingService (same risk/order path)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterator

from logger import log

_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_last_exit_at: dict[str, float] = {}  # symbol -> mono time


@contextmanager
def restoring_tenant_cycle_context(tenant_id: str) -> Iterator[None]:
    """``tenant_cycle_context`` plus restore of the process-global positions store.

    ``tenant_cycle_context`` calls ``activate_tenant_positions`` and does not
    put ``_active_key`` back on exit (it is a module global, not a contextvar).
    Bot-process callers (Flask fire, hub fire, book daemon) must restore so
    the default cycle does not read/write the wrong store.
    """
    from core.tenant_context import resolve_tenant_id
    from core.tenant_routing import tenant_cycle_context
    from strategies.positions import activate_tenant_positions, get_active_scope

    prev_tid = resolve_tenant_id()
    prev_scope = get_active_scope()
    try:
        with tenant_cycle_context(tenant_id):
            yield
    finally:
        try:
            activate_tenant_positions(scope=prev_scope, tenant_id=prev_tid)
        except Exception as exc:
            log(
                f"exit_ws restore positions tenant={prev_tid} scope={prev_scope}: {exc}",
                "ERROR",
            )


def recently_exited(symbol: str, within_sec: float = 120.0) -> bool:
    t = _last_exit_at.get(symbol, 0.0)
    return t > 0 and (time.monotonic() - t) < within_sec


def _remote_execute_trail_exit(
    *,
    url: str,
    symbol: str,
    timeframe: str,
    price: float,
    action: str,
    exit_source: str,
    rationale: str,
    token: str,
    tenant_id: str = "",
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """POST fire request to bot ``/internal/exit-ws/fire`` (sidecar path)."""
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": price,
        "action": action,
        "exit_source": exit_source,
        "rationale": rationale,
        "idempotency_key": f"{symbol}|{timeframe}|{exit_source}|{price:.8g}",
        "tenant_id": tenant_id,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "xagent-exit-radar-sidecar/1",
    }
    if token:
        headers["X-Exit-Ws-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                return {
                    "ok": False,
                    "executed": False,
                    "message": "bad_remote_response",
                    "remote": True,
                }
            data.setdefault("remote", True)
            return data
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return {
            "ok": False,
            "executed": False,
            "message": f"remote_http_{e.code}:{detail or e.reason}",
            "remote": True,
        }
    except Exception as e:
        return {
            "ok": False,
            "executed": False,
            "message": f"remote_error:{e}"[:200],
            "remote": True,
        }


def _execute_short_cover(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    amount: float,
    exit_source: str,
    rationale: str,
    trading: Any | None,
) -> dict[str, Any]:
    from core.models import TradeOrder
    from services.trading_service import TradingService

    order = TradeOrder(
        type="COVER",
        symbol=symbol,
        price=price,
        amount=amount,
        signal="COVER",
        source="exit_ws",
        exit_source=str(exit_source or "short_cover"),
        exit_rationale=str(rationale or "")[:240],
    )
    if trading is None:
        trading = TradingService()
    result = trading.execute_order(order, timeframe, source="exit_ws", confidence=80.0)
    executed = bool(getattr(result, "executed", False))
    msg = str(getattr(result, "message", "") or "")
    if executed:
        _last_exit_at[symbol] = time.monotonic()
        log(
            f"exit_ws COVER {symbol} {timeframe} src={exit_source} "
            f"px={price:.6g} amt={amount:.6g} :: {msg[:80]}",
            "INFO",
        )
    return {
        "ok": True,
        "executed": executed,
        "message": msg,
        "symbol": symbol,
        "timeframe": timeframe,
        "exit_source": exit_source,
        "price": price,
        "amount": amount,
        "cover": True,
    }


def try_execute_trail_exit(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    action: str,
    exit_source: str,
    rationale: str = "",
    trading: Any | None = None,
    force_local: bool = False,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """
    Full-position SELL through RiskManager + order path.

    When ``EXIT_EXECUTE_URL`` is set (sidecar), posts to the bot internal
    fire endpoint instead of executing locally — bot remains sole write path.
    Returns {ok, executed, message, ...}.
    """
    from core.tenant_context import resolve_tenant_id as _resolve_tid
    from services.exit_realtime.config import exit_execute_url, exit_ws_internal_token

    requested_tid = str(tenant_id or "").strip()
    if requested_tid and _resolve_tid() != requested_tid:
        with restoring_tenant_cycle_context(requested_tid):
            return try_execute_trail_exit(
                symbol=symbol,
                timeframe=timeframe,
                price=price,
                action=action,
                exit_source=exit_source,
                rationale=rationale,
                trading=trading,
                force_local=force_local,
                tenant_id=None,
            )

    sym = str(symbol or "")
    tf = str(timeframe or "1h")
    px = float(price or 0)
    if not sym or px <= 0:
        return {"ok": False, "executed": False, "message": "bad_args"}

    active_tid = requested_tid or _resolve_tid()

    # recovery_hold / sniper_focus: block trail-class WS fires (hard SL not via this path)
    # Short lots skip this — cover (liq/stop/time) must still fire.
    try:
        from strategies.positions import get_position
        from strategies.recovery_hold import (
            auto_sells_blocked_reason,
            maybe_promote_recovery_hold,
        )
        from strategies.short_math import is_short as _is_short_lot

        pos = get_position(sym, tf) or {}
        if pos and not _is_short_lot(pos):
            if maybe_promote_recovery_hold(pos, px):
                try:
                    from strategies.positions import flush_positions

                    flush_positions()
                except Exception as exc:
                    log(
                        f"exit_ws recovery_hold flush failed {sym}: {exc}",
                        "WARNING",
                    )
            block = auto_sells_blocked_reason(pos, str(exit_source or "trailing_stop"))
            if block:
                return {
                    "ok": True,
                    "executed": False,
                    "message": block,
                    "recovery_hold": True,
                }
    except Exception as e:
        log(f"exit_ws recovery_hold check skip: {e}", "DEBUG")

    remote_url = "" if force_local else exit_execute_url()
    if remote_url:
        return _remote_execute_trail_exit(
            url=remote_url,
            symbol=sym,
            timeframe=tf,
            price=px,
            action=action,
            exit_source=exit_source,
            rationale=rationale,
            token=exit_ws_internal_token(),
            tenant_id=active_tid,
        )

    from core.actions import SELL_FULL
    from core.models import TradeOrder
    from strategies.positions import (
        get_position,
        is_open_position,
        mark_trailing_take_profit_step,
    )

    with _inflight_lock:
        if sym in _inflight:
            return {"ok": False, "executed": False, "message": "inflight"}
        if recently_exited(sym, within_sec=60.0):
            return {"ok": False, "executed": False, "message": "recent_exit"}
        _inflight.add(sym)

    try:
        pos = get_position(sym, tf)
        if not is_open_position(pos):
            return {"ok": False, "executed": False, "message": "no_open_position"}
        amount = float(pos.get("amount") or 0)
        if amount <= 0:
            return {"ok": False, "executed": False, "message": "amount_zero"}

        short_lot = False
        try:
            from strategies.short_math import is_short as _is_short

            short_lot = bool(_is_short(pos))
        except Exception as exc:
            log(f"exit_ws side check failed {sym}: {exc}", "ERROR")
            return {
                "ok": False,
                "executed": False,
                "message": f"side_check_error:{exc}"[:200],
            }
        if short_lot or str(action or "").upper() == "COVER":
            return _execute_short_cover(
                symbol=sym,
                timeframe=tf,
                price=px,
                amount=amount,
                exit_source=exit_source,
                rationale=rationale,
                trading=trading,
            )

        try:
            from strategies.position_lock import (
                attach_lock_from_ledger,
                auto_sell_blocked,
                log_lock_block,
            )

            pos = attach_lock_from_ledger(pos, sym, tf) or pos
            locked, lock_msg = auto_sell_blocked(pos, "exit_ws")
            if locked:
                log_lock_block(sym, lock_msg, source="exit_ws")
                return {
                    "ok": False,
                    "executed": False,
                    "message": lock_msg,
                    "code": "position_locked",
                }
        except Exception as exc:
            # Fail-closed: do not trail-sell if lock check is broken
            log(f"exit_ws position_lock check error {sym}: {exc}", "ERROR")
            return {
                "ok": False,
                "executed": False,
                "message": f"position_lock_check_error: {exc}"[:200],
                "code": "position_lock_check_error",
            }

        signal = str(action or SELL_FULL).strip() or SELL_FULL
        # Prefer full close for trail sources
        if "PARTIAL" not in signal.upper() and signal.upper() in (
            "SELL",
            "SELL_FULL",
            SELL_FULL,
        ):
            signal = SELL_FULL

        order = TradeOrder(
            type="SELL",
            symbol=sym,
            price=px,
            amount=amount,
            signal=signal,
            source="exit_ws",
            exit_source=str(exit_source or ""),
            exit_rationale=str(rationale or "")[:240],
        )

        if trading is None:
            from services.trading_service import TradingService

            trading = TradingService()

        result = trading.execute_order(
            order,
            tf,
            source="exit_ws",
            confidence=80.0,
        )
        executed = bool(getattr(result, "executed", False))
        msg = str(getattr(result, "message", "") or "")
        out = {
            "ok": True,
            "executed": executed,
            "message": msg,
            "symbol": sym,
            "timeframe": tf,
            "exit_source": exit_source,
            "price": px,
            "amount": amount,
        }
        if executed:
            _last_exit_at[sym] = time.monotonic()
            try:
                if exit_source == "trailing_take_profit":
                    mark_trailing_take_profit_step(sym, tf, px)
                    # increment steps so pure eval won't re-fire immediately
                    pos2 = get_position(sym, tf)
                    steps = int(pos2.get("trail_tp_steps") or 0) + 1
                    pos2["trail_tp_steps"] = steps
                    from strategies.positions import flush_positions

                    flush_positions()
            except Exception as exc:
                log(f"exit_ws post-mark failed {sym}: {exc}", "WARNING")
            log(
                f"exit_ws LIVE SELL {sym} {tf} src={exit_source} "
                f"px={px:.6g} amt={amount:.6g} :: {msg[:80]}",
                "INFO",
            )
        else:
            log(
                f"exit_ws SELL blocked/failed {sym} src={exit_source}: {msg[:120]}",
                "INFO",
            )
        return out
    except Exception as exc:
        log(f"exit_ws execute error {symbol}: {exc}", "ERROR")
        return {"ok": False, "executed": False, "message": str(exc)[:200]}
    finally:
        with _inflight_lock:
            _inflight.discard(sym)


def _gross_unrealized_pct(pos: dict[str, Any], price: float) -> float:
    """Gross unrealized % the way exit_ws computes long gain: (px/entry - 1)*100."""
    entry = float(pos.get("average_entry") or 0)
    px = float(price or 0)
    if entry <= 0 or px <= 0:
        return 0.0
    try:
        from strategies.short_math import is_short as _is_short

        if _is_short(pos):
            return (entry - px) / entry * 100.0
    except Exception:
        pass
    return (px / entry - 1.0) * 100.0


def _lot_in_profit(pos: dict[str, Any], price: float, raw_config: dict | None) -> bool:
    """Longs: net of one sell (buy fee already in average_entry). Shorts: gross minus round trip."""
    from core.costs import CostModel

    try:
        from strategies.short_math import is_short as _is_short

        short = _is_short(pos)
    except Exception:
        short = False
    if short:
        gain = _gross_unrealized_pct(pos, price)
        rt = float(CostModel.from_config(raw_config).round_trip_pct())
        return (gain - rt) > 0.0
    amount = float(pos.get("amount") or 0)
    entry = float(pos.get("average_entry") or 0)
    px = float(price or 0)
    if amount <= 0 or entry <= 0 or px <= 0:
        return False
    model = CostModel.from_config(raw_config, symbol=str(pos.get("symbol") or "") or None)
    sell = model.simulate_sell(px, amount)
    pnl = CostModel.realized_pnl(qty_sold=amount, avg_entry_net=entry, sell=sell)
    return pnl > 0.0


def execute_cascade_exit(
    *,
    side: str,
    lots: list[dict[str, Any]] | None = None,
    prices: dict[str, float] | None = None,
    trading: Any | None = None,
    fire_enabled: bool | None = None,
    raw_config: dict | None = None,
    now_mono: float | None = None,
    state: Any | None = None,
) -> dict[str, Any]:
    """Binary full-exit for one cascade side. Not routed through try_execute_trail_exit."""
    from core.actions import COVER_FULL, SELL_FULL
    from core.models import TradeOrder
    from services.exit_realtime.config import cascade_config
    from strategies.sell_sources import LIQ_CASCADE_SOURCE

    side_key = "short" if str(side or "").strip().lower() in ("short", "pump") else "long"
    short_side = side_key == "short"
    action = COVER_FULL if short_side else SELL_FULL
    rationale = (
        "liq cascade pump full cover"
        if short_side
        else "liq cascade dump full exit"
    )
    cc = cascade_config(raw_config)
    if fire_enabled is None:
        fire_enabled = bool(cc.get("fire_enabled"))
    mono = float(now_mono if now_mono is not None else time.monotonic())
    px_map = dict(prices or {})

    if not fire_enabled:
        log(
            f"liq_cascade fire side={side_key} fire_enabled=false — detector only, no flatten",
            "INFO",
        )
        return {
            "ok": True,
            "executed": False,
            "message": "fire_disabled",
            "side": side_key,
            "action": action,
            "filled": 0,
            "results": [],
        }

    from strategies.positions import get_position, is_open_position
    from strategies.short_math import is_short as _is_short

    if lots is None:
        from strategies.positions import list_active_positions

        lots = list(list_active_positions() or [])

    snapshot = []
    for lot in lots or []:
        if not isinstance(lot, dict):
            continue
        try:
            if bool(_is_short(lot)) != short_side:
                continue
        except Exception:
            continue
        snapshot.append(lot)

    if trading is None:
        from services.trading_service import TradingService

        trading = TradingService()

    results: list[dict[str, Any]] = []
    filled = 0

    for lot in snapshot:
        sym = str(lot.get("symbol") or "")
        tf = str(lot.get("timeframe") or "1h")
        if not sym:
            continue
        try:
            px = float(px_map.get(sym) or lot.get("current_price") or lot.get("last_price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            results.append(
                {
                    "symbol": sym,
                    "timeframe": tf,
                    "executed": False,
                    "message": "no_price",
                }
            )
            continue

        with _inflight_lock:
            if sym in _inflight:
                results.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "executed": False,
                        "message": "inflight",
                    }
                )
                continue
            if recently_exited(sym, within_sec=60.0):
                results.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "executed": False,
                        "message": "recent_exit",
                    }
                )
                continue
            _inflight.add(sym)

        try:
            pos = get_position(sym, tf) or dict(lot)
            if not is_open_position(pos):
                results.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "executed": False,
                        "message": "no_open_position",
                    }
                )
                continue
            amount = float(pos.get("amount") or lot.get("amount") or 0)
            if amount <= 0:
                results.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "executed": False,
                        "message": "amount_zero",
                    }
                )
                continue

            try:
                from strategies.position_lock import (
                    attach_lock_from_ledger,
                    auto_sell_blocked,
                    log_lock_block,
                )

                pos = attach_lock_from_ledger(pos, sym, tf) or pos
                locked, lock_msg = auto_sell_blocked(pos, LIQ_CASCADE_SOURCE)
                if locked:
                    log_lock_block(sym, lock_msg, source=LIQ_CASCADE_SOURCE)
                    log(
                        f"liq_cascade position_locked {sym} {tf} side={side_key} :: {lock_msg}",
                        "INFO",
                    )
                    results.append(
                        {
                            "symbol": sym,
                            "timeframe": tf,
                            "executed": False,
                            "message": lock_msg,
                            "code": "position_locked",
                        }
                    )
                    continue
            except Exception as exc:
                log(f"liq_cascade position_lock check error {sym}: {exc}", "ERROR")
                results.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "executed": False,
                        "message": f"position_lock_check_error: {exc}"[:200],
                        "code": "position_lock_check_error",
                    }
                )
                continue

            if not _lot_in_profit(pos, px, raw_config):
                results.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "executed": False,
                        "message": "not_in_profit",
                    }
                )
                continue

            order = TradeOrder(
                type="COVER" if short_side else "SELL",
                symbol=sym,
                price=px,
                amount=amount,
                signal=action,
                source=LIQ_CASCADE_SOURCE,
                exit_source=LIQ_CASCADE_SOURCE,
                exit_rationale=rationale,
            )
            result = trading.execute_order(
                order, tf, source=LIQ_CASCADE_SOURCE, confidence=80.0
            )
            executed = bool(getattr(result, "executed", False))
            msg = str(getattr(result, "message", "") or "")
            row = {
                "symbol": sym,
                "timeframe": tf,
                "executed": executed,
                "message": msg,
                "action": action,
                "price": px,
                "amount": amount,
            }
            if executed:
                filled += 1
                _last_exit_at[sym] = time.monotonic()
                log(
                    f"liq_cascade {action} {sym} {tf} px={px:.6g} amt={amount:.6g} :: {msg[:80]}",
                    "INFO",
                )
            results.append(row)
        except Exception as exc:
            log(f"liq_cascade execute error {sym}: {exc}", "ERROR")
            results.append(
                {
                    "symbol": sym,
                    "timeframe": tf,
                    "executed": False,
                    "message": str(exc)[:200],
                }
            )
        finally:
            with _inflight_lock:
                _inflight.discard(sym)

    if filled > 0 and state is not None:
        try:
            state.note_fill(side_key, mono)
        except Exception as exc:
            log(f"liq_cascade note_fill: {exc}", "DEBUG")

    return {
        "ok": True,
        "executed": filled > 0,
        "message": "ok" if filled else "no_fill",
        "side": side_key,
        "action": action,
        "filled": filled,
        "results": results,
    }


def _cascade_batch_text() -> str:
    """Gate ``text`` for one batch sell. Prefixed, charset-legal, ≤28 payload bytes."""
    import uuid

    from execution.gate_adapter import _GATE_TEXT_PREFIX, _clamp_gate_client_order_id

    return f"{_GATE_TEXT_PREFIX}{_clamp_gate_client_order_id(uuid.uuid4().hex)}"


def _chunk_cascade_batch(orders: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Pack selected sells into Gate chunks: 4 pairs, 10 orders per pair."""
    from execution.gate_adapter import (
        GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR,
        GATE_SPOT_BATCH_MAX_PAIRS,
    )

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    pairs: list[str] = []
    counts: dict[str, int] = {}

    def flush() -> None:
        nonlocal current, pairs, counts
        if current:
            chunks.append(current)
        current = []
        pairs = []
        counts = {}

    for order in orders:
        pair = str(order.get("symbol") or "")
        if counts.get(pair, 0) >= GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR or (
            pair not in counts and len(pairs) >= GATE_SPOT_BATCH_MAX_PAIRS
        ):
            flush()
        if pair not in counts:
            pairs.append(pair)
        counts[pair] = counts.get(pair, 0) + 1
        current.append(order)
    flush()
    return chunks


def _english_failed(symbol: str, code: str, detail: str = "") -> str:
    extra = str(detail or "").strip()
    if extra and extra != code:
        return f"failed {symbol}: {code} {extra}"[:240]
    readable = code.replace("_", " ")
    return f"failed {symbol}: {readable}"[:240]


def _evidence(
    *,
    symbol: str,
    timeframe: str,
    executed: bool,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "timeframe": timeframe,
        "executed": executed,
        "status": "closed" if executed else "failed",
        "code": code,
        "message": message,
    }
    row.update(extra)
    return row


def _cascade_batch_seller(trading: Any):
    adapter = getattr(trading, "adapter", None)
    if adapter is not None:
        fn = getattr(adapter, "batch_market_sell", None)
        if callable(fn):
            return fn
    fn = getattr(trading, "batch_market_sell", None)
    if callable(fn):
        return fn
    return None


def _portfolio_for_batch(trading: Any):
    portfolio = getattr(trading, "portfolio", None)
    if portfolio is not None and callable(getattr(portfolio, "execute_sell", None)):
        return portfolio
    adapter = getattr(trading, "adapter", None)
    portfolio = getattr(adapter, "portfolio", None) if adapter is not None else None
    if portfolio is not None and callable(getattr(portfolio, "execute_sell", None)):
        return portfolio
    return None


def _ack_succeeded(row: dict[str, Any]):
    from execution.gate_adapter import _batch_succeeded_flag

    if not isinstance(row, dict):
        return None
    return _batch_succeeded_flag(row.get("succeeded"))


def _match_batch_acks(
    chunk: list[dict[str, Any]], acks: list
) -> tuple[list[tuple[dict[str, Any], dict]], list[dict[str, Any]]]:
    by_text: dict[str, dict] = {}
    for row in acks:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip().lower()
        if text:
            by_text.setdefault(text, row)
    matched: list[tuple[dict[str, Any], dict]] = []
    missing: list[dict[str, Any]] = []
    for order in chunk:
        text = str(order.get("text") or "").strip().lower()
        row = by_text.get(text)
        if row is None and text.startswith("t-"):
            row = by_text.get(text[2:])
        if row is None and text:
            row = by_text.get(text if text.startswith("t-") else f"t-{text}")
        if row is None:
            missing.append(order)
            continue
        matched.append((order, row))
    return matched, missing


def _ack_finish_as(ack: dict[str, Any]) -> str:
    direct = str(ack.get("finish_as") or "").strip().lower()
    if direct:
        return direct
    info = ack.get("info")
    if isinstance(info, dict):
        return str(info.get("finish_as") or "").strip().lower()
    return ""


def _ack_optional_float(ack: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in ack:
            continue
        raw = ack.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _batch_exchange_raw(ack: dict[str, Any]) -> dict[str, Any]:
    """Shape a flat batch ACK so the sequential finish_as helpers can read it.

    Those helpers look at ccxt ``info.finish_as`` only. Batch rows are flat.
    """
    return {
        "status": str(ack.get("status") or ""),
        "info": {"finish_as": _ack_finish_as(ack)},
    }


def _classify_succeeded_batch_ack(
    ack: dict[str, Any], requested: float
) -> tuple[str, float, float]:
    """Classify one succeeded batch row the way the sequential adapter does.

    Returns ``(kind, filled_amount, fill_price)``. ``kind`` is ``fill``,
    ``zero_fill_cancel``, ``missing_fill``, or ``missing_fill_price``.
    ``succeeded`` true is never a snapshot fill. A positive fill is booked
    at ``filled_amount`` and the fill price only.
    """
    from core.models import OrderStatus
    from execution.gate_adapter import _zero_fill_cancel_status

    filled = _ack_optional_float(ack, "filled_amount", "filled")
    raw = _batch_exchange_raw(ack)
    if filled is None:
        return "missing_fill", 0.0, 0.0
    if _zero_fill_cancel_status(raw, filled) is OrderStatus.CANCELED:
        return "zero_fill_cancel", 0.0, 0.0
    if filled <= 0:
        # Zero without a cancel finish_as is not a fill and not a retry.
        return "missing_fill", 0.0, 0.0
    price = _ack_optional_float(ack, "fill_price", "avg_deal_price", "average")
    if price is None or price <= 0:
        return "missing_fill_price", filled, 0.0
    return "fill", filled, price


def _batch_order_status(ack: dict[str, Any], filled: float, requested: float):
    from core.models import OrderStatus
    from execution.gate_adapter import _partial_remainder_cancel

    raw = _batch_exchange_raw(ack)
    token = str(ack.get("status") or "").strip().lower()
    if _partial_remainder_cancel(raw, filled, requested, token):
        # Same stamp as the sequential remainder-canceled fill: booked, then canceled.
        return OrderStatus.CANCELED
    if token in ("closed", "filled"):
        return OrderStatus.EXECUTED
    if requested > 0 and 0 < filled < requested:
        return OrderStatus.PARTIALLY_FILLED
    return OrderStatus.EXECUTED


def _trading_mode_name(trading: Any) -> str | None:
    cfg = getattr(trading, "config", None)
    if cfg is None:
        return None
    mode = getattr(cfg, "trading_mode", None)
    if mode:
        return str(mode)
    raw = getattr(cfg, "raw", None)
    if isinstance(raw, dict) and raw.get("trading_mode"):
        return str(raw.get("trading_mode"))
    return None


def _record_batch_fill(
    *,
    trading: Any,
    symbol: str,
    timeframe: str,
    action: str,
    source: str,
    text: str,
    requested: float,
    filled: float,
    fill_price: float,
    ack: dict[str, Any],
    sell_result: Any,
) -> None:
    """Order row and live trade for one acked fill. No second exchange order."""
    from datetime import datetime

    from core.costs import COST_MODEL_VERSION
    from core.models import TradeOrder, TradeResult, trade_ctx_fields
    from data_manager import record_live_trade
    from services.order_service import OrderService

    exchange_id = str(ack.get("id") or "")
    order_status = _batch_order_status(ack, filled, requested)
    trade = TradeOrder(
        type="SELL",
        symbol=symbol,
        price=fill_price,
        amount=filled,
        signal=action,
        source=source,
        exit_source=source,
        exit_rationale="liq cascade dump full exit",
        client_order_id=text,
        exchange_order_id=exchange_id,
        order_exist_in_exchange=bool(exchange_id),
        filled_qty=filled,
        status=order_status,
    )
    ledger = OrderService()
    created = ledger.create_from_request(
        trade,
        timeframe=timeframe,
        status="executing",
        request_extra={
            "text": text,
            "requested_amount": float(requested),
            "batch": True,
            "finish_as": _ack_finish_as(ack),
        },
        idempotency_key=text,
    )
    ledger_id = str(created.get("id") or "")
    usdt = float(getattr(sell_result, "usdt_amount", 0) or 0) or (fill_price * filled)
    pnl = float(getattr(sell_result, "pnl", 0) or 0)
    fee = float(getattr(sell_result, "fee", 0) or 0)
    booked = float(getattr(sell_result, "amount", 0) or 0) or filled
    result = TradeResult(
        True,
        "SELL",
        symbol,
        amount=booked,
        price=fill_price,
        usdt_amount=usdt,
        pnl=pnl,
        message="batch market sell acknowledged",
        order_id=ledger_id,
        exchange_order_id=exchange_id,
        fee=fee,
        order_status=order_status,
        order_exist_in_exchange=bool(exchange_id),
        filled_qty=filled,
    )
    ledger.link_execution_result(ledger_id, result, trade)
    adapter = getattr(trading, "adapter", None)
    mode = getattr(adapter, "mode", None) or _trading_mode_name(trading) or "batch"
    rec = {
        "type": "SELL",
        "symbol": symbol,
        "price": fill_price,
        "amount": booked,
        "usdt_amount": usdt,
        "usdt_received": usdt,
        "pnl": pnl,
        "exchange_order_id": exchange_id,
        "order_id": ledger_id,
        "fee": fee,
        "source": source,
        "text": text,
        "client_order_id": text,
        "timestamp": datetime.now().isoformat(),
        "mode": mode,
        "cost_model": COST_MODEL_VERSION,
        "timeframe": timeframe,
        "finish_as": _ack_finish_as(ack),
        **trade_ctx_fields(trade),
    }
    record_live_trade(rec)


def _attribute_acked_batch_sell(
    trading: Any,
    *,
    symbol: str,
    timeframe: str,
    price: float,
    amount: float,
    requested: float,
    ack: dict[str, Any],
    text: str,
    source: str,
    action: str,
) -> bool:
    """Book one acked fill under the ledger lock. No second exchange order.

    ``price`` and ``amount`` are the fill, not the cascade snapshot.
    """
    from bus.locks import ledger_lock
    from core.tenant_context import resolve_tenant_scope
    from data_manager import uses_exchange_ledger

    portfolio = _portfolio_for_batch(trading)
    if portfolio is None:
        log(
            f"liq_cascade batch ack but no portfolio to attribute {symbol} {timeframe}",
            "ERROR",
        )
        return False
    cfg = getattr(trading, "config", None)
    sync_virtual = not uses_exchange_ledger(_trading_mode_name(trading))
    try:
        with ledger_lock(resolve_tenant_scope(), cfg=cfg):
            result = portfolio.execute_sell(
                symbol,
                timeframe,
                price,
                action,
                amount,
                source=source,
                order_id=str(ack.get("id") or "") or None,
                sync_virtual_ledger=sync_virtual,
            )
            if not bool(getattr(result, "executed", False)):
                log(
                    f"liq_cascade batch attribute rejected {symbol}: "
                    f"{getattr(result, 'message', '')}",
                    "ERROR",
                )
                return False
            try:
                from strategies.positions import hard_clear_closed_lot

                hard_clear_closed_lot(symbol, timeframe)
            except Exception as exc:
                log(f"liq_cascade batch hard_clear failed {symbol}: {exc}", "ERROR")
            _record_batch_fill(
                trading=trading,
                symbol=symbol,
                timeframe=timeframe,
                action=action,
                source=source,
                text=text,
                requested=requested,
                filled=amount,
                fill_price=price,
                ack=ack,
                sell_result=result,
            )
    except Exception as exc:
        log(f"liq_cascade batch attribute failed {symbol}: {exc}", "ERROR")
        return False
    return True


def _mark_closed(order: dict[str, Any], *, how: str) -> None:
    _last_exit_at[str(order["symbol"])] = time.monotonic()
    log(
        f"liq_cascade batch SELL_FULL {order['symbol']} {order['timeframe']} "
        f"px={float(order['price']):.6g} amt={float(order['amount']):.6g} :: {how}",
        "INFO",
    )


def execute_cascade_exit_batch(
    *,
    side: str,
    lots: list[dict[str, Any]] | None = None,
    prices: dict[str, float] | None = None,
    trading: Any | None = None,
    fire_enabled: bool | None = None,
    raw_config: dict | None = None,
    now_mono: float | None = None,
    state: Any | None = None,
) -> dict[str, Any]:
    """Batch close for an already-selected long #564 set.

    Shorts stay on sequential COVER_FULL (``execute_cascade_exit``). Spot
    batch is SELL_FULL, source ``liq_cascade``, and is not routed through
    the trail exit. ``fire_enabled`` false returns before any flatten, even
    when the caller reached this function with batch enabled.
    """
    from core.actions import COVER_FULL, SELL_FULL
    from services.exit_realtime.config import cascade_config
    from strategies.sell_sources import LIQ_CASCADE_SOURCE

    side_key = "short" if str(side or "").strip().lower() in ("short", "pump") else "long"
    short_side = side_key == "short"
    action = COVER_FULL if short_side else SELL_FULL
    cc = cascade_config(raw_config)
    if fire_enabled is None:
        fire_enabled = bool(cc.get("fire_enabled"))
    mono = float(now_mono if now_mono is not None else time.monotonic())

    if not fire_enabled:
        log(
            f"liq_cascade fire side={side_key} fire_enabled=false — detector only, no flatten",
            "INFO",
        )
        return {
            "ok": True,
            "executed": False,
            "message": "fire_disabled",
            "side": side_key,
            "action": action,
            "filled": 0,
            "results": [],
        }

    if short_side:
        return execute_cascade_exit(
            side=side,
            lots=lots,
            prices=prices,
            trading=trading,
            fire_enabled=True,
            raw_config=raw_config,
            now_mono=now_mono,
            state=state,
        )

    return _execute_long_cascade_batch(
        side_key=side_key,
        action=action,
        lots=lots,
        prices=prices,
        trading=trading,
        raw_config=raw_config,
        mono=mono,
        state=state,
        source=LIQ_CASCADE_SOURCE,
    )


def _execute_long_cascade_batch(
    *,
    side_key: str,
    action: str,
    lots: list[dict[str, Any]] | None,
    prices: dict[str, float] | None,
    trading: Any | None,
    raw_config: dict | None,
    mono: float,
    state: Any | None,
    source: str,
) -> dict[str, Any]:
    from strategies.positions import get_position, is_open_position
    from strategies.short_math import is_short as _is_short

    px_map = dict(prices or {})
    if lots is None:
        from strategies.positions import list_active_positions

        lots = list(list_active_positions() or [])

    snapshot: list[dict[str, Any]] = []
    for lot in lots or []:
        if not isinstance(lot, dict):
            continue
        try:
            if bool(_is_short(lot)):
                continue
        except Exception:
            continue
        snapshot.append(lot)

    if trading is None:
        from services.trading_service import TradingService

        trading = TradingService()

    results: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    claimed: set[str] = set()
    filled = 0

    def _release_if_unused(sym: str) -> None:
        if any(row["symbol"] == sym for row in selected):
            return
        with _inflight_lock:
            claimed.discard(sym)
            _inflight.discard(sym)

    try:
        for lot in snapshot:
            sym = str(lot.get("symbol") or "")
            tf = str(lot.get("timeframe") or "1h")
            if not sym:
                continue
            try:
                px = float(
                    px_map.get(sym) or lot.get("current_price") or lot.get("last_price") or 0
                )
            except (TypeError, ValueError):
                px = 0.0
            if px <= 0:
                results.append(
                    _evidence(
                        symbol=sym,
                        timeframe=tf,
                        executed=False,
                        code="no_price",
                        message=_english_failed(sym, "no_price"),
                        action=action,
                    )
                )
                continue

            with _inflight_lock:
                ours = sym in claimed
                if sym in _inflight and not ours:
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="inflight",
                            message=_english_failed(sym, "inflight"),
                            action=action,
                        )
                    )
                    continue
                if recently_exited(sym, within_sec=60.0) and not ours:
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="recent_exit",
                            message=_english_failed(sym, "recent_exit"),
                            action=action,
                        )
                    )
                    continue
                if not ours:
                    _inflight.add(sym)
                    claimed.add(sym)

            try:
                pos = get_position(sym, tf) or dict(lot)
                if not is_open_position(pos):
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="no_open_position",
                            message=_english_failed(sym, "no_open_position"),
                            action=action,
                        )
                    )
                    _release_if_unused(sym)
                    continue
                amount = float(pos.get("amount") or lot.get("amount") or 0)
                if amount <= 0:
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="amount_zero",
                            message=_english_failed(sym, "amount_zero"),
                            action=action,
                        )
                    )
                    _release_if_unused(sym)
                    continue
                try:
                    from strategies.position_lock import (
                        attach_lock_from_ledger,
                        auto_sell_blocked,
                        log_lock_block,
                    )

                    pos = attach_lock_from_ledger(pos, sym, tf) or pos
                    locked, lock_msg = auto_sell_blocked(pos, source)
                    if locked:
                        log_lock_block(sym, lock_msg, source=source)
                        log(
                            f"liq_cascade position_locked {sym} {tf} side={side_key} :: {lock_msg}",
                            "INFO",
                        )
                        results.append(
                            _evidence(
                                symbol=sym,
                                timeframe=tf,
                                executed=False,
                                code="position_locked",
                                message=_english_failed(sym, "position_locked", str(lock_msg or "")),
                                action=action,
                            )
                        )
                        _release_if_unused(sym)
                        continue
                except Exception as exc:
                    log(f"liq_cascade position_lock check error {sym}: {exc}", "ERROR")
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="position_lock_check_error",
                            message=f"failed {sym}: position lock check error"[:240],
                            action=action,
                        )
                    )
                    _release_if_unused(sym)
                    continue

                if not _lot_in_profit(pos, px, raw_config):
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="not_in_profit",
                            message=_english_failed(sym, "not_in_profit"),
                            action=action,
                        )
                    )
                    _release_if_unused(sym)
                    continue

                if ":" in sym:
                    results.append(
                        _evidence(
                            symbol=sym,
                            timeframe=tf,
                            executed=False,
                            code="not_spot",
                            message=_english_failed(sym, "not_spot"),
                            action=action,
                        )
                    )
                    _release_if_unused(sym)
                    continue

                selected.append(
                    {
                        "symbol": sym,
                        "timeframe": tf,
                        "amount": amount,
                        "price": px,
                        "text": _cascade_batch_text(),
                        "action": action,
                    }
                )
            except Exception as exc:
                log(f"liq_cascade batch select error {sym}: {exc}", "ERROR")
                results.append(
                    _evidence(
                        symbol=sym,
                        timeframe=tf,
                        executed=False,
                        code="select_error",
                        message=f"failed {sym}: {exc}"[:240],
                        action=action,
                    )
                )
                _release_if_unused(sym)

        filled, abort_reason = _submit_long_cascade_batches(
            selected=selected,
            results=results,
            trading=trading,
            action=action,
            side_key=side_key,
            source=source,
        )
        if filled > 0 and state is not None:
            try:
                state.note_fill(side_key, mono)
            except Exception as exc:
                log(f"liq_cascade note_fill: {exc}", "DEBUG")
        if abort_reason:
            message = abort_reason
        else:
            message = "ok" if filled else "no_fill"
        return {
            "ok": True,
            "executed": filled > 0,
            "message": message,
            "side": side_key,
            "action": action,
            "filled": filled,
            "batch": True,
            "results": results,
        }
    finally:
        with _inflight_lock:
            for sym in claimed:
                _inflight.discard(sym)


def _refuse_batch_fire(
    selected: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    action: str,
    code: str,
) -> None:
    for order in selected:
        results.append(
            _evidence(
                symbol=order["symbol"],
                timeframe=order["timeframe"],
                executed=False,
                code=code,
                message=_english_failed(order["symbol"], code),
                action=action,
                price=order["price"],
                amount=order["amount"],
                text=order["text"],
            )
        )
    log(
        f"liq_cascade batch abort {code} orders={len(selected)} — no batch submitted",
        "ERROR",
    )


def _batch_fire_refused(
    trading: Any,
    source: str,
    selected: list[dict[str, Any]],
    results: list[dict[str, Any]],
    action: str,
) -> str:
    """Lease and intent guards. Empty string means the fire may send.

    A missing writer lease aborts the whole fire (``no_writer_lease``).
    When the runtime would queue an intent, this path fails closed
    (``intent_queue_required``) and does not build an intent.
    """
    from bus.writer_lease import require_lease_for_order
    from services.trading_engine_runtime import should_queue_intent

    try:
        require_lease_for_order()
    except Exception as exc:
        log(
            f"liq_cascade batch no_writer_lease: {exc.__class__.__name__}: {exc}",
            "ERROR",
        )
        _refuse_batch_fire(selected, results, action=action, code="no_writer_lease")
        return "no_writer_lease"
    cfg = getattr(trading, "config", None)
    try:
        queue = bool(should_queue_intent(source, cfg))
    except Exception as exc:
        log(
            f"liq_cascade batch should_queue_intent failed: {exc.__class__.__name__}: {exc}",
            "ERROR",
        )
        _refuse_batch_fire(
            selected, results, action=action, code="intent_queue_required"
        )
        return "intent_queue_required"
    if queue:
        log(
            "liq_cascade batch intent queue is on — fail closed, no batch and no intent",
            "ERROR",
        )
        _refuse_batch_fire(
            selected, results, action=action, code="intent_queue_required"
        )
        return "intent_queue_required"
    return ""


def _submit_long_cascade_batches(
    *,
    selected: list[dict[str, Any]],
    results: list[dict[str, Any]],
    trading: Any,
    action: str,
    side_key: str,
    source: str,
) -> tuple[int, str]:
    """Submit chunks. Returns lots closed, and an abort reason when the fire did not send."""
    from execution.gate_adapter import BatchMarketSellNoAck

    if not selected:
        return 0, ""

    refused = _batch_fire_refused(trading, source, selected, results, action)
    if refused:
        return 0, refused

    seller = _cascade_batch_seller(trading)
    if seller is None:
        log(
            f"liq_cascade batch side={side_key} has no batch_market_sell — no flatten",
            "ERROR",
        )
        for order in selected:
            results.append(
                _evidence(
                    symbol=order["symbol"],
                    timeframe=order["timeframe"],
                    executed=False,
                    code="chunk_failure",
                    message=_english_failed(order["symbol"], "chunk_failure"),
                    action=action,
                )
            )
        return 0, ""

    chunks = _chunk_cascade_batch(selected)
    aborted = False
    explicit_failures: list[tuple[dict[str, Any], dict[str, Any]]] = []
    filled = 0

    for chunk in chunks:
        if aborted:
            for order in chunk:
                results.append(
                    _evidence(
                        symbol=order["symbol"],
                        timeframe=order["timeframe"],
                        executed=False,
                        code="chunk_failure",
                        message=_english_failed(order["symbol"], "chunk_failure"),
                        action=action,
                        price=order["price"],
                        amount=order["amount"],
                    )
                )
            continue
        payload = [
            {
                "symbol": order["symbol"],
                "amount": order["amount"],
                "price": order["price"],
                "text": order["text"],
                "account": "spot",
                "side": "sell",
                "type": "market",
            }
            for order in chunk
        ]
        try:
            ack = seller(payload)
        except BatchMarketSellNoAck as exc:
            log(
                f"liq_cascade batch abort side={side_key} orders={len(chunk)}: {exc}",
                "ERROR",
            )
            aborted = True
            _append_transport_rows(chunk, results, action=action)
            continue
        except Exception as exc:
            log(
                f"liq_cascade batch abort side={side_key} orders={len(chunk)}: "
                f"{exc.__class__.__name__}: {exc}",
                "ERROR",
            )
            aborted = True
            _append_transport_rows(chunk, results, action=action)
            continue
        if not isinstance(ack, list) or not ack:
            log(
                f"liq_cascade batch abort side={side_key} orders={len(chunk)}: "
                "no per-order ack list",
                "ERROR",
            )
            aborted = True
            _append_transport_rows(chunk, results, action=action)
            continue

        matched, missing = _match_batch_acks(chunk, ack)
        for order in missing:
            _brake_resend(order["symbol"], why="no_ack")
            results.append(
                _evidence(
                    symbol=order["symbol"],
                    timeframe=order["timeframe"],
                    executed=False,
                    code="no_ack",
                    message=_english_failed(order["symbol"], "no_ack"),
                    action=action,
                    price=order["price"],
                    amount=order["amount"],
                    text=order["text"],
                )
            )
            log(
                f"liq_cascade batch no_ack {order['symbol']} {order['timeframe']} "
                "— fail closed, no sequential retry",
                "ERROR",
            )
        for order, row in matched:
            flag = _ack_succeeded(row)
            if flag is True:
                requested = float(order["amount"])
                kind, filled_amt, fill_px = _classify_succeeded_batch_ack(row, requested)
                if kind == "zero_fill_cancel":
                    finish_as = _ack_finish_as(row)
                    explicit_failures.append((order, row))
                    results.append(
                        _evidence(
                            symbol=order["symbol"],
                            timeframe=order["timeframe"],
                            executed=False,
                            code="zero_fill_cancel",
                            message=_english_failed(
                                order["symbol"], "zero_fill_cancel", finish_as
                            ),
                            action=action,
                            price=order["price"],
                            amount=order["amount"],
                            text=order["text"],
                            gate_label=finish_as,
                            gate_message=str(row.get("message") or ""),
                            finish_as=finish_as,
                            filled_amount=0.0,
                        )
                    )
                    log(
                        f"liq_cascade batch zero_fill_cancel {order['symbol']} "
                        f"finish_as={finish_as or 'unset'} — no flatten, "
                        "sequential remainder eligible",
                        "INFO",
                    )
                elif kind != "fill":
                    _brake_resend(order["symbol"], why=kind)
                    results.append(
                        _evidence(
                            symbol=order["symbol"],
                            timeframe=order["timeframe"],
                            executed=False,
                            code=kind,
                            message=_english_failed(order["symbol"], kind),
                            action=action,
                            price=order["price"],
                            amount=order["amount"],
                            text=order["text"],
                            needs_reconcile=True,
                            finish_as=_ack_finish_as(row),
                        )
                    )
                    log(
                        f"liq_cascade batch {kind} {order['symbol']} — "
                        "succeeded without a bookable fill, no sequential retry",
                        "ERROR",
                    )
                else:
                    ok = _attribute_acked_batch_sell(
                        trading,
                        symbol=order["symbol"],
                        timeframe=order["timeframe"],
                        price=fill_px,
                        amount=filled_amt,
                        requested=requested,
                        ack=row,
                        text=str(order.get("text") or ""),
                        source=source,
                        action=action,
                    )
                    if ok:
                        filled += 1
                        booked = dict(order)
                        booked["price"] = fill_px
                        booked["amount"] = filled_amt
                        _mark_closed(booked, how="batch ack")
                        results.append(
                            _evidence(
                                symbol=order["symbol"],
                                timeframe=order["timeframe"],
                                executed=True,
                                code="batch_ack",
                                message=(
                                    f"closed {order['symbol']}: batch market sell acknowledged"
                                ),
                                action=action,
                                price=fill_px,
                                amount=filled_amt,
                                text=order["text"],
                                exchange_order_id=str(row.get("id") or ""),
                                finish_as=_ack_finish_as(row),
                                filled_amount=filled_amt,
                            )
                        )
                    else:
                        _brake_resend(order["symbol"], why="attribution_failed")
                        results.append(
                            _evidence(
                                symbol=order["symbol"],
                                timeframe=order["timeframe"],
                                executed=False,
                                code="attribution_failed",
                                message=(
                                    f"failed {order['symbol']}: exchange ack but local "
                                    "attribution failed"
                                )[:240],
                                action=action,
                                price=fill_px or order["price"],
                                amount=filled_amt,
                                text=order["text"],
                                needs_reconcile=True,
                            )
                        )
                        log(
                            f"liq_cascade batch attribution_failed {order['symbol']} "
                            "— no sequential retry",
                            "ERROR",
                        )
            elif flag is False:
                label = str(row.get("label") or "").strip()
                message = str(row.get("message") or "").strip()
                code = label or "gate_rejected"
                explicit_failures.append((order, row))
                results.append(
                    _evidence(
                        symbol=order["symbol"],
                        timeframe=order["timeframe"],
                        executed=False,
                        code=code,
                        message=_english_failed(order["symbol"], code, message),
                        action=action,
                        price=order["price"],
                        amount=order["amount"],
                        text=order["text"],
                        gate_label=label,
                        gate_message=message,
                    )
                )
            else:
                _brake_resend(order["symbol"], why="no_ack")
                results.append(
                    _evidence(
                        symbol=order["symbol"],
                        timeframe=order["timeframe"],
                        executed=False,
                        code="no_ack",
                        message=_english_failed(order["symbol"], "no_ack"),
                        action=action,
                        price=order["price"],
                        amount=order["amount"],
                        text=order["text"],
                    )
                )
                log(
                    f"liq_cascade batch uncertain ack {order['symbol']} — fail closed",
                    "ERROR",
                )

    if aborted:
        log(
            f"liq_cascade batch side={side_key} aborted further submits "
            f"explicit_failures_not_retried={len(explicit_failures)}",
            "ERROR",
        )
        return filled, ""

    for order, row in explicit_failures:
        if _sequential_remainder(
            trading=trading,
            order=order,
            source=source,
            results=results,
        ):
            filled += 1
    return filled, ""


def _brake_resend(symbol: str, *, why: str) -> None:
    """Block a second cascade sell for 60s. Does not count as a fill."""
    _last_exit_at[str(symbol)] = time.monotonic()
    log(
        f"liq_cascade batch resend brake {symbol} ({why}) — no fill invented",
        "ERROR",
    )


def _append_transport_rows(chunk, results, *, action: str) -> None:
    for order in chunk:
        _brake_resend(order["symbol"], why="transport_error")
        results.append(
            _evidence(
                symbol=order["symbol"],
                timeframe=order["timeframe"],
                executed=False,
                code="transport_error",
                message=_english_failed(order["symbol"], "transport_error"),
                action=action,
                price=order["price"],
                amount=order["amount"],
                text=order["text"],
            )
        )


def _sequential_remainder(
    *,
    trading: Any,
    order: dict[str, Any],
    source: str,
    results: list[dict[str, Any]],
) -> bool:
    """One SELL_FULL for a lot whose batch ACK said succeeded=false. Same snapshot."""
    from core.models import TradeOrder

    sym = order["symbol"]
    tf = order["timeframe"]
    text = str(order.get("text") or "")
    client_id = text[2:] if text.startswith("t-") else text
    trade = TradeOrder(
        type="SELL",
        symbol=sym,
        price=float(order["price"]),
        amount=float(order["amount"]),
        signal=order["action"],
        source=source,
        exit_source=source,
        exit_rationale="liq cascade dump full exit",
        client_order_id=client_id,
    )
    try:
        result = trading.execute_order(trade, tf, source=source, confidence=80.0)
    except Exception as exc:
        log(f"liq_cascade batch sequential remainder error {sym}: {exc}", "ERROR")
        _stamp_remainder(results, order, executed=False, message=str(exc)[:200])
        return False
    executed = bool(getattr(result, "executed", False))
    msg = str(getattr(result, "message", "") or "")
    if executed:
        _mark_closed(order, how="sequential remainder")
        _stamp_remainder(results, order, executed=True, message=msg)
        return True
    log(
        f"liq_cascade batch sequential remainder failed {sym}: {msg[:160]}",
        "INFO",
    )
    _stamp_remainder(results, order, executed=False, message=msg)
    return False


def _stamp_remainder(
    results: list[dict[str, Any]],
    order: dict[str, Any],
    *,
    executed: bool,
    message: str,
) -> None:
    for row in results:
        if row.get("text") == order.get("text") and row.get("symbol") == order["symbol"]:
            label = str(row.get("gate_label") or row.get("code") or "")
            row["sequential"] = True
            row["executed"] = executed
            row["status"] = "closed" if executed else "failed"
            if executed:
                row["code"] = "sequential_remainder"
                row["message"] = (
                    f"closed {order['symbol']}: sequential remainder after {label}"
                )[:240]
            else:
                row["message"] = (
                    f"failed {order['symbol']}: {label}; sequential remainder did not fill"
                    + (f" ({message})" if message else "")
                )[:240]
            return

