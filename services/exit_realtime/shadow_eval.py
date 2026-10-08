"""Pure would-sell evaluation for realtime shadow (no I/O)."""

from __future__ import annotations

from typing import Any

from core.models import MarketContext


def to_gate_pair(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}_{quote}"
    if s.endswith("USDT"):
        return f"{s[:-4]}_USDT"
    return s


def from_gate_pair(pair: str) -> str:
    p = str(pair or "").strip().upper()
    if "_" in p:
        return p.replace("_", "/", 1)
    return p


def build_market(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    average_entry: float,
    atr_pct: float = 3.0,
    strategy_params: dict | None = None,
) -> MarketContext:
    return MarketContext(
        symbol=symbol,
        timeframe=timeframe or "1h",
        current_price=float(price),
        has_position=True,
        average_entry=float(average_entry or 0),
        atr_pct=float(atr_pct or 3.0),
        strategy_params=dict(strategy_params or {}),
    )


def _positive_number(value: Any) -> float | None:
    """Parsed number > 0, or None. Non-numeric input is None, never an exception."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN or not strictly positive
        return None
    return number


def _confirm_long_hard_stop(
    last_hit: tuple[str, str] | None,
    bid_hit: tuple[str, str] | None,
) -> tuple[str, str] | None:
    """Fire only the level crossed on both last and bid. Full only if both are full."""
    if not last_hit or not bid_hit:
        return None
    last_action, src = last_hit
    bid_action = bid_hit[0]
    if last_action == "SELL_STOP_FULL" and bid_action == "SELL_STOP_FULL":
        return "SELL_STOP_FULL", src
    if last_action in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL") and bid_action in (
        "SELL_STOP_FULL",
        "SELL_STOP_PARTIAL",
    ):
        return "SELL_STOP_PARTIAL", src
    return None


def evaluate_would_sells(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    position: dict[str, Any],
    strategy_params: dict[str, Any],
    sources: frozenset[str] | set[str] | None = None,
    atr_pct: float = 3.0,
    bid: Any = None,
    stop_position: dict[str, Any] | None = None,
    base_stop_loss_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Return list of would-sell events (dicts) for allowed sources."""
    allowed = sources or frozenset({"trailing_take_profit", "trailing_stop"})
    try:
        px = float(price)
    except (TypeError, ValueError):
        return []
    if px <= 0:
        return []
    entry = float(position.get("average_entry") or 0)
    stop_pos = dict(stop_position) if isinstance(stop_position, dict) else dict(position)
    stop_entry = float(stop_pos.get("average_entry") or 0)
    if entry <= 0 and (stop_entry <= 0 or "stop_loss" not in allowed):
        return []

    # Work on a shallow copy so peak bump does not mutate caller unexpectedly
    pos = dict(position)
    recent_high = float(pos.get("recent_high") or 0)
    if px > recent_high:
        pos["recent_high"] = float(px)
        recent_high = float(px)

    out: list[dict[str, Any]] = []
    if "stop_loss" in allowed and stop_entry > 0:
        try:
            from strategies.dca import evaluate_long_hard_stop

            base_sl = base_stop_loss_pct
            if base_sl is None:
                raw_sl = (strategy_params or {}).get("stop_loss_pct")
                base_sl = float(raw_sl) if raw_sl is not None else None
            if base_sl is None:
                try:
                    from core.config import get_bot_config

                    base_sl = float(get_bot_config().stop_loss_pct)
                except Exception:
                    base_sl = 0.0
            last_hit = evaluate_long_hard_stop(
                price=px,
                entry=stop_entry,
                position=stop_pos,
                strategy_params=strategy_params,
                base_stop_loss_pct=float(base_sl),
            )
            if last_hit:
                bid_px = _positive_number(bid)
                if bid_px is None:
                    out.append(
                        {
                            "source": "stop_loss",
                            "action": "",
                            "skip": "ws_stop_skip_no_bid",
                            "bid_raw": bid,
                            "priority": 8,
                            "strategy_shadow": False,
                        }
                    )
                else:
                    bid_hit = evaluate_long_hard_stop(
                        price=bid_px,
                        entry=stop_entry,
                        position=stop_pos,
                        strategy_params=strategy_params,
                        base_stop_loss_pct=float(base_sl),
                    )
                    confirmed = _confirm_long_hard_stop(last_hit, bid_hit)
                    if confirmed:
                        action, src = confirmed
                        out.append(
                            {
                                "source": src,
                                "action": action,
                                "priority": 8,
                                "rationale": (
                                    f"WS hard stop {action} last={px:.8g} bid={bid_px:.8g}"
                                ),
                                "strategy_shadow": False,
                            }
                        )
        except Exception as exc:
            out.append({"source": "stop_loss", "error": str(exc)[:160]})

    if entry <= 0:
        market = None
        gain = 0.0
        peak_gain = 0.0
        drop = 0.0
    else:
        market = build_market(
            symbol=symbol,
            timeframe=timeframe,
            price=px,
            average_entry=entry,
            atr_pct=atr_pct,
            strategy_params=strategy_params,
        )
        gain = (px / entry - 1.0) * 100.0
        peak_gain = (recent_high / entry - 1.0) * 100.0 if recent_high > 0 else gain
        drop = (1.0 - px / recent_high) * 100.0 if recent_high > 0 else 0.0

    if market is not None and "trailing_take_profit" in allowed:
        try:
            from strategies.trailing_take_profit import evaluate_trailing_take_profit

            cand = evaluate_trailing_take_profit(market, pos, strategy_params)
            if cand:
                out.append(
                    {
                        "source": cand.source,
                        "action": cand.action,
                        "priority": cand.priority,
                        "rationale": cand.rationale,
                        "strategy_shadow": bool(cand.shadow_only),
                    }
                )
        except Exception as exc:
            out.append({"source": "trailing_take_profit", "error": str(exc)[:160]})

    if market is not None and "trailing_stop" in allowed:
        try:
            from strategies.trailing_stop import evaluate_trailing_stop

            cand = evaluate_trailing_stop(market, pos, strategy_params)
            if cand:
                out.append(
                    {
                        "source": cand.source,
                        "action": cand.action,
                        "priority": cand.priority,
                        "rationale": cand.rationale,
                        "strategy_shadow": bool(cand.shadow_only),
                    }
                )
        except Exception as exc:
            out.append({"source": "trailing_stop", "error": str(exc)[:160]})

    if market is not None and "rsi_sell" in allowed:
        try:
            from core.actions import SELL_FULL
            from strategies.indicator_regime import (
                apply_rsi_sell_overlay,
                rsi_full_close,
                trail_allow_rsi,
            )

            if trail_allow_rsi(None):
                params = apply_rsi_sell_overlay(dict(strategy_params or {}))
                last_rsi = float(pos.get("last_rsi") or 0)
                rsi_20 = float(params.get("rsi_sell_20") or 78)
                min_gain = float(params.get("rsi_sell_min_gain_pct") or 15)
                if last_rsi >= rsi_20 and gain >= min_gain:
                    action = SELL_FULL if rsi_full_close(None) else "SELL_20"
                    out.append(
                        {
                            "source": "rsi_sell",
                            "action": action,
                            "priority": 5,
                            "rationale": (
                                f"WS RSI->{action} (rsi={last_rsi:.0f}>={rsi_20:.0f}, "
                                f"gain={gain:.1f}%)"
                            ),
                            "strategy_shadow": False,
                        }
                    )
        except Exception as exc:
            out.append({"source": "rsi_sell", "error": str(exc)[:160]})

    if not out:
        return []

    # Peak may have been bumped on local pos copy
    recent_high = float(pos.get("recent_high") or recent_high)
    display_entry = entry if entry > 0 else stop_entry
    if display_entry > 0:
        gain = (px / display_entry - 1.0) * 100.0
        peak_gain = (recent_high / display_entry - 1.0) * 100.0 if recent_high > 0 else gain
        drop = (1.0 - px / recent_high) * 100.0 if recent_high > 0 else 0.0

    base = {
        "type": "exit_ws_shadow",
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(float(px), 10),
        "entry": round(display_entry, 10),
        "recent_high": round(float(recent_high), 10),
        "gain_pct": round(gain, 4),
        "peak_gain_pct": round(peak_gain, 4),
        "drop_from_high_pct": round(drop, 4),
        "atr_pct": round(float(atr_pct), 4),
    }
    return [{**base, **ev} for ev in out]
