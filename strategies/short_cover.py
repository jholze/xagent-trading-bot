"""Tick cover rules for paper shorts: stop, liquidation, time-cap.

Trail-TP / RSI-cover come later. No DCA/grid/RelVol.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from strategies.short_math import (
    apply_liq_buffer,
    clamp_leverage,
    is_short,
    liquidation_price_isolated,
    should_stop_or_liquidate,
    stop_price,
)
from strategies.short_policy import resolve_short_params, shorts_enabled


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def evaluate_short_cover(
    pos: dict | None,
    mark: float,
    *,
    now: datetime | None = None,
    symbol: str | None = None,
    config_raw: dict | None = None,
) -> dict[str, Any] | None:
    """Return {source, rationale} if this tick should COVER, else None."""
    if not shorts_enabled(config_raw):
        return None
    if not is_short(pos) or float((pos or {}).get("amount") or 0) <= 0:
        return None
    px = float(mark or 0)
    entry = float((pos or {}).get("average_entry") or 0)
    if px <= 0 or entry <= 0:
        return None
    params = resolve_short_params(
        symbol=symbol or (pos or {}).get("symbol"),
        tier=(pos or {}).get("strategy_tier"),
        lot=pos,
        config_raw=config_raw,
    )
    lev = clamp_leverage((pos or {}).get("leverage") or params["leverage"], cap=params["leverage_cap"])
    stop = stop_price("short", entry, float(params.get("stop_margin_pct") or 0.12), lev)
    liq = liquidation_price_isolated(
        "short",
        entry,
        lev,
        fee_rate=float(params.get("fee_rate") or 0.001),
    )
    liq = apply_liq_buffer("short", entry, liq, float(params.get("liquidation_buffer") or 0.05))
    hit = should_stop_or_liquidate("short", px, stop=stop, liq=liq)
    if hit == "liquidation":
        return {
            "source": "liquidation",
            "rationale": f"mark {px:g} >= liq {liq:g} (lev {lev:g}x)",
        }
    if hit == "stop":
        return {
            "source": "trailing_stop",
            "rationale": f"mark {px:g} >= stop {stop:g} (margin risk)",
        }
    opened = _parse_ts((pos or {}).get("entry_at") or (pos or {}).get("first_buy_at"))
    n = now or datetime.now(timezone.utc)
    cap_h = float(params.get("time_cap_hours") or 4)
    if opened and cap_h > 0:
        age_h = (n - opened).total_seconds() / 3600.0
        if age_h >= cap_h:
            return {
                "source": "time_cap",
                "rationale": f"held {age_h:.1f}h >= cap {cap_h:g}h",
            }
    return None
