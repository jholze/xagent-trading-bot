"""Cycle-scan stall: daily board must not refetch within its refresh window.

Spec: specs/cycle-scan-stall/04-requirements.md (Rev 1).
The slow path fetches every symbol again on a second scan in the same
refresh window, and opens a new Gate client per symbol.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import historical_prices as hp
from services.gainer_universe.scanner import (
    build_daily_history,
    reset_daily_bar_cache,
    run_scan,
)

_T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
_SYMBOLS = [f"S{i}/USDT" for i in range(40)]
_CFG = {
    "daily_history_days": 10,
    "scan_workers": 4,
    "min_day_ret_pct": 0,
    "daily_top_max": 40,
    "daily_refresh_sec": 900,
    "universe_top_by_volume": 40,
    "daily_min_volume": 1,
    "min_volume_usdt_24h": 1,
    "live_top_n": 5,
    "blacklist_suffixes": [],
    "expand_inject_max": 40,
    "prev_top_ttl_hours": 36,
    "enable_continuation": False,
    "live_heat_trade": False,
    "streak_min_days_in_top20": 2,
    "streak_lookback_days": 3,
}


def _bars(end: datetime) -> list:
    prev = int((end - timedelta(days=1)).timestamp() * 1000)
    today = int(end.timestamp() * 1000)
    return [
        [prev, 1.0, 1.0, 1.0, 1.0, 100.0],
        [today, 1.0, 1.1, 0.9, 1.2, 200.0],
    ]


def setup_function():
    reset_daily_bar_cache()
    hp.clear_cache()


def test_daily_history_fetches_once_per_refresh_window():
    """AC1. A second build inside daily_refresh_sec does not call the fetcher."""
    calls: list[str] = []

    def fetch(sym, start, end):
        calls.append(sym)
        return _bars(end)

    later = _T0 + timedelta(seconds=200)
    first = build_daily_history(_SYMBOLS, _CFG, fetch_ohlcv_fn=fetch, now=_T0)
    second = build_daily_history(_SYMBOLS, _CFG, fetch_ohlcv_fn=fetch, now=later)
    assert len(calls) == len(_SYMBOLS)
    assert set(calls) == set(_SYMBOLS)

    def ranked(board: dict) -> dict:
        return {
            day: sorted((row["symbol"], row["day_ret_pct"]) for row in rows)
            for day, rows in board.items()
        }

    assert ranked(second) == ranked(first)


def test_daily_history_refetches_after_refresh_window(monkeypatch):
    """AC1 boundary. Past daily_refresh_sec the board fetches again."""
    clock = {"now": 10_000.0}
    monkeypatch.setattr(
        "services.gainer_universe.scanner.time.monotonic",
        lambda: clock["now"],
    )
    calls: list[str] = []

    def fetch(sym, start, end):
        calls.append(sym)
        return _bars(end)

    build_daily_history(["AAA/USDT"], _CFG, fetch_ohlcv_fn=fetch, now=_T0)
    clock["now"] += 1_000  # > daily_refresh_sec 900
    later = _T0 + timedelta(seconds=1000)
    build_daily_history(["AAA/USDT"], _CFG, fetch_ohlcv_fn=fetch, now=later)
    assert calls == ["AAA/USDT", "AAA/USDT"]


def test_slow_scan_completion_stamp_suppresses_rescan(monkeypatch):
    """AC2. daily_scanned_at is the completion clock, not the pre-fetch now.

    A tenant that starts 1000s after the logical scan start, but only 200s
    after the fetch finished, must not download the board again.
    """
    finished = _T0 + timedelta(seconds=800)
    monkeypatch.setattr(
        "services.gainer_universe.scanner._clock",
        lambda: finished,
    )
    calls = {"n": 0}

    def fetch(sym, start, end):
        calls["n"] += 1
        return _bars(end)

    tickers = {s: {"last": 1, "quoteVolume": 1_000_000, "percentage": 5} for s in _SYMBOLS}
    snap = run_scan(
        _CFG,
        fetch_tickers_fn=lambda: tickers,
        fetch_ohlcv_fn=fetch,
        now=_T0,
    )
    assert snap["daily_scanned_at"] == finished.isoformat()
    # Drop the bar cache so only the stamp can suppress the second wave.
    reset_daily_bar_cache()
    calls["n"] = 0
    run_scan(
        _CFG,
        prev_state=snap,
        fetch_tickers_fn=lambda: tickers,
        fetch_ohlcv_fn=fetch,
        now=_T0 + timedelta(seconds=1000),
    )
    assert calls["n"] == 0


def test_ohlcv_reuses_one_gate_client(monkeypatch):
    """AC3. Parallel symbol fetches share one Gate client."""
    created: list[int] = []

    class Fake:
        def __init__(self, *args, **kwargs):
            created.append(1)

        def fetch_ohlcv(self, *args, **kwargs):
            since = int(kwargs.get("since") or args[2])
            return [[since, 1, 1, 1, 1, 1]]

    monkeypatch.setattr(hp.ccxt, "gate", Fake)
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=2)

    def one(i: int):
        return hp._fetch_ohlcv_range(f"S{i}/USDT", start, end, "1d")

    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in [pool.submit(one, i) for i in range(8)]:
            assert fut.result()
    assert len(created) == 1


def test_two_scan_benchmark_stays_on_fast_path():
    """AC4. Second scan in the refresh window is not another fetch wave.

    40 symbols, 50ms each, 4 workers. One wave is ~0.50s. The slow path
    runs that wave twice (~1.0s, second/first ≈ 1). The fix keeps the
    second wave under half the first.
    """
    calls = {"n": 0}

    def fetch(sym, start, end):
        calls["n"] += 1
        time.sleep(0.05)
        return _bars(end)

    started = time.perf_counter()
    build_daily_history(_SYMBOLS, _CFG, fetch_ohlcv_fn=fetch, now=_T0)
    mid = time.perf_counter()
    build_daily_history(
        _SYMBOLS,
        _CFG,
        fetch_ohlcv_fn=fetch,
        now=_T0 + timedelta(seconds=200),
    )
    ended = time.perf_counter()
    first = mid - started
    second = ended - mid
    assert calls["n"] == len(_SYMBOLS)
    assert first > 0.2
    assert second < first * 0.5
