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
    from core.costs import CostModel

    gain = _gross_unrealized_pct(pos, price)
    rt = float(CostModel.from_config(raw_config).round_trip_pct())
    return (gain - rt) > 0.0


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
