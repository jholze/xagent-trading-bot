"""OHLCV structure helpers for reclaim / free-fall (staging sharp gates)."""

from __future__ import annotations

from typing import Any


def free_fall_from_bars(bars: list[list | tuple], *, n: int = 4) -> bool:
    """bars: ccxt-style [ts,o,h,l,c,v]. True if last n lows strictly decreasing."""
    if not bars or len(bars) < n:
        return False
    lows = [float(b[3]) for b in bars[-n:]]
    return all(lows[i] < lows[i - 1] for i in range(1, len(lows)))


def reclaim_ok_from_bars(bars: list[list | tuple], *, lookback: int = 12) -> bool:
    """3-bar non-declining lows + ≥3% bounce from local low."""
    if not bars or len(bars) < 4:
        return False
    if free_fall_from_bars(bars, n=4):
        return False
    window = bars[-min(lookback, len(bars)) :]
    local_low = min(float(b[3]) for b in window)
    if local_low <= 0:
        return False
    last = bars[-1]
    c = float(last[4])
    if c < local_low * 1.03:
        return False
    l0, l1, l2 = float(bars[-1][3]), float(bars[-2][3]), float(bars[-3][3])
    if not (l0 >= l1 * 0.998 and l1 >= l2 * 0.998):
        return False
    c3 = float(bars[-4][4]) if len(bars) >= 4 else c
    if c < c3 * 0.99:
        return False
    return True


def structure_flags_for_symbol(
    symbol: str,
    timeframe: str = "1h",
    *,
    limit: int = 24,
) -> dict[str, Any]:
    """Best-effort fetch; fail-open with None flags if market unavailable."""
    try:
        from services.market_service import MarketService

        ms = MarketService()
        bars = None
        if hasattr(ms, "fetch_ohlcv"):
            bars = ms.fetch_ohlcv(symbol, timeframe, limit)
        if not bars:
            return {
                "free_fall": None,
                "reclaim_ok": None,
                "structure_ok": None,
                "timeframe": timeframe,
            }
        # normalize if list of dicts
        if bars and isinstance(bars[0], dict):
            raw = [
                [
                    b.get("ts") or b.get("timestamp") or 0,
                    b.get("o") or b.get("open"),
                    b.get("h") or b.get("high"),
                    b.get("l") or b.get("low"),
                    b.get("c") or b.get("close"),
                    b.get("v") or b.get("volume") or 0,
                ]
                for b in bars
            ]
        else:
            raw = list(bars)
        ff = free_fall_from_bars(raw)
        rc = reclaim_ok_from_bars(raw)
        return {
            "free_fall": ff,
            "reclaim_ok": rc,
            "structure_ok": (not ff) and rc,
            "timeframe": timeframe,
            "bars": len(raw),
        }
    except Exception:
        return {
            "free_fall": None,
            "reclaim_ok": None,
            "structure_ok": None,
            "timeframe": timeframe,
        }


def structure_flags_multi_tf(
    symbol: str,
    timeframes: list[str] | tuple[str, ...] | None = None,
    *,
    limit: int = 24,
) -> dict[str, Any]:
    """Multi-timeframe structure for deep analysis.

    Aggregate rules (conservative for recovery):
    - free_fall if **any** TF free-falls
    - reclaim_ok if **any** higher TF (1h/4h) reclaim; else any TF if only 15m known
    - structure_ok = not free_fall and reclaim_ok
    """
    tfs = list(timeframes or ("15m", "1h", "4h"))
    by_tf: dict[str, dict[str, Any]] = {}
    for tf in tfs:
        by_tf[str(tf)] = structure_flags_for_symbol(symbol, str(tf), limit=limit)

    free_vals = [v.get("free_fall") for v in by_tf.values() if v.get("free_fall") is not None]
    free_fall: bool | None
    if not free_vals:
        free_fall = None
    else:
        free_fall = any(bool(x) for x in free_vals)

    # Prefer 1h/4h reclaim signals
    prefer = []
    for key in ("4h", "1h", "15m"):
        if key in by_tf and by_tf[key].get("reclaim_ok") is not None:
            prefer.append(bool(by_tf[key].get("reclaim_ok")))
    reclaim_ok: bool | None
    if not prefer:
        reclaim_ok = None
    else:
        # any higher-TF reclaim is enough to unlock small; all free-fall still blocks via free_fall
        reclaim_ok = any(prefer)

    structure_ok: bool | None
    if free_fall is None and reclaim_ok is None:
        structure_ok = None
    else:
        structure_ok = (free_fall is not True) and (reclaim_ok is True)

    return {
        "free_fall": free_fall,
        "reclaim_ok": reclaim_ok,
        "structure_ok": structure_ok,
        "structure_by_tf": by_tf,
        "timeframes": tfs,
    }
