"""WS/REST ticker-based RelVol ignition (no bulk OHLCV scan).

Samples Gate 24h quote_volume over time. Approximates last-hour volume as
  qvol_1h ≈ max(0, qv_24h(t) − qv_24h(t − 1h))
Baseline = median of prior 1h-slices; fire when qvol_1h >= mult × baseline
and price green vs last sample open proxy (last > prev last).

This rides the existing gainer_signal REST seed + spot.tickers WS path.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from statistics import median
from typing import Any, Deque

from services.gainer_signal.pure import (
    is_leverage_symbol,
    normalize_symbol,
    parse_pct_24h,
    parse_quote_vol,
)


def _parse_last(t: dict[str, Any] | None) -> float:
    if not isinstance(t, dict):
        return 0.0
    try:
        return float(t.get("last") or 0)
    except (TypeError, ValueError):
        return 0.0


class RelvolTracker:
    """Per-symbol ring of (ts, quote_vol_24h, last)."""

    def __init__(
        self,
        *,
        mult: float = 10.0,
        baseline_hours: float = 12.0,
        sample_keep_hours: float = 14.0,
        min_ign_qvol: float = 5_000.0,
        baseline_floor: float = 100.0,
        cooldown_sec: float = 8 * 3600,
        min_samples_for_1h: int = 2,
    ) -> None:
        self.mult = float(mult)
        self.baseline_hours = float(baseline_hours)
        self.sample_keep_sec = float(sample_keep_hours) * 3600
        self.min_ign_qvol = float(min_ign_qvol)
        self.baseline_floor = float(baseline_floor)
        self.cooldown_sec = float(cooldown_sec)
        self.min_samples_for_1h = int(min_samples_for_1h)
        self._hist: dict[str, Deque[tuple[float, float, float]]] = defaultdict(
            lambda: deque(maxlen=2000)
        )
        self._last_fire: dict[str, float] = {}

    def sample_tickers(self, tickers: dict[str, Any], *, now: float | None = None) -> int:
        """Ingest a full or partial ticker book. Returns n symbols updated."""
        now = now or time.time()
        n = 0
        for raw, t in (tickers or {}).items():
            if not isinstance(t, dict):
                continue
            sym = normalize_symbol(raw) or normalize_symbol(
                str((t.get("symbol") or raw))
            )
            if not sym or not sym.endswith("/USDT"):
                continue
            if is_leverage_symbol(sym):
                continue
            qv = parse_quote_vol(t)
            last = _parse_last(t)
            if qv <= 0 and last <= 0:
                continue
            dq = self._hist[sym]
            # de-dupe if same second
            if dq and abs(dq[-1][0] - now) < 1.0:
                dq[-1] = (now, qv, last)
            else:
                dq.append((now, qv, last))
            # prune
            cut = now - self.sample_keep_sec
            while dq and dq[0][0] < cut:
                dq.popleft()
            n += 1
        return n

    def _qvol_1h_at(self, hist: Deque[tuple[float, float, float]], t_idx: int) -> float | None:
        """Approx 1h quote volume ending at hist[t_idx] via 24h rolling delta."""
        if t_idx < 0 or t_idx >= len(hist):
            return None
        ts, qv, _ = hist[t_idx]
        target = ts - 3600.0
        # find sample closest at or before target
        prev_qv = None
        for i in range(t_idx, -1, -1):
            if hist[i][0] <= target + 30:  # 30s slack
                prev_qv = hist[i][1]
                break
        if prev_qv is None:
            if len(hist) < self.min_samples_for_1h:
                return None
            # not enough history for full hour — use growth from oldest in window
            prev_qv = hist[0][1]
            # scale if window < 1h
            dt = max(60.0, ts - hist[0][0])
            raw = max(0.0, qv - prev_qv)
            return raw * (3600.0 / dt) if dt < 3600 else raw
        return max(0.0, qv - prev_qv)

    def evaluate(
        self, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Return new ignition signals (cooldown applied)."""
        now = now or time.time()
        out: list[dict[str, Any]] = []
        win_sec = self.baseline_hours * 3600
        for sym, hist in list(self._hist.items()):
            if len(hist) < self.min_samples_for_1h + 2:
                continue
            last_fire = self._last_fire.get(sym, 0)
            if now - last_fire < self.cooldown_sec:
                continue
            # current 1h vol
            q1 = self._qvol_1h_at(hist, len(hist) - 1)
            if q1 is None or q1 < self.min_ign_qvol:
                continue
            # baseline: median of 1h vols at samples spaced ~1h back
            baselines: list[float] = []
            # walk back up to baseline_hours using points near each hour
            ts_end = hist[-1][0]
            for h in range(1, int(self.baseline_hours) + 1):
                target_ts = ts_end - h * 3600
                # find index nearest target_ts
                best_i = None
                best_d = 1e18
                for i, (ts, _, _) in enumerate(hist):
                    d = abs(ts - target_ts)
                    if d < best_d:
                        best_d = d
                        best_i = i
                if best_i is None or best_d > 900:  # >15 min off
                    continue
                b = self._qvol_1h_at(hist, best_i)
                if b is not None and b >= 0:
                    baselines.append(b)
            if len(baselines) < 3:
                continue
            base = max(median(baselines), self.baseline_floor)
            if q1 < self.mult * base:
                continue
            # green proxy: last price > price ~1h ago
            last_px = hist[-1][2]
            px_1h = hist[0][2]
            for i in range(len(hist) - 1, -1, -1):
                if hist[i][0] <= hist[-1][0] - 3600 + 60:
                    px_1h = hist[i][2]
                    break
            if last_px <= 0 or last_px < px_1h * 0.998:  # allow flat-ish green
                # require non-red: last >= price 1h ago * 0.998
                if last_px < px_1h:
                    continue
            factor = q1 / base if base > 0 else 0.0
            abs24 = float(hist[-1][1] or 0)
            self._last_fire[sym] = now
            out.append(
                {
                    "symbol": sym,
                    "source": "gainer_relvol",
                    "trigger": "relvol_ws",
                    "entry_source": "gainer_relvol",
                    "last": last_px,
                    "price": last_px,
                    "pct_24h": 0.0,
                    "quote_vol": abs24,
                    "quote_vol_24h": abs24,
                    "qvol": round(q1, 2),
                    "qvol_1h": round(q1, 2),
                    "baseline": round(base, 2),
                    "factor": round(factor, 2),
                    "eligible": True,
                    "reject_reason": None,
                    "leverage": False,
                    "rank": 0,
                    "variant": "ws_qv24_delta_1h",
                    "would_pass_prod_min_vol": abs24 >= 500_000,
                    "abs_vol_24h_est": round(abs24, 2),
                }
            )
        return out
