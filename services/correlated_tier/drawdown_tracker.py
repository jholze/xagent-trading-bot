"""Rolling-window group drawdown detector (pure; no network).

Uses window-high (not all-time high) so slow multi-hour bleeds never trip
and recovery is automatic as the old high ages out of the sample window.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque


def _norm_symbol(symbol: str | None) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    return s


def compute_drawdown_pct(
    samples: list[tuple[float, float]],
    now: float,
    window_sec: float,
) -> float | None:
    """Return drawdown % from rolling window-high to latest price, or None.

    samples: list of (ts, price). Only samples in (now - window_sec, now] count.
    Drawdown = (window_high - last) / window_high * 100.
    """
    if not samples or window_sec <= 0:
        return None
    cut = float(now) - float(window_sec)
    window = [(ts, px) for ts, px in samples if ts > cut and px > 0]
    if not window:
        return None
    # prefer samples at or before now
    window = [(ts, px) for ts, px in window if ts <= float(now) + 1e-6]
    if not window:
        return None
    last_px = window[-1][1]
    high = max(px for _, px in window)
    if high <= 0 or last_px <= 0:
        return None
    return (1.0 - last_px / high) * 100.0


class GroupDrawdownTracker:
    """Per-group ring of proxy price samples + evaluate(active / confirming)."""

    def __init__(
        self,
        group_name: str,
        proxy_symbols: list[str],
        *,
        drawdown_pct: float = 5.0,
        window_sec: float = 600.0,
        min_confirming: int = 1,
        sample_keep_sec: float | None = None,
    ) -> None:
        self.group_name = str(group_name)
        self.proxy_symbols = [_norm_symbol(s) for s in (proxy_symbols or []) if s]
        self.proxy_set = set(self.proxy_symbols)
        self.drawdown_pct = float(drawdown_pct)
        self.window_sec = float(window_sec)
        self.min_confirming = max(1, int(min_confirming))
        # keep a bit more than window so edges are stable
        self.sample_keep_sec = float(
            sample_keep_sec if sample_keep_sec is not None else max(window_sec * 2, window_sec + 60)
        )
        self._hist: dict[str, Deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=4000)
        )

    def on_tick(self, symbol: str, price: float, *, now: float | None = None) -> None:
        sym = _norm_symbol(symbol)
        if sym not in self.proxy_set:
            return
        try:
            px = float(price)
        except (TypeError, ValueError):
            return
        if px <= 0:
            return
        now = float(now if now is not None else time.time())
        dq = self._hist[sym]
        if dq and abs(dq[-1][0] - now) < 0.5:
            dq[-1] = (now, px)
        else:
            dq.append((now, px))
        cut = now - self.sample_keep_sec
        while dq and dq[0][0] < cut:
            dq.popleft()

    def evaluate(self, *, now: float | None = None) -> dict:
        now = float(now if now is not None else time.time())
        per_symbol: dict[str, bool] = {}
        confirming = 0
        for sym in self.proxy_symbols:
            hist = list(self._hist.get(sym) or ())
            dd = compute_drawdown_pct(hist, now, self.window_sec)
            fired = dd is not None and dd >= self.drawdown_pct
            per_symbol[sym] = fired
            if fired:
                confirming += 1
        active = confirming >= self.min_confirming
        return {
            "group": self.group_name,
            "per_symbol": per_symbol,
            "active": active,
            "confirming": confirming,
            "min_confirming": self.min_confirming,
            "drawdown_pct": self.drawdown_pct,
            "window_sec": self.window_sec,
            "updated_at": now,
        }
