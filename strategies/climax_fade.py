"""Paper climax-fade SHORT: closed 4h Gate bar, independent of a prior spot sell.

Formula (scan 2026-09-29): ret ≥ +6% and vol 4/20 ≥ 2.0 on a *closed* bar.
Cover doors: isolated liq, +10% price stop, 4h close ≤ −3% take, 16h time.
No I/O in the public formula helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.time_utils import ledger_datetime_utc
from strategies.short_math import (
    apply_liq_buffer,
    clamp_leverage,
    is_short,
    liquidation_price_isolated,
    should_stop_or_liquidate,
)

_FOUR_H_MS = 4 * 3600 * 1000
_SCAN_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "timeframe": "4h",
    "min_return_pct": 6.0,
    "vol_short": 4,
    "vol_long": 20,
    "vol_mult_min": 2.0,
    "cover_close_pct": 3.0,
    "stop_price_pct": 10.0,
    "time_cap_hours": 16,
    "size_factor": 0.5,
    "exclude_symbols": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"],
}

_COVER_SOURCES = frozenset({"climax_cover", "climax_stop", "climax_time", "liquidation"})


def climax_fade_config(raw: dict | None = None) -> dict[str, Any]:
    """Scan defaults. Missing block → enabled False (no new opens)."""
    out = dict(_SCAN_DEFAULTS)
    out["exclude_symbols"] = list(_SCAN_DEFAULTS["exclude_symbols"])
    shorts = (raw or {}).get("shorts") if isinstance(raw, dict) else None
    block = shorts.get("climax_fade") if isinstance(shorts, dict) else None
    if not isinstance(block, dict):
        out["enabled"] = False
        return out
    if "enabled" in block:
        out["enabled"] = bool(block.get("enabled"))
    else:
        out["enabled"] = False
    for key in (
        "timeframe",
        "min_return_pct",
        "vol_short",
        "vol_long",
        "vol_mult_min",
        "cover_close_pct",
        "stop_price_pct",
        "time_cap_hours",
        "size_factor",
    ):
        if key in block and block[key] is not None:
            out[key] = block[key]
    if isinstance(block.get("exclude_symbols"), list) and block["exclude_symbols"]:
        out["exclude_symbols"] = [normalize_climax_symbol(s) for s in block["exclude_symbols"]]
    return out


def normalize_climax_symbol(sym: str | None) -> str:
    s = str(sym or "").strip().upper().replace("-", "/")
    if not s:
        return ""
    if ":" in s:
        s = s.split(":", 1)[0]
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    if s.endswith("USDT") and "/" not in s:
        s = s[:-4] + "/USDT"
    return s


def is_excluded_symbol(symbol: str | None, cfg: dict | None = None) -> bool:
    want = normalize_climax_symbol(symbol)
    exclude = (cfg or {}).get("exclude_symbols") or _SCAN_DEFAULTS["exclude_symbols"]
    banned = {normalize_climax_symbol(s) for s in exclude}
    return want in banned


def vol_mult_4_20(volumes) -> float | None:
    """Mean(last 4) / mean(prior 16). Needs 20 bars. 16×100 + 4×250 → 2.5."""
    if volumes is None:
        return None
    try:
        vols = [float(v) for v in volumes]
    except (TypeError, ValueError):
        return None
    if len(vols) < 20:
        return None
    i = len(vols) - 1
    short = vols[i - 3 : i + 1]
    prior = vols[i - 19 : i - 3]
    if len(short) != 4 or len(prior) != 16:
        return None
    short_mean = sum(short) / 4.0
    long_mean = sum(prior) / 16.0
    if long_mean <= 0:
        return None
    return short_mean / long_mean


def bar_return(prev_close, close) -> float | None:
    try:
        prev = float(prev_close)
        last = float(close)
    except (TypeError, ValueError):
        return None
    if prev <= 0:
        return None
    return last / prev - 1.0


def is_closed_4h_bar(bar_ts_ms, now) -> bool:
    try:
        ts = int(bar_ts_ms)
    except (TypeError, ValueError):
        return False
    if now is None:
        n = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        return False
    now_ms = int(n.timestamp() * 1000)
    return now_ms >= ts + _FOUR_H_MS


def _vol_mult_at(volumes, bar_index: int) -> float | None:
    if volumes is None or bar_index < 19:
        return None
    window = list(volumes)[: bar_index + 1]
    if len(window) < 20:
        return None
    return vol_mult_4_20(window[-20:])


def climax_entry_signal(*, closes, volumes, bar_index, cfg) -> bool:
    """True only on closed-bar index ``i`` (caller must not pass the forming bar)."""
    try:
        i = int(bar_index)
    except (TypeError, ValueError):
        return False
    if i < 19 or closes is None or volumes is None:
        return False
    if i >= len(closes) or i >= len(volumes):
        return False
    cfg = cfg or _SCAN_DEFAULTS
    try:
        min_ret = float(cfg.get("min_return_pct") or 6.0) / 100.0
        vol_min = float(cfg.get("vol_mult_min") or 2.0)
    except (TypeError, ValueError):
        return False
    ret = bar_return(closes[i - 1], closes[i])
    if ret is None or ret < min_ret:
        return False
    vm = _vol_mult_at(volumes, i)
    if vm is None or vm < vol_min:
        return False
    return True


def is_climax_lot(lot: dict | None) -> bool:
    if not isinstance(lot, dict):
        return False
    return (
        str(lot.get("short_recipe") or "").strip() == "climax_fade"
        or str(lot.get("exit_source") or "").strip() == "climax_fade"
    )


def _entry_px(lot: dict) -> float:
    for key in ("average_entry", "entry_price", "last_buy_price"):
        try:
            px = float(lot.get(key) or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            return px
    return 0.0


def lot_entry_at_ms(lot: dict | None) -> int | None:
    opened = ledger_datetime_utc(
        (lot or {}).get("entry_at") or (lot or {}).get("first_buy_at")
    )
    if opened is None:
        return None
    return int(opened.timestamp() * 1000)


def climax_cover_decision(
    lot,
    *,
    closed_4h_close,
    mark,
    now,
    cfg,
    ws_tick: bool = False,
    bar_open_ts_ms=None,
) -> dict | None:
    """Priority: Liq → Stop → Take → Time. WS tick: stop/liq only."""
    if not is_climax_lot(lot) or not is_short(lot):
        return None
    if float((lot or {}).get("amount") or 0) <= 0:
        return None
    entry = _entry_px(lot or {})
    try:
        px = float(mark or 0)
    except (TypeError, ValueError):
        px = 0.0
    if entry <= 0 or px <= 0:
        return None
    cfg = cfg or _SCAN_DEFAULTS
    try:
        stop_pct = float(cfg.get("stop_price_pct") or 10.0) / 100.0
        take_pct = float(cfg.get("cover_close_pct") or 3.0) / 100.0
        cap_h = float(cfg.get("time_cap_hours") or 16)
    except (TypeError, ValueError):
        stop_pct, take_pct, cap_h = 0.10, 0.03, 16.0

    lev = clamp_leverage((lot or {}).get("leverage") or 2, cap=2)
    try:
        from core.costs import CostModel

        fee_frac = CostModel.from_config(None, market="swap").fee_pct("market") / 100.0
    except Exception:
        fee_frac = 0.0005
    liq = liquidation_price_isolated("short", entry, lev, fee_frac=fee_frac)
    liq = apply_liq_buffer("short", entry, liq, 0.05)
    hit = should_stop_or_liquidate("short", px, stop=None, liq=liq)
    if hit == "liquidation":
        return {
            "source": "liquidation",
            "rationale": f"mark {px:g} >= liq {liq:g} (lev {lev:g}x)",
        }

    stop_px = entry * (1.0 + stop_pct)
    if (px / entry - 1.0) >= stop_pct - 1e-12:
        return {
            "source": "climax_stop",
            "rationale": f"mark {px:g} >= stop {stop_px:g} (+{stop_pct * 100:g}% price)",
        }

    if not ws_tick and closed_4h_close is not None:
        take_ok = True
        if bar_open_ts_ms is not None:
            entry_ms = lot_entry_at_ms(lot)
            try:
                bar_ts = int(bar_open_ts_ms)
            except (TypeError, ValueError):
                bar_ts = None
            # Only a bar that opened (and therefore closed) after entry.
            if bar_ts is None or entry_ms is None or bar_ts < entry_ms:
                take_ok = False
        if take_ok:
            try:
                c4 = float(closed_4h_close)
            except (TypeError, ValueError):
                c4 = 0.0
            take_px = entry * (1.0 - take_pct)
            if c4 > 0 and (c4 / entry - 1.0) <= -take_pct + 1e-12:
                return {
                    "source": "climax_cover",
                    "rationale": f"4h close {c4:g} <= take {take_px:g} (−{take_pct * 100:g}%)",
                }

    if not ws_tick and cap_h > 0:
        opened = ledger_datetime_utc(
            (lot or {}).get("entry_at") or (lot or {}).get("first_buy_at")
        )
        n = now or datetime.now(timezone.utc)
        if opened is not None:
            if n.tzinfo is None:
                n = n.replace(tzinfo=timezone.utc)
            age_h = (n - opened).total_seconds() / 3600.0
            if age_h >= cap_h:
                return {
                    "source": "climax_time",
                    "rationale": f"held {age_h:.1f}h >= cap {cap_h:g}h",
                }
    return None


def last_closed_4h_bar(
    symbol,
    now,
    *,
    market=None,
    ohlcv=None,
    entry_at_ms=None,
) -> tuple[float, int] | None:
    """Last *closed* 4h (close, bar_open_ts_ms). Optional ``entry_at_ms`` keeps
    only bars that opened at/after entry. Missing data → None (fail-closed)."""
    df = ohlcv
    if df is None and market is not None:
        try:
            df = market.fetch_ohlcv(symbol, "4h", 30)
        except Exception:
            return None
    if df is None:
        return None
    try:
        if hasattr(df, "empty") and df.empty:
            return None
        ts_col = df["ts"] if "ts" in df.columns else None
        close_col = df["close"] if "close" in df.columns else None
        if ts_col is None or close_col is None:
            return None
        n = now or datetime.now(timezone.utc)
        floor = None
        if entry_at_ms is not None:
            try:
                floor = int(entry_at_ms)
            except (TypeError, ValueError):
                floor = None
        last = None
        for ts, close in zip(ts_col.tolist(), close_col.tolist()):
            try:
                ts_i = int(ts)
            except (TypeError, ValueError):
                continue
            if floor is not None and ts_i < floor:
                continue
            if is_closed_4h_bar(ts_i, n):
                last = (float(close), ts_i)
        return last
    except Exception:
        return None


def last_closed_4h_close(
    symbol, now, *, market=None, ohlcv=None, entry_at_ms=None
) -> float | None:
    """Last *closed* 4h close for ``symbol``. Missing data → None (fail-closed)."""
    bar = last_closed_4h_bar(
        symbol, now, market=market, ohlcv=ohlcv, entry_at_ms=entry_at_ms
    )
    return None if bar is None else bar[0]


def closed_bar_from_ohlcv(ohlcv, now) -> dict | None:
    """Build the closed-bar payload for ``_maybe_climax_fade_short``. None if incomplete."""
    if ohlcv is None:
        return None
    try:
        if hasattr(ohlcv, "empty") and ohlcv.empty:
            return None
        closes = [float(x) for x in ohlcv["close"].tolist()]
        volumes = [float(x) for x in ohlcv["volume"].tolist()]
        ts_list = [int(x) for x in ohlcv["ts"].tolist()]
    except Exception:
        return None
    if len(closes) < 20:
        return None
    n = now or datetime.now(timezone.utc)
    idx = None
    for i in range(len(ts_list) - 1, -1, -1):
        if is_closed_4h_bar(ts_list[i], n):
            idx = i
            break
    if idx is None or idx < 19:
        return None
    return {
        "ts_ms": ts_list[idx],
        "closes": closes[: idx + 1],
        "volumes": volumes[: idx + 1],
        "bar_index": idx,
    }


def _profit_factor(pnls) -> float | None:
    """Gross win / gross loss. No trades → None; wins and no losses → inf."""
    wins = sum(p for p in pnls if p > 0)
    losses = sum(-p for p in pnls if p < 0)
    if not pnls:
        return None
    if losses <= 0:
        return float("inf") if wins > 0 else None
    return wins / losses


def replay_climax_fade(
    closes,
    volumes,
    *,
    cfg=None,
    fee_bp_side: float = 7.5,
    funding_rate_8h: float = 0.0001,
    bar_hours: float = 4.0,
) -> dict[str, Any]:
    """Walk a synthetic series: one entry at signal close, cover on scan doors.

    Fill is the signal close (paper stand-in for next open). Fees in bp/side.
    """
    cfg = cfg or _SCAN_DEFAULTS
    n = len(closes)
    open_entry = None
    open_i = None
    covers: list[str] = []
    n_entries = 0
    pnl_pct = 0.0
    trade_pnls: list[float] = []
    for i in range(19, n):
        if open_entry is None:
            if climax_entry_signal(closes=closes, volumes=volumes, bar_index=i, cfg=cfg):
                open_entry = float(closes[i])
                open_i = i
                n_entries += 1
            continue
        lot = {
            "side": "short",
            "amount": 1.0,
            "average_entry": open_entry,
            "leverage": 2.0,
            "short_recipe": "climax_fade",
            "entry_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        age_h = (i - open_i) * float(bar_hours)
        opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
        from datetime import timedelta

        lot["entry_at"] = (opened).isoformat()
        now = opened + timedelta(hours=age_h)
        # Replay fills at signal close; subsequent bars opened after that fill.
        bar_open_ts_ms = int(opened.timestamp() * 1000) + int(
            (i - open_i) * float(bar_hours) * 3600 * 1000
        )
        hit = climax_cover_decision(
            lot,
            closed_4h_close=float(closes[i]),
            mark=float(closes[i]),
            now=now,
            cfg=cfg,
            bar_open_ts_ms=bar_open_ts_ms,
        )
        if not hit:
            continue
        covers.append(str(hit.get("source")))
        cover_px = float(closes[i])
        gross = (open_entry - cover_px) / open_entry
        fee = 2.0 * (float(fee_bp_side) / 10_000.0)
        funding = float(funding_rate_8h) * (age_h / 8.0)
        trade_pnl = gross - fee - funding
        pnl_pct += trade_pnl
        trade_pnls.append(trade_pnl)
        open_entry = None
        open_i = None
    return {
        "n_entries": n_entries,
        "n_covers": len(covers),
        "cover_sources": covers,
        "pnl_pct": pnl_pct,
        "pf": _profit_factor(trade_pnls),
    }
