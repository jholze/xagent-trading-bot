"""Per-tenant rule switches for the fresh-start package (T, G, V, E, limits).

Every switch is OFF unless the tenant config sets ``rule_switches.<rule>.enabled``
to true. A tenant without a ``rule_switches`` block behaves exactly as before.

Rules (formulas match the backtest harness in ``backtest/harness/scenarios.py``
and ``tools/entry_filter_replay.py``; see docs/domain/rule_switches_spec.md):

* T ``trend_filter``  — new entries only: last closed daily close > SMA50,
  close at most 20 % above SMA50, 24 h rise (last closed 1h close vs. 24 bars
  earlier) at most 20 %. Missing / gapped data keeps the entry.
* G ``no_chase``      — new entries only: skip when the price is more than 15 %
  above the lowest 1h low of the closed bars in the 48 h before the signal.
  Fewer than 36 bars keeps the entry.
* V ``vol_sizing``    — non-manual buys: size × min(1, 0.05 / realized daily
  vol) with vol = stdev of the last 24 closed hourly returns × sqrt(24).
  Missing data keeps size 1.0.
* E ``age_loss_exit`` — full exit of a lot that is at least 14 days old and at
  or below −15 % vs. its average entry. Locked lots only with
  ``include_locked``.
* ``limits``          — per-order USDT cap, max open positions, max % capital per
  coin, max lots per coin, drawdown brake on new entries.
* ``elig_*``          — criteria eligibility (#643 extension) inside the #641
  liquidity guard, ``services/venue_quality.evaluate_eligibility``. Each has a
  ``mode`` of ``off`` | ``shadow`` | ``enforce`` (default ``off``) and applies
  to every BUY incl. DCA adds: ``elig_liq7`` (7d median daily quote volume),
  ``elig_collapse90`` (close vs. 90d high), ``elig_chase48`` (price vs. 48h low),
  ``elig_mcap`` (the #563 long mcap floor, extended to DCA adds on an open lot).

Only bars that are closed at decision time are used (no lookahead).
Nothing here ever blocks a sell or a stop.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

HOUR = 3600
DAY = 86400

RULES = ("trend_filter", "no_chase", "vol_sizing", "age_loss_exit", "limits")

# Criteria eligibility (#643 extension). Mode switch instead of ``enabled``.
ELIG_RULES = ("elig_liq7", "elig_collapse90", "elig_chase48", "elig_mcap")
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

_DEFAULTS: dict[str, dict[str, Any]] = {
    "trend_filter": {
        "enabled": False,
        "sma_days": 50,
        "max_extension_pct": 20.0,
        "max_pump_24h_pct": 20.0,
    },
    "no_chase": {
        "enabled": False,
        "max_above_48h_low_pct": 15.0,
        "lookback_hours": 48,
        "min_bars": 36,
    },
    "vol_sizing": {
        "enabled": False,
        "target_daily_vol": 0.05,
    },
    "age_loss_exit": {
        "enabled": False,
        "min_age_days": 14.0,
        "loss_pct": 15.0,
        "include_locked": False,
    },
    "limits": {
        "enabled": False,
        "max_usdt_per_order": None,
        "max_open_positions": None,
        "max_position_pct": None,
        "max_lots_per_coin": None,
        "drawdown_brake_pct": None,
    },
    # Thresholds: kauf_setup report section 10, harness scenario (k) "elig_primary".
    # ``on_missing``: "keep" (harness parity, default) or "block" (#643 R3).
    "elig_liq7": {
        "mode": MODE_OFF,
        "min_median_quote_volume_usdt": 100_000.0,
        "days": 7,
        "min_days": 3,
        "on_missing": "keep",
    },
    "elig_collapse90": {
        "mode": MODE_OFF,
        "min_close_to_high": 0.20,
        "days": 90,
        "min_days": 20,
        "on_missing": "keep",
    },
    "elig_chase48": {
        "mode": MODE_OFF,
        "max_above_48h_low_pct": 15.0,
        "on_missing": "keep",
    },
    # #563 floor (5M volatile / 20M stable via resolve_long_market_cap_min_usd).
    # New long entries keep the always-on #563 check; this switch adds DCA adds
    # on an open lot. Unknown mcap blocks, as in #563.
    "elig_mcap": {
        "mode": MODE_OFF,
        "on_missing": "block",
    },
}

AGE_LOSS_EXIT_SOURCE = "age_loss_exit"



def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_float(value: Any, default: float | None) -> float | None:
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _as_pos_float(value: Any) -> float | None:
    out = _as_float(value, None)
    if out is None or out <= 0:
        return None
    return out


def _as_pos_int(value: Any) -> int | None:
    out = _as_float(value, None)
    if out is None or out < 1:
        return None
    return int(out)


def _raw_of(config: Any) -> dict:
    if isinstance(config, dict):
        return config
    raw = getattr(config, "raw", None)
    return raw if isinstance(raw, dict) else {}


def rule_config(config: Any, rule: str) -> dict[str, Any]:
    """Merged settings for *rule* (defaults + tenant block). ``enabled`` is a bool."""
    if rule not in _DEFAULTS:
        raise KeyError(rule)
    block = _raw_of(config).get("rule_switches")
    sub = block.get(rule) if isinstance(block, dict) else None
    out = dict(_DEFAULTS[rule])
    if isinstance(sub, dict):
        out.update({k: v for k, v in sub.items() if not str(k).startswith("_")})
    out["enabled"] = _as_bool(out.get("enabled", False))
    return out


def rule_mode(config: Any, rule: str) -> str:
    """``off`` | ``shadow`` | ``enforce`` for an ``elig_*`` rule. Missing = off.

    Booleans map true -> enforce, false -> off. Any other unknown value is
    malformed and maps to ``enforce`` (#643 R4: a malformed value must not
    silently lift a guard the tenant meant to set).
    """
    if rule not in ELIG_RULES:
        raise KeyError(rule)
    block = _raw_of(config).get("rule_switches")
    sub = block.get(rule) if isinstance(block, dict) else None
    if not isinstance(sub, dict) or "mode" not in sub:
        return MODE_OFF
    value = sub.get("mode")
    if value is None:
        return MODE_OFF
    if isinstance(value, bool):
        return MODE_ENFORCE if value else MODE_OFF
    text = str(value).strip().lower()
    if text in MODES:
        return text
    return MODE_ENFORCE


def elig_modes(config: Any) -> dict[str, str]:
    return {rule: rule_mode(config, rule) for rule in ELIG_RULES}


def any_elig_rule_on(config: Any) -> bool:
    return any(mode != MODE_OFF for mode in elig_modes(config).values())


def rule_enabled(config: Any, rule: str) -> bool:
    return bool(rule_config(config, rule)["enabled"])


def any_entry_rule_enabled(config: Any) -> bool:
    return any(rule_enabled(config, r) for r in ("trend_filter", "no_chase", "vol_sizing"))


def is_manual_source(source: Any) -> bool:
    """Owner-intent sources (same rule as position locks). T, G, V and the brake skip them."""
    from strategies.position_lock import is_manual_source as _is_manual

    return bool(_is_manual(str(source or "")))


@dataclass(frozen=True)
class Limits:
    max_usdt_per_order: float | None = None
    max_open_positions: int | None = None
    max_position_pct: float | None = None
    max_lots_per_coin: int | None = None
    drawdown_brake_pct: float | None = None

    @property
    def active(self) -> bool:
        return any(
            v is not None
            for v in (
                self.max_usdt_per_order,
                self.max_open_positions,
                self.max_position_pct,
                self.max_lots_per_coin,
                self.drawdown_brake_pct,
            )
        )


def limits_config(config: Any) -> Limits:
    """Tenant limits; every field None (= off) unless ``limits.enabled`` is true."""
    cfg = rule_config(config, "limits")
    if not cfg["enabled"]:
        return Limits()
    pct = _as_pos_float(cfg.get("max_position_pct"))
    brake = _as_pos_float(cfg.get("drawdown_brake_pct"))
    return Limits(
        max_usdt_per_order=_as_pos_float(cfg.get("max_usdt_per_order")),
        max_open_positions=_as_pos_int(cfg.get("max_open_positions")),
        max_position_pct=min(pct, 100.0) if pct is not None else None,
        max_lots_per_coin=_as_pos_int(cfg.get("max_lots_per_coin")),
        drawdown_brake_pct=min(brake, 100.0) if brake is not None else None,
    )


# --- candle helpers (closed bars only) -------------------------------------------------


def bar_open_unix(bar: Any) -> int:
    ts = int(float(bar[0]))
    return ts // 1000 if ts > 10_000_000_000 else ts


def normalize_bars(bars: Any) -> list[tuple[int, float, float, float, float, float]]:
    """Rows ``[ts, open, high, low, close, volume]`` (list or DataFrame) → sorted
    tuples with ts in unix seconds. Bad rows are dropped."""
    if bars is None:
        return []
    rows: Iterable[Any]
    if hasattr(bars, "itertuples") and hasattr(bars, "columns"):
        cols = ["ts", "open", "high", "low", "close", "volume"]
        if not all(c in bars.columns for c in cols):
            return []
        rows = bars[cols].itertuples(index=False, name=None)
    else:
        rows = bars
    out: dict[int, tuple[int, float, float, float, float, float]] = {}
    for row in rows:
        try:
            t = bar_open_unix(row)
            o, h, lo, c = (float(row[i]) for i in (1, 2, 3, 4))
            v = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if not all(math.isfinite(x) for x in (o, h, lo, c)):
            continue
        out[t] = (t, o, h, lo, c, v)
    return [out[k] for k in sorted(out)]


def closed_bars(bars: Any, signal_unix: int, bar_seconds: int) -> list[tuple]:
    """Only bars whose close time (open + bar_seconds) is <= *signal_unix*."""
    return [b for b in normalize_bars(bars) if b[0] + bar_seconds <= int(signal_unix)]


def sma_extension(daily_closed: list[tuple], length: int) -> tuple[bool | None, float | None]:
    """(close > SMA, extension %) on the last *length* contiguous closed daily bars."""
    if length < 1 or len(daily_closed) < length:
        return None, None
    window = daily_closed[-length:]
    if window[-1][0] - window[0][0] != (length - 1) * DAY:
        return None, None
    sma = statistics.fmean(b[4] for b in window)
    if sma <= 0:
        return None, None
    close = window[-1][4]
    return close > sma, (close / sma - 1.0) * 100.0


def pump_24h_pct(hourly_closed: list[tuple]) -> float | None:
    """Last closed 1h close vs. the close 24 bars earlier (contiguous), in %."""
    if len(hourly_closed) < 25:
        return None
    last = hourly_closed[-1]
    prev = hourly_closed[-25]
    if last[0] - prev[0] != 24 * HOUR or prev[4] <= 0:
        return None
    return (last[4] / prev[4] - 1.0) * 100.0


def realized_daily_vol(hourly_closed: list[tuple]) -> float | None:
    """stdev of the last 24 closed hourly returns × sqrt(24)."""
    if len(hourly_closed) < 25:
        return None
    window = hourly_closed[-25:]
    if window[-1][0] - window[0][0] != 24 * HOUR:
        return None
    rets: list[float] = []
    for a, b in zip(window, window[1:]):
        if a[4] <= 0:
            return None
        rets.append(b[4] / a[4] - 1.0)
    return statistics.stdev(rets) * math.sqrt(24.0)


def pump_above_low_pct(
    hourly_closed: list[tuple],
    price: float,
    signal_unix: int,
    *,
    lookback_hours: int = 48,
    min_bars: int = 36,
) -> float | None:
    """(price / lowest 1h low of the lookback window - 1) × 100, or None."""
    lo = int(signal_unix) - int(lookback_hours) * HOUR
    lows = [b[3] for b in hourly_closed if lo <= b[0] and b[0] + HOUR <= int(signal_unix)]
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        return None
    if len(lows) < int(min_bars) or min(lows) <= 0 or px <= 0:
        return None
    return (px / min(lows) - 1.0) * 100.0


def vol_multiplier(rv: float | None, target: float) -> float:
    if rv is None or rv <= 0 or target <= 0:
        return 1.0
    return min(1.0, float(target) / float(rv))


# --- decisions ------------------------------------------------------------------------


@dataclass
class EntryCheck:
    """Outcome of T/G for one buy. ``block_code`` None = allowed."""

    block_code: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_trend_filter(
    cfg: dict[str, Any], daily_closed: list[tuple], hourly_closed: list[tuple]
) -> EntryCheck:
    length = int(_as_float(cfg.get("sma_days"), 50) or 50)
    max_ext = _as_float(cfg.get("max_extension_pct"), 20.0)
    max_pump = _as_float(cfg.get("max_pump_24h_pct"), 20.0)
    above, ext = sma_extension(daily_closed, length)
    pump = pump_24h_pct(hourly_closed)
    details = {"above_sma": above, "extension_pct": ext, "pump_24h_pct": pump, "sma_days": length}
    if above is None or ext is None or pump is None:
        details["missing_data"] = True
        return EntryCheck(None, "trend filter: no data, entry kept", details)
    if not above:
        return EntryCheck("trend_filter_below_sma", f"trend filter: close below SMA{length}", details)
    if ext > max_ext:
        return EntryCheck(
            "trend_filter_extended",
            f"trend filter: close {ext:.1f}% above SMA{length} (max {max_ext:.0f}%)",
            details,
        )
    if pump > max_pump:
        return EntryCheck(
            "trend_filter_pump_24h",
            f"trend filter: 24h rise {pump:.1f}% (max {max_pump:.0f}%)",
            details,
        )
    return EntryCheck(None, "trend filter: pass", details)


def evaluate_no_chase(
    cfg: dict[str, Any], hourly_closed: list[tuple], price: float, signal_unix: int
) -> EntryCheck:
    limit = _as_float(cfg.get("max_above_48h_low_pct"), 15.0)
    pump = pump_above_low_pct(
        hourly_closed,
        price,
        signal_unix,
        lookback_hours=int(_as_float(cfg.get("lookback_hours"), 48) or 48),
        min_bars=int(_as_float(cfg.get("min_bars"), 36) or 36),
    )
    details = {"above_48h_low_pct": pump, "limit_pct": limit}
    if pump is None:
        details["missing_data"] = True
        return EntryCheck(None, "no-chase: no data, entry kept", details)
    if pump > limit:
        return EntryCheck(
            "no_chase_48h",
            f"no-chase: price {pump:.1f}% above 48h low (max {limit:.0f}%)",
            details,
        )
    return EntryCheck(None, "no-chase: pass", details)


def _parse_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Ledger timestamps without zone are Berlin wall time (to_naive_berlin_iso).
        try:
            from zoneinfo import ZoneInfo

            dt = dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def lot_entry_unix(position: dict | None) -> float | None:
    if not isinstance(position, dict):
        return None
    for key in ("entry_at", "opened_at", "first_buy_at", "created_at"):
        ts = _parse_ts(position.get(key))
        if ts is not None:
            return ts
    return None


def age_loss_exit_due(
    cfg: dict[str, Any],
    position: dict | None,
    price: float,
    now_unix: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """True when the lot is >= min_age_days old and price <= avg × (1 - loss_pct/100)."""
    details: dict[str, Any] = {}
    if not isinstance(position, dict):
        return False, details
    if str(position.get("side") or "long").lower() == "short":
        return False, details
    try:
        amount = float(position.get("amount") or 0)
        avg = float(position.get("average_entry") or 0)
        px = float(price or 0)
    except (TypeError, ValueError):
        return False, details
    if amount <= 0 or avg <= 0 or px <= 0:
        return False, details
    entry = lot_entry_unix(position)
    if entry is None:
        return False, {"missing_entry_at": True}
    now = float(now_unix if now_unix is not None else time.time())
    min_age = float(_as_float(cfg.get("min_age_days"), 14.0) or 0.0)
    loss = float(_as_float(cfg.get("loss_pct"), 15.0) or 0.0)
    age_days = (now - entry) / DAY
    pnl_pct = (px / avg - 1.0) * 100.0
    details = {"age_days": round(age_days, 3), "pnl_pct": round(pnl_pct, 3)}
    due = age_days >= min_age and px <= avg * (1.0 - loss / 100.0)
    return due, details
