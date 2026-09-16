"""Block late chase buys on prev-day gainer tops.

Issue #162: after a huge day, buying next session into further extension
is often poor; Decision Engine may still fire — risk layer soft-blocks.

Config gates (disabled, unknown symbol, source not in ``chase_guard_sources``,
same-day) stay fail-open. Issue #423: once a symbol *is* a chase-guard
candidate, a missing prev-day close is fail-closed — the last cached close
for that (symbol, day) is used if available, otherwise the buy is blocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from logger import log
from services.gainer_universe.config import gainer_universe_config
from services.gainer_universe.filters import normalize_symbol
from services.gainer_universe.store import load_gainer_state

# (symbol, prev_day) -> last successfully fetched prev-day close (#423 fallback)
_LAST_CLOSE: dict[tuple[str, str], float] = {}


def _prev_day_close(symbol: str, prev_day: str) -> float | None:
    """Close of calendar day prev_day (UTC) from 1d OHLCV.

    Returns None when the bar is missing or OHLCV fails; the caller decides
    (#423: fail-closed for chase_guard_sources).
    """
    try:
        from historical_prices import _fetch_ohlcv_range

        d0 = datetime.fromisoformat(prev_day).replace(tzinfo=timezone.utc)
        start = d0 - timedelta(days=1)
        end = d0 + timedelta(days=2)
        bars = _fetch_ohlcv_range(symbol, start, end, timeframe="1d") or []
        for b in bars:
            day = datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc).date().isoformat()
            if day == prev_day:
                c = float(b[4])
                if c > 0:
                    _LAST_CLOSE[(symbol, prev_day)] = c
                    return c
                return None
    except Exception as e:
        log(f"chase_guard ohlcv {symbol} {prev_day}: {e}", "WARNING")
    return None


def gainer_meta_for_symbol(symbol: str, state: dict | None = None) -> dict[str, Any] | None:
    state = state if state is not None else load_gainer_state()
    sym = normalize_symbol(symbol)
    for e in state.get("eligible") or []:
        if normalize_symbol(e.get("symbol") or "") == sym:
            return e
    return None


def check_gainer_chase_guard(
    symbol: str,
    price: float,
    *,
    config: dict | None = None,
    state: dict | None = None,
) -> tuple[bool, str]:
    """Return (blocked, reason).

    Config/eligibility gates are fail-open → (False, ""). For a symbol whose
    gainer source is in ``chase_guard_sources``, a missing prev-day close is
    fail-closed (#423): last cached close, else (True, reason).
    """
    cfg = gainer_universe_config(config)
    if not cfg.get("enabled") or cfg.get("mode") == "off":
        return False, ""
    if not cfg.get("chase_guard_enabled", True):
        return False, ""
    if price <= 0:
        return False, ""

    meta = gainer_meta_for_symbol(symbol, state)
    if not meta:
        return False, ""

    src = str(meta.get("source") or "")
    allowed = cfg.get("chase_guard_sources") or ["gate_prev_top"]
    if src not in allowed:
        return False, ""

    prev_day = str(meta.get("day") or "")
    if not prev_day:
        return False, ""

    # Only apply on days *after* the big day (same calendar day = still developing)
    today = datetime.now(timezone.utc).date().isoformat()
    if prev_day >= today:
        return False, ""

    sym = normalize_symbol(symbol)
    close = _prev_day_close(sym, prev_day)
    if not close or close <= 0:
        cached = _LAST_CLOSE.get((sym, prev_day))
        if cached and cached > 0:
            log(
                f"gainer_chase_guard: {symbol} prev close ({prev_day}) unavailable, "
                f"using cached {cached}",
                "WARNING",
            )
            close = cached
        else:
            return True, (
                f"gainer_chase_guard: {symbol} prev close ({prev_day}) unavailable "
                f"and no cached close — fail-closed (source={src})"
            )

    gain_from_close = (float(price) / close - 1.0) * 100.0
    max_gain = float(cfg.get("chase_max_gain_from_prev_close_pct") or 18.0)
    if gain_from_close > max_gain:
        msg = (
            f"gainer_chase_guard: {symbol} +{gain_from_close:.1f}% vs prev close "
            f"({prev_day}) > max {max_gain:.0f}% (source={src})"
        )
        return True, msg
    return False, ""
