"""In-memory leaders board for gainer-signal service."""

from __future__ import annotations

import threading
import time
from typing import Any

from services.gainer_signal.pure import (
    DEFAULT_ELIGIBLE_MIN_VOL,
    DEFAULT_RECOGNIZE_TOP_N,
    rank_leaders_from_tickers,
    select_entry_signals,
)


class LeadersBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leaders: list[dict[str, Any]] = []
        self._by_symbol: dict[str, dict[str, Any]] = {}
        self._last_board_at: float = 0.0
        self._stats = {
            "ticks": 0,
            "rest_seeds": 0,
            "signals_emitted": 0,
            "signals_pushed_ok": 0,
            "signals_push_fail": 0,
            "connected": False,
            "n_subscribed": 0,
            "reconnects": 0,
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n_rec = len(self._leaders)
            n_elig = sum(1 for r in self._leaders if r.get("eligible"))
            return {
                **self._stats,
                "n_recognized": n_rec,
                "n_eligible": n_elig,
                "last_board_at": self._last_board_at,
            }

    def set_connected(self, ok: bool) -> None:
        with self._lock:
            self._stats["connected"] = bool(ok)

    def set_subscribed(self, n: int) -> None:
        with self._lock:
            self._stats["n_subscribed"] = int(n)

    def bump_reconnect(self) -> None:
        with self._lock:
            self._stats["reconnects"] += 1

    def bump_tick(self) -> None:
        with self._lock:
            self._stats["ticks"] += 1

    def apply_tickers(
        self,
        tickers: dict[str, Any],
        *,
        top_n: int = DEFAULT_RECOGNIZE_TOP_N,
        min_vol: float = DEFAULT_ELIGIBLE_MIN_VOL,
        from_rest: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        leaders = rank_leaders_from_tickers(
            tickers, top_n=top_n, min_vol_eligible=min_vol
        )
        with self._lock:
            prev = dict(self._by_symbol)
            self._prev_board = prev
            self._leaders = leaders
            self._by_symbol = {r["symbol"]: r for r in leaders}
            self._last_board_at = time.time()
            if from_rest:
                self._stats["rest_seeds"] += 1
        return leaders, prev

    def leaders(self, *, eligible_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._leaders)
        if eligible_only:
            return [r for r in rows if r.get("eligible")]
        return rows

    def select_signals(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            leaders = list(self._leaders)
            prev = dict(getattr(self, "_prev_board", {}) or {})
        return select_entry_signals(leaders, prev_board=prev, **kwargs)

    def note_prev_after_signals(self) -> None:
        with self._lock:
            self._prev_board = dict(self._by_symbol)

    def record_signal_emit(self, n: int = 1) -> None:
        with self._lock:
            self._stats["signals_emitted"] += int(n)

    def record_push(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._stats["signals_pushed_ok"] += 1
            else:
                self._stats["signals_push_fail"] += 1


_BOARD: LeadersBoard | None = None
_BOARD_LOCK = threading.Lock()


def get_board() -> LeadersBoard:
    global _BOARD
    with _BOARD_LOCK:
        if _BOARD is None:
            _BOARD = LeadersBoard()
        return _BOARD


def reset_board() -> LeadersBoard:
    global _BOARD
    with _BOARD_LOCK:
        _BOARD = LeadersBoard()
        return _BOARD
