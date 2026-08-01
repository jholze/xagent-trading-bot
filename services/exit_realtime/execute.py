"""Execute trail exits from WS path via TradingService (same risk/order path)."""

from __future__ import annotations

import threading
import time
from typing import Any

from logger import log

_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_last_exit_at: dict[str, float] = {}  # symbol -> mono time


def recently_exited(symbol: str, within_sec: float = 120.0) -> bool:
    t = _last_exit_at.get(symbol, 0.0)
    return t > 0 and (time.monotonic() - t) < within_sec


def try_execute_trail_exit(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    action: str,
    exit_source: str,
    rationale: str = "",
    trading: Any | None = None,
) -> dict[str, Any]:
    """
    Full-position SELL through RiskManager + order path.
    Returns {ok, executed, message, ...}.
    """
    from core.actions import SELL_FULL
    from core.models import TradeOrder
    from strategies.positions import (
        get_position,
        is_open_position,
        mark_trailing_take_profit_step,
    )

    sym = str(symbol or "")
    tf = str(timeframe or "1h")
    px = float(price or 0)
    if not sym or px <= 0:
        return {"ok": False, "executed": False, "message": "bad_args"}

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
