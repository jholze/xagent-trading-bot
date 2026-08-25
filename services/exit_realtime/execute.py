"""Execute trail exits from WS path via TradingService (same risk/order path)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from logger import log

_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_last_exit_at: dict[str, float] = {}  # symbol -> mono time


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
) -> dict[str, Any]:
    """
    Full-position SELL through RiskManager + order path.

    When ``EXIT_EXECUTE_URL`` is set (sidecar), posts to the bot internal
    fire endpoint instead of executing locally — bot remains sole write path.
    Returns {ok, executed, message, ...}.
    """
    from services.exit_realtime.config import exit_execute_url, exit_ws_internal_token

    sym = str(symbol or "")
    tf = str(timeframe or "1h")
    px = float(price or 0)
    if not sym or px <= 0:
        return {"ok": False, "executed": False, "message": "bad_args"}

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
                except Exception:
                    pass
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
