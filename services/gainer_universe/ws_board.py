"""WS-backed gainer board (identify only — shadow logs, no orders).

REST seeds the watch set; Gate spot.tickers ticks update last/pct and rank.
Kill: gainer_universe.ws_board.enabled=false or mode=off.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from logger import LOG_DIR, log
from services.gainer_universe.config import gainer_universe_config
from services.gainer_universe.filters import normalize_symbol

_DEFAULT_WS_BOARD: dict[str, Any] = {
    "enabled": False,
    "mode": "shadow",  # shadow | off  (live identify later — still no auto-buy here)
    "max_watch": 40,
    "log_top_n": 15,
    "log_interval_sec": 30,
    "min_pct_to_rank": 5.0,
}


def ws_board_config(config: dict | None = None) -> dict[str, Any]:
    raw: dict = {}
    if isinstance(config, dict):
        if isinstance(config.get("gainer_universe"), dict):
            wb = config["gainer_universe"].get("ws_board")
            if isinstance(wb, dict):
                raw = wb
        elif isinstance(config.get("ws_board"), dict):
            # already the gainer section or explicit ws_board
            raw = config["ws_board"]
        else:
            g = gainer_universe_config(config)
            if isinstance(g.get("ws_board"), dict):
                raw = g["ws_board"]
    out = {**_DEFAULT_WS_BOARD, **(raw or {})}
    out["enabled"] = bool(out.get("enabled", False))
    mode = str(out.get("mode") or "shadow").strip().lower()
    if mode not in ("shadow", "off"):
        mode = "shadow"
    out["mode"] = mode
    out["max_watch"] = max(5, int(out.get("max_watch") or 40))
    out["log_top_n"] = max(3, int(out.get("log_top_n") or 15))
    out["log_interval_sec"] = max(5.0, float(out.get("log_interval_sec") or 30))
    out["min_pct_to_rank"] = float(out.get("min_pct_to_rank") or 5.0)
    return out


def ws_board_enabled(config: dict | None = None) -> bool:
    cfg = ws_board_config(config)
    return bool(cfg.get("enabled")) and cfg.get("mode") != "off"


def watch_symbols_from_gainer_state(
    state: dict | None,
    cfg: dict | None = None,
) -> list[str]:
    """Prefer live_top then eligible — capped for WS subscribe budget."""
    wb = ws_board_config(cfg)
    cap = int(wb.get("max_watch") or 40)
    state = state or {}
    seen: set[str] = set()
    out: list[str] = []
    for row in list(state.get("live_top") or []) + list(state.get("eligible") or []):
        if not isinstance(row, dict):
            continue
        sym = normalize_symbol(row.get("symbol") or "")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= cap:
            break
    return out


class WsBoardState:
    """Thread-safe in-memory board updated from ticker ticks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ticks: dict[str, dict[str, Any]] = {}
        self._last_log_mono: float = 0.0
        self._stats = {
            "ticks": 0,
            "ranked": 0,
            "logs": 0,
            "last_board_at": 0.0,
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def on_tick(
        self,
        symbol: str,
        *,
        last: float,
        pct_24h: float | None = None,
        quote_volume: float | None = None,
    ) -> None:
        sym = normalize_symbol(symbol)
        if not sym or last <= 0:
            return
        now = time.time()
        with self._lock:
            prev = self._ticks.get(sym) or {}
            row = {
                "symbol": sym,
                "last": float(last),
                "pct_24h": (
                    float(pct_24h)
                    if pct_24h is not None
                    else float(prev.get("pct_24h") or 0.0)
                ),
                "quote_volume": (
                    float(quote_volume)
                    if quote_volume is not None
                    else float(prev.get("quote_volume") or 0.0)
                ),
                "ts": now,
                "tick_n": int(prev.get("tick_n") or 0) + 1,
            }
            # sticky: first time above min band
            min_pct = 5.0  # refined at rank time from config
            if row["pct_24h"] >= min_pct and not prev.get("first_hot_ts"):
                row["first_hot_ts"] = now
            else:
                row["first_hot_ts"] = prev.get("first_hot_ts")
            self._ticks[sym] = row
            self._stats["ticks"] += 1

    def ranked_board(
        self,
        *,
        top_n: int = 15,
        min_pct: float = 5.0,
        max_age_sec: float = 120.0,
    ) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            rows = []
            for sym, r in self._ticks.items():
                age = now - float(r.get("ts") or 0)
                if age > max_age_sec:
                    continue
                pct = float(r.get("pct_24h") or 0)
                if pct < min_pct:
                    continue
                rows.append(dict(r))
            rows.sort(
                key=lambda x: (float(x.get("pct_24h") or 0), float(x.get("quote_volume") or 0)),
                reverse=True,
            )
            out = []
            for i, r in enumerate(rows[: max(1, top_n)], 1):
                r["rank"] = i
                r["age_sec"] = round(now - float(r.get("ts") or now), 2)
                out.append(r)
            self._stats["ranked"] = len(out)
            self._stats["last_board_at"] = now
            return out

    def maybe_log_board(self, config: dict | None = None) -> list[dict[str, Any]] | None:
        """Throttle-log top board to jsonl. Returns board if logged else None."""
        if not ws_board_enabled(config):
            return None
        cfg = ws_board_config(config)
        interval = float(cfg.get("log_interval_sec") or 30)
        now_m = time.monotonic()
        with self._lock:
            if self._last_log_mono and (now_m - self._last_log_mono) < interval:
                return None
            self._last_log_mono = now_m
        board = self.ranked_board(
            top_n=int(cfg.get("log_top_n") or 15),
            min_pct=float(cfg.get("min_pct_to_rank") or 5.0),
        )
        event = {
            "type": "gainer_ws_board",
            "mode": cfg.get("mode"),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "n": len(board),
            "top": [
                {
                    "rank": r.get("rank"),
                    "symbol": r.get("symbol"),
                    "pct_24h": r.get("pct_24h"),
                    "last": r.get("last"),
                    "tick_n": r.get("tick_n"),
                    "age_sec": r.get("age_sec"),
                }
                for r in board
            ],
            "stats": self.stats(),
        }
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, "gainer_ws_board.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            with self._lock:
                self._stats["logs"] += 1
            if board:
                top3 = ", ".join(
                    f"{r['symbol']} {float(r.get('pct_24h') or 0):+.1f}%"
                    for r in board[:3]
                )
                log(f"gainer_ws_board shadow top={len(board)} {top3}", "INFO")
        except Exception as e:
            log(f"gainer_ws_board log failed: {e}", "DEBUG")
        return board


_board: WsBoardState | None = None
_board_lock = threading.Lock()


def get_ws_board() -> WsBoardState:
    global _board
    with _board_lock:
        if _board is None:
            _board = WsBoardState()
        return _board


def reset_ws_board() -> None:
    """Test helper."""
    global _board
    with _board_lock:
        _board = WsBoardState()
