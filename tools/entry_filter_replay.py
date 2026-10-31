"""Offline replay of pre-registered entry filters. No trading side effects.

The parameters below were fixed before any forward return was computed.
Do not retune them to this sample. The hold-out in
``docs/domain/hypothesis-protocol.md`` starts 2026-11-06, so a run on the
attached tape can kill a filter and cannot prove one.

Costs follow ``tools.hypothesis_protocol`` (taker 0.2% per side, class
slippage, stress and hard stress). Fills use the next 1h open after the
signal (one closed bar of delay). Daily features use the last daily bar
that was already closed at the signal. Spot funding does not apply.

Candle cache is public Gate data (Binance only as a fallback). No API keys.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from core.stablecoins import is_stablecoin_symbol
from tools.hypothesis_protocol import (
    TAKER_FEE_PCT_PER_SIDE,
    expected_slippage_pct,
    validate_payload,
)

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

# --- pre-registered design (do not fit these to the tape) ---

SMA_PRIMARY = 50
SMA_GRID = (38, 63)  # ±25% of 50, the ledger's E1 lengths
DISTANCE_X_PCT = 20.0
DISTANCE_Y_PCT = 20.0
TOP_N_PRIMARY = 100
TOP_N_GRID = (75, 125)  # ±25% of 100
LISTING_AGE_PRIMARY = (30.0, 90.0)
TARGET_DAILY_VOL = 0.05
TRIAL_COUNT_N = 31
HOLDOUT_ENTRIES = 0  # hold-out starts 2026-11-06; this tape has none
REGIME_BASE = "BTC"
HIT_DRAWDOWN = 0.20
BAR_1H = 3600
BAR_1D = 86400

PROTOCOL_START = "2026-07-25"
PROTOCOL_END = "2026-10-23"
OWNER_START = "2026-10-03"
OWNER_END = "2026-10-31"
WINDOWS = (
    ("W1", "2026-07-29", "2026-08-26"),
    ("W2", "2026-08-27", "2026-09-23"),
    ("W3", "2026-09-24", "2026-10-23"),
)
HALVES = (
    ("A", "2026-07-25", "2026-09-08"),
    ("B", "2026-09-09", "2026-10-23"),
)

# Daily history from this UTC day. Coins whose first candle is this open
# are censored (listed on or before the window). 2026-04-01 is 119 days
# before the first fill day 2026-07-29, which is above the listing grid
# (112.5). Young listings inside the window are not censored.
DAILY_FROM = datetime(2026, 4, 1, tzinfo=UTC)
HOURLY_FROM = datetime(2026, 6, 20, tzinfo=UTC)
CANDLE_UNTIL = datetime(2026, 11, 8, tzinfo=UTC)

GATE_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
BINANCE_URL = "https://api.binance.com/api/v3/klines"
USER_AGENT = "xagent-entry-filter-replay/1 (paper research; public market data)"

# Candle: open_ts, open, high, low, close, quote_volume
Candle = tuple[int, float, float, float, float, float]
EULER = 0.5772156649015329


@dataclass(frozen=True)
class TrialSpec:
    trial_id: str
    kind: str
    group: str
    rank: bool
    sma: int | None = None
    x_pct: float | None = None
    y_pct: float | None = None
    top_n: int | None = None
    min_age_days: float | None = None
    cost_tier: str = "base"
    delay_bars: int = 1


@dataclass(frozen=True)
class RawEntry:
    entry_id: str
    symbol: str
    signal_ts: datetime
    day_berlin: str
    source: str
    status: str
    code: str
    tenant: str
    dataset: str
    signal_price: float | None
    usdt: float | None
    csv_fwd24: float | None
    csv_fwd72: float | None
    is_add: bool


def trial_specs() -> list[TrialSpec]:
    """The 31 trials counted in N. Rank flags are the only recommendation set."""
    specs: list[TrialSpec] = []

    def add(spec: TrialSpec) -> None:
        specs.append(spec)

    add(TrialSpec("trend-sma50", "trend", "trend", True, sma=SMA_PRIMARY))
    add(TrialSpec("trend-sma38", "trend", "trend", False, sma=SMA_GRID[0]))
    add(TrialSpec("trend-sma63", "trend", "trend", False, sma=SMA_GRID[1]))
    add(
        TrialSpec(
            "distance-x20-y20",
            "distance",
            "distance",
            True,
            x_pct=DISTANCE_X_PCT,
            y_pct=DISTANCE_Y_PCT,
        )
    )
    add(TrialSpec("distance-x15-y20", "distance", "distance", False, x_pct=15.0, y_pct=20.0))
    add(TrialSpec("distance-x25-y20", "distance", "distance", False, x_pct=25.0, y_pct=20.0))
    add(TrialSpec("distance-x20-y15", "distance", "distance", False, x_pct=20.0, y_pct=15.0))
    add(TrialSpec("distance-x20-y25", "distance", "distance", False, x_pct=20.0, y_pct=25.0))
    add(TrialSpec("btc-sma50", "btc", "btc", True, sma=SMA_PRIMARY))
    add(TrialSpec("btc-sma38", "btc", "btc", False, sma=SMA_GRID[0]))
    add(TrialSpec("btc-sma63", "btc", "btc", False, sma=SMA_GRID[1]))
    add(TrialSpec("trend50-and-btc50", "trend_btc", "trend-btc", True))
    add(TrialSpec("trend50-and-distance20", "trend_distance", "trend-distance", True))
    add(TrialSpec("smallcap-top100", "smallcap", "smallcap", True, top_n=TOP_N_PRIMARY))
    add(TrialSpec("smallcap-top75", "smallcap", "smallcap", False, top_n=TOP_N_GRID[0]))
    add(TrialSpec("smallcap-top125", "smallcap", "smallcap", False, top_n=TOP_N_GRID[1]))
    add(TrialSpec("listing-age-30", "listing", "listing-30", True, min_age_days=30.0))
    add(TrialSpec("listing-age-22_5", "listing", "listing-30", False, min_age_days=22.5))
    add(TrialSpec("listing-age-37_5", "listing", "listing-30", False, min_age_days=37.5))
    add(TrialSpec("listing-age-90", "listing", "listing-90", True, min_age_days=90.0))
    add(TrialSpec("listing-age-67_5", "listing", "listing-90", False, min_age_days=67.5))
    add(TrialSpec("listing-age-112_5", "listing", "listing-90", False, min_age_days=112.5))
    for kind, trial, sma, x_pct, y_pct in (
        ("trend", "trend-sma50", SMA_PRIMARY, None, None),
        ("distance", "distance-x20-y20", None, DISTANCE_X_PCT, DISTANCE_Y_PCT),
        ("btc", "btc-sma50", SMA_PRIMARY, None, None),
    ):
        add(
            TrialSpec(
                f"stress-{trial}",
                kind,
                f"stress-{kind}",
                False,
                sma=sma,
                x_pct=x_pct,
                y_pct=y_pct,
                cost_tier="stress",
            )
        )
    for kind, trial, sma, x_pct, y_pct in (
        ("trend", "trend-sma50", SMA_PRIMARY, None, None),
        ("distance", "distance-x20-y20", None, DISTANCE_X_PCT, DISTANCE_Y_PCT),
        ("btc", "btc-sma50", SMA_PRIMARY, None, None),
    ):
        add(
            TrialSpec(
                f"hard-{trial}",
                kind,
                f"hard-{kind}",
                False,
                sma=sma,
                x_pct=x_pct,
                y_pct=y_pct,
                cost_tier="hard_stress",
            )
        )
    for kind, trial, sma, x_pct, y_pct in (
        ("trend", "trend-sma50", SMA_PRIMARY, None, None),
        ("distance", "distance-x20-y20", None, DISTANCE_X_PCT, DISTANCE_Y_PCT),
        ("btc", "btc-sma50", SMA_PRIMARY, None, None),
    ):
        add(
            TrialSpec(
                f"delay2-{trial}",
                kind,
                f"delay2-{kind}",
                False,
                sma=sma,
                x_pct=x_pct,
                y_pct=y_pct,
                delay_bars=2,
            )
        )
    if len(specs) != TRIAL_COUNT_N:
        raise RuntimeError(f"trial count {len(specs)} != {TRIAL_COUNT_N}")
    return specs


def parse_berlin(text: str) -> datetime:
    """Naive timestamps in this export are Europe/Berlin."""
    raw = datetime.fromisoformat(text.strip())
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=BERLIN)
    return raw.astimezone(UTC)


def berlin_day(moment: datetime) -> str:
    return moment.astimezone(BERLIN).date().isoformat()


def day_start_berlin(day: str) -> datetime:
    parsed = date.fromisoformat(day)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=BERLIN).astimezone(UTC)


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def is_add(source: str, signal: str) -> bool:
    return source == "dca" or signal == "BUY_DCA"


def gate_pair(symbol: str) -> str:
    text = symbol.strip().upper().replace("-", "/")
    if "/" in text:
        base, quote = text.split("/", 1)
        return f"{base}_{quote}"
    return text.replace("/", "_")


def symbol_from_pair(pair: str) -> str:
    base, _, quote = pair.partition("_")
    return f"{base}/{quote}" if quote else pair


def load_decisions(path: Path) -> list[RawEntry]:
    rows: list[RawEntry] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            day = (record.get("day") or "").strip()
            symbol = (record.get("symbol") or "").strip().upper()
            if not day or not symbol:
                continue
            source = (record.get("source") or "").strip()
            code = (record.get("code") or "").strip()
            tenant = (record.get("tenant") or "").strip()
            signal = (record.get("signal") or "").strip()
            rows.append(
                RawEntry(
                    entry_id=f"{tenant}|{day}|{symbol}|{source}|{code}",
                    symbol=symbol,
                    signal_ts=day_start_berlin(day),
                    day_berlin=day,
                    source=source,
                    status=(record.get("status") or "").strip(),
                    code=code,
                    tenant=tenant,
                    dataset="decisions",
                    signal_price=_float_or_none(record.get("price")),
                    usdt=None,
                    csv_fwd24=_float_or_none(record.get("fwd24h_pct")),
                    csv_fwd72=_float_or_none(record.get("fwd72h_pct")),
                    is_add=is_add(source, signal),
                )
            )
    return rows


def load_fills(path: Path) -> list[RawEntry]:
    rows: list[RawEntry] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if str(record.get("side") or "").lower() != "buy":
                continue
            if str(record.get("status") or "").lower() != "filled":
                continue
            symbol = str(record.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            stamps = record.get("timestamps") or {}
            raw_ts = stamps.get("created") or record.get("ts_event")
            if not raw_ts:
                continue
            signal_ts = parse_berlin(str(raw_ts))
            source = str(record.get("source") or "")
            signal = str(record.get("signal") or "")
            execution = record.get("execution") or {}
            request = record.get("request") or {}
            price = _float_or_none(execution.get("price"))
            if price is None:
                price = _float_or_none(request.get("price"))
            usdt = _float_or_none(execution.get("usdt"))
            if usdt is None:
                usdt = _float_or_none(request.get("usdt"))
            order_id = str(record.get("id") or record.get("_id") or len(rows))
            rows.append(
                RawEntry(
                    entry_id=order_id,
                    symbol=symbol,
                    signal_ts=signal_ts,
                    day_berlin=berlin_day(signal_ts),
                    source=source,
                    status="filled",
                    code="FILLED",
                    tenant=str(record.get("tenant_id") or ""),
                    dataset="fills",
                    signal_price=price,
                    usdt=usdt,
                    csv_fwd24=None,
                    csv_fwd72=None,
                    is_add=is_add(source, signal),
                )
            )
    return rows


def entry_universe(raw: list[RawEntry]) -> list[RawEntry]:
    """Non-add buys. Fills collapse to one row per symbol and signal hour."""
    decisions_by_key: dict[tuple[str, str, str, str], RawEntry] = {}
    fills: list[RawEntry] = []
    for row in raw:
        if row.is_add:
            continue
        if row.dataset == "decisions":
            key = (row.tenant, row.symbol, row.day_berlin, row.source)
            previous = decisions_by_key.get(key)
            if previous is None or (previous.status != "filled" and row.status == "filled"):
                decisions_by_key[key] = row
            continue
        fills.append(row)
    decisions = list(decisions_by_key.values())
    best: dict[tuple[str, int], RawEntry] = {}
    for row in fills:
        hour = int(row.signal_ts.timestamp()) // BAR_1H
        key = (row.symbol, hour)
        previous = best.get(key)
        if previous is None or row.signal_ts < previous.signal_ts:
            best[key] = row
    return decisions + list(best.values())


def parse_gate_candle(row: object) -> Candle | None:
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    try:
        open_ts = int(float(row[0]))
        quote = float(row[1])
        close = float(row[2])
        high = float(row[3])
        low = float(row[4])
        opened = float(row[5])
    except (TypeError, ValueError):
        return None
    if open_ts <= 0 or opened <= 0 or close <= 0:
        return None
    return (open_ts, opened, high, low, close, quote)


def parse_binance_kline(row: object) -> Candle | None:
    if not isinstance(row, (list, tuple)) or len(row) < 8:
        return None
    try:
        open_ts = int(float(row[0])) // 1000
        opened = float(row[1])
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        quote = float(row[7])
    except (TypeError, ValueError):
        return None
    if open_ts <= 0 or opened <= 0 or close <= 0:
        return None
    return (open_ts, opened, high, low, close, quote)


class MissingPair(Exception):
    pass


class ExchangeBanned(Exception):
    pass


class PublicClient:
    """Gate public candlesticks, Binance spot as fallback. No credentials."""

    def __init__(self, per_second: float = 6.0, timeout: float = 30.0):
        self.gap = 1.0 / per_second if per_second > 0 else 0.0
        self.timeout = timeout
        self._lock = threading.Lock()
        self._next = 0.0
        self.binance_enabled = True

    def _wait(self) -> None:
        delay = 0.0
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                delay = self._next - now
                self._next += self.gap
            else:
                self._next = now + self.gap
        if delay:
            time.sleep(delay)

    def _get_json(self, url: str) -> object:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        last_error: Exception | None = None
        for attempt in range(4):
            self._wait()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 400:
                    raise MissingPair(url) from exc
                if exc.code == 451:
                    raise ExchangeBanned(url) from exc
                last_error = exc
                if exc.code in (418, 429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise
        raise RuntimeError(f"fetch failed: {last_error}")

    def fetch_range(self, pair: str, interval: str, start: int, end: int) -> list[Candle]:
        step = BAR_1H if interval == "1h" else BAR_1D
        try:
            candles = self._page_gate(pair, interval, start, end, step)
        except MissingPair:
            candles = []
        if candles or not self.binance_enabled:
            return _dedupe_candles(candles)
        try:
            candles = self._page_binance(pair, interval, start, end, step)
        except MissingPair:
            candles = []
        except ExchangeBanned:
            self.binance_enabled = False
            candles = []
        return _dedupe_candles(candles)

    def _page_gate(self, pair: str, interval: str, start: int, end: int, step: int) -> list[Candle]:
        cursor = start
        found: list[Candle] = []
        while cursor < end:
            params = urllib.parse.urlencode(
                {"currency_pair": pair, "interval": interval, "from": str(cursor), "limit": "1000"}
            )
            payload = self._get_json(f"{GATE_URL}?{params}")
            if not isinstance(payload, list):
                break
            parsed = [item for item in (parse_gate_candle(row) for row in payload) if item is not None]
            page = [item for item in parsed if cursor <= item[0] < end]
            if not page:
                if not parsed:
                    cursor += 1000 * step
                    continue
                last = max(item[0] for item in parsed)
                nxt = max(cursor + step, last + step)
                if nxt <= cursor:
                    break
                cursor = nxt
                continue
            found.extend(page)
            last = max(item[0] for item in page)
            nxt = last + step
            if nxt <= cursor:
                break
            cursor = nxt
        return found

    def _page_binance(self, pair: str, interval: str, start: int, end: int, step: int) -> list[Candle]:
        symbol = pair.replace("_", "")
        cursor = start
        found: list[Candle] = []
        while cursor < end:
            params = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": str(cursor * 1000),
                    "endTime": str(end * 1000),
                    "limit": "1000",
                }
            )
            payload = self._get_json(f"{BINANCE_URL}?{params}")
            if not isinstance(payload, list) or not payload:
                break
            page = [item for item in (parse_binance_kline(row) for row in payload) if item is not None]
            if not page:
                break
            found.extend(page)
            last = max(item[0] for item in page)
            nxt = last + step
            if nxt <= cursor:
                break
            cursor = nxt
        return found


def _dedupe_candles(candles: list[Candle]) -> list[Candle]:
    by_ts: dict[int, Candle] = {}
    for candle in candles:
        by_ts[candle[0]] = candle
    return [by_ts[key] for key in sorted(by_ts)]


class CandleStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                pair TEXT NOT NULL,
                interval TEXT NOT NULL,
                open_ts INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                quote_volume REAL NOT NULL,
                PRIMARY KEY (pair, interval, open_ts)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coverage (
                pair TEXT NOT NULL,
                interval TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                n INTEGER NOT NULL,
                PRIMARY KEY (pair, interval, start_ts, end_ts)
            )
            """
        )
        self.conn.commit()

    def covered(self, pair: str, interval: str, start: int, end: int) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM coverage
            WHERE pair = ? AND interval = ? AND start_ts <= ? AND end_ts >= ?
            """,
            (pair, interval, start, end),
        ).fetchone()
        return row is not None

    def save(self, pair: str, interval: str, start: int, end: int, candles: list[Candle]) -> None:
        self.conn.executemany(
            """
            INSERT INTO candles (pair, interval, open_ts, open, high, low, close, quote_volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair, interval, open_ts) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                quote_volume = excluded.quote_volume
            """,
            [(pair, interval, *candle) for candle in candles],
        )
        self.conn.execute(
            """
            INSERT INTO coverage (pair, interval, start_ts, end_ts, n)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pair, interval, start_ts, end_ts) DO UPDATE SET n = excluded.n
            """,
            (pair, interval, start, end, len(candles)),
        )
        self.conn.commit()

    def load(self, pair: str, interval: str) -> list[Candle]:
        rows = self.conn.execute(
            """
            SELECT open_ts, open, high, low, close, quote_volume
            FROM candles WHERE pair = ? AND interval = ? ORDER BY open_ts
            """,
            (pair, interval),
        ).fetchall()
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    def close(self) -> None:
        self.conn.close()


def ensure_candles(
    store: CandleStore,
    pairs: list[str],
    *,
    network: bool,
    workers: int = 6,
    per_second: float = 6.0,
    client: PublicClient | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    schedules = (
        ("1d", int(DAILY_FROM.timestamp()), int(CANDLE_UNTIL.timestamp())),
        ("1h", int(HOURLY_FROM.timestamp()), int(CANDLE_UNTIL.timestamp())),
    )
    jobs: list[tuple[str, str, int, int]] = []
    for pair in pairs:
        for interval, start, end in schedules:
            if not store.covered(pair, interval, start, end):
                jobs.append((pair, interval, start, end))
    if not jobs:
        return
    if not network:
        raise RuntimeError(
            f"{len(jobs)} candle ranges are not cached. Re-run with --network to fetch public OHLCV."
        )
    emit = log or (lambda _line: None)
    client = client or PublicClient(per_second=per_second)
    emit(f"fetching {len(jobs)} ranges for {len(pairs)} pairs")

    def _one(job: tuple[str, str, int, int]) -> tuple[tuple[str, str, int, int], list[Candle]]:
        pair, interval, start, end = job
        candles = client.fetch_range(pair, interval, start, end)
        return job, candles

    done = 0
    failed: list[tuple[tuple[str, str, int, int], str]] = []

    def _safe(job: tuple[str, str, int, int]) -> tuple[tuple[str, str, int, int], list[Candle], str | None]:
        try:
            return (*_one(job), None)
        except Exception as exc:  # one pair must not abort the replay
            return job, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_safe, job) for job in jobs]
        for future in as_completed(futures):
            job, candles, error = future.result()
            pair, interval, start, end = job
            if error is None:
                store.save(pair, interval, start, end, candles)
            else:
                failed.append((job, error))
            done += 1
            if done % 50 == 0 or done == len(jobs):
                emit(f"cached {done}/{len(jobs)} ({pair} {interval}, {len(candles)} bars, failed {len(failed)})")
    if failed:
        emit(f"retrying {len(failed)} failed ranges")
        for job, _error in failed:
            pair, interval, start, end = job
            try:
                candles = client.fetch_range(pair, interval, start, end)
            except Exception as exc:
                emit(f"give up {pair} {interval}: {type(exc).__name__}: {exc}")
                continue
            store.save(pair, interval, start, end, candles)


def last_closed_index(candles: list[Candle], signal_unix: int, bar_seconds: int) -> int:
    """Rightmost bar whose close time is <= signal_unix. -1 when none."""
    limit = signal_unix - bar_seconds
    lo = 0
    hi = len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid][0] <= limit:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _find_ts(candles: list[Candle], open_ts: int) -> Candle | None:
    lo = 0
    hi = len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        stamp = candles[mid][0]
        if stamp == open_ts:
            return candles[mid]
        if stamp < open_ts:
            lo = mid + 1
        else:
            hi = mid
    return None


def _sma_above(daily: list[Candle], signal_unix: int, length: int) -> tuple[bool | None, float | None, float | None]:
    """Return (close > sma, extension percent, sma). None when warmup is gapped or short."""
    index = last_closed_index(daily, signal_unix, BAR_1D)
    if index < length - 1:
        return None, None, None
    window = daily[index - length + 1 : index + 1]
    if len(window) != length:
        return None, None, None
    if window[-1][0] - window[0][0] != (length - 1) * BAR_1D:
        return None, None, None
    closes = [bar[4] for bar in window]
    sma = statistics.fmean(closes)
    if sma <= 0:
        return None, None, None
    close = window[-1][4]
    extension = (close / sma - 1.0) * 100.0
    return close > sma, extension, sma


def _pump_24h(hourly: list[Candle], signal_unix: int) -> float | None:
    index = last_closed_index(hourly, signal_unix, BAR_1H)
    if index < 24:
        return None
    last = hourly[index]
    prev = hourly[index - 24]
    if last[0] - prev[0] != 24 * BAR_1H:
        return None
    if prev[4] <= 0:
        return None
    return (last[4] / prev[4] - 1.0) * 100.0


def _rv_daily(hourly: list[Candle], signal_unix: int) -> float | None:
    index = last_closed_index(hourly, signal_unix, BAR_1H)
    if index < 24:
        return None
    window = hourly[index - 24 : index + 1]
    if window[-1][0] - window[0][0] != 24 * BAR_1H:
        return None
    returns: list[float] = []
    for prev, nxt in zip(window, window[1:]):
        if prev[4] <= 0:
            return None
        returns.append(nxt[4] / prev[4] - 1.0)
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(24.0)


def _path(hourly: list[Candle], signal_unix: int, delay_bars: int) -> dict[str, Any]:
    target = (signal_unix // BAR_1H + delay_bars) * BAR_1H
    entry = _find_ts(hourly, target)
    empty: dict[str, Any] = {
        "gross_24": None,
        "gross_72": None,
        "hit_m20_72": None,
        "hit_m20_7d": None,
    }
    if entry is None or entry[1] <= 0:
        return empty
    entry_open = entry[1]

    def _open_at(hours: int) -> float | None:
        bar = _find_ts(hourly, target + hours * BAR_1H)
        if bar is None:
            return None
        return bar[1]

    def _hit(hours: int) -> bool | None:
        # Complete only when every hour in the hold exists and the exit bar exists.
        if _find_ts(hourly, target + hours * BAR_1H) is None:
            return None
        for step in range(hours):
            bar = _find_ts(hourly, target + step * BAR_1H)
            if bar is None:
                return None
            if bar[3] <= entry_open * (1.0 - HIT_DRAWDOWN):
                return True
        return False

    open_24 = _open_at(24)
    open_72 = _open_at(72)
    return {
        "gross_24": (open_24 / entry_open - 1.0) if open_24 is not None else None,
        "gross_72": (open_72 / entry_open - 1.0) if open_72 is not None else None,
        "hit_m20_72": _hit(72),
        "hit_m20_7d": _hit(168),
    }


def listing_bounds(daily: list[Candle]) -> tuple[int | None, bool]:
    if not daily:
        return None, False
    first = daily[0][0]
    censored = first <= int(DAILY_FROM.timestamp()) + BAR_1D
    return first, censored


def listing_allows(age_days: float | None, censored: bool, threshold: float) -> bool | None:
    """True keeps, False drops, None excludes (unknown age)."""
    if age_days is None:
        return None
    if age_days >= threshold:
        return True
    if censored:
        return None
    return False


def volume_medians(hourly_by_symbol: dict[str, list[Candle]]) -> dict[str, dict[str, float]]:
    """UTC snapshot day -> symbol -> median of the prior 30 calendar daily quote sums.

    Daily sums are built from 1h quote volume. A snapshot day uses only
    earlier days. Missing hours in an observed span contribute 0.
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for symbol, candles in hourly_by_symbol.items():
        if not candles:
            continue
        daily: dict[str, float] = defaultdict(float)
        seen: set[str] = set()
        for open_ts, _opened, _high, _low, _close, quote in candles:
            day = datetime.fromtimestamp(open_ts, UTC).date().isoformat()
            daily[day] += quote
            seen.add(day)
        start = date.fromisoformat(min(seen))
        end = date.fromisoformat(max(seen))
        dates: list[str] = []
        cursor = start
        while cursor <= end:
            dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        values = [daily.get(day, 0.0) for day in dates]
        for index in range(30, len(values) + 1):
            window_days = dates[index - 30 : index]
            if not any(day in seen for day in window_days):
                continue
            if index < len(dates):
                snap = dates[index]
            else:
                snap = (date.fromisoformat(dates[-1]) + timedelta(days=1)).isoformat()
            out[snap][symbol] = statistics.median(values[index - 30 : index])
    return out


def is_small_cap(
    symbol: str,
    snapshot_day: str,
    volume_by_day: dict[str, dict[str, float]],
    top_n: int,
) -> bool:
    """Missing volume is small-cap. Rank 1 is the highest median quote volume."""
    peers = volume_by_day.get(snapshot_day) or {}
    median = peers.get(symbol)
    if median is None:
        return True
    higher = sum(1 for value in peers.values() if value > median)
    return higher + 1 > top_n


def _window_name(day: str) -> str:
    for name, start, end in WINDOWS:
        if start <= day <= end:
            return name
    return ""


def _half_name(day: str) -> str:
    for name, start, end in HALVES:
        if start <= day <= end:
            return name
    return ""


def build_feature_row(
    raw: RawEntry,
    coin_1h: list[Candle],
    coin_1d: list[Candle],
    btc_1d: list[Candle],
    volume_median: float | None,
) -> dict[str, Any]:
    signal_unix = int(raw.signal_ts.timestamp())
    above_50, extension, _sma50 = _sma_above(coin_1d, signal_unix, SMA_PRIMARY)
    above_38, _ext38, _sma38 = _sma_above(coin_1d, signal_unix, SMA_GRID[0])
    above_63, _ext63, _sma63 = _sma_above(coin_1d, signal_unix, SMA_GRID[1])
    btc_50, _bext, _bs = _sma_above(btc_1d, signal_unix, SMA_PRIMARY)
    btc_38, _, _ = _sma_above(btc_1d, signal_unix, SMA_GRID[0])
    btc_63, _, _ = _sma_above(btc_1d, signal_unix, SMA_GRID[1])
    first, censored = listing_bounds(coin_1d)
    if first is None:
        age = None
    else:
        age = (signal_unix - first) / float(BAR_1D)
    path_1 = _path(coin_1h, signal_unix, 1)
    path_2 = _path(coin_1h, signal_unix, 2)
    snapshot = datetime.fromtimestamp(signal_unix, UTC).date().isoformat()
    return {
        "entry_id": raw.entry_id,
        "symbol": raw.symbol,
        "signal_ts": raw.signal_ts.isoformat(),
        "day": raw.day_berlin,
        "snapshot_day": snapshot,
        "source": raw.source,
        "status": raw.status,
        "code": raw.code,
        "tenant": raw.tenant,
        "dataset": raw.dataset,
        "usdt": raw.usdt,
        "asset_class": "stable" if is_stablecoin_symbol(raw.symbol) else "other",
        "csv_fwd24": raw.csv_fwd24,
        "csv_fwd72": raw.csv_fwd72,
        "window": _window_name(raw.day_berlin),
        "half": _half_name(raw.day_berlin),
        "in_protocol": PROTOCOL_START <= raw.day_berlin <= PROTOCOL_END,
        "in_owner": OWNER_START <= raw.day_berlin <= OWNER_END,
        "sma_above_38": above_38,
        "sma_above_50": above_50,
        "sma_above_63": above_63,
        "extension_pct": extension,
        "pump_24h_pct": _pump_24h(coin_1h, signal_unix),
        "btc_above_38": btc_38,
        "btc_above_50": btc_50,
        "btc_above_63": btc_63,
        "volume_median": volume_median,
        "listing_age_days": age,
        "listing_censored": censored,
        "rv_daily": _rv_daily(coin_1h, signal_unix),
        "gross_24_d1": path_1["gross_24"],
        "gross_72_d1": path_1["gross_72"],
        "hit_m20_72_d1": path_1["hit_m20_72"],
        "hit_m20_7d_d1": path_1["hit_m20_7d"],
        "gross_24_d2": path_2["gross_24"],
        "gross_72_d2": path_2["gross_72"],
        "hit_m20_72_d2": path_2["hit_m20_72"],
    }


def compute_features(entries: list[RawEntry], store: CandleStore) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    symbols = sorted({row.symbol for row in entries})
    regime = f"{REGIME_BASE}/USDT"
    hourly: dict[str, list[Candle]] = {}
    daily: dict[str, list[Candle]] = {}
    for symbol in symbols:
        pair = gate_pair(symbol)
        hourly[symbol] = store.load(pair, "1h")
        daily[symbol] = store.load(pair, "1d")
    if regime not in hourly:
        hourly[regime] = store.load(gate_pair(regime), "1h")
        daily[regime] = store.load(gate_pair(regime), "1d")
    volume = volume_medians({symbol: candles for symbol, candles in hourly.items() if symbol != regime})
    btc_daily = daily.get(regime, [])
    rows: list[dict[str, Any]] = []
    for raw in entries:
        snap = datetime.fromtimestamp(int(raw.signal_ts.timestamp()), UTC).date().isoformat()
        median = (volume.get(snap) or {}).get(raw.symbol)
        rows.append(
            build_feature_row(
                raw,
                hourly.get(raw.symbol, []),
                daily.get(raw.symbol, []),
                btc_daily,
                median,
            )
        )
    return rows, volume


def accepts(
    spec: TrialSpec,
    row: dict[str, Any],
    volume_by_day: dict[str, dict[str, float]],
) -> bool | None:
    if spec.kind == "trend":
        return row.get(f"sma_above_{spec.sma}")
    if spec.kind == "btc":
        return row.get(f"btc_above_{spec.sma}")
    if spec.kind == "distance":
        extension = row.get("extension_pct")
        pump = row.get("pump_24h_pct")
        if extension is None or pump is None or spec.x_pct is None or spec.y_pct is None:
            return None
        return float(extension) <= spec.x_pct and float(pump) <= spec.y_pct
    if spec.kind == "trend_btc":
        coin = row.get("sma_above_50")
        btc = row.get("btc_above_50")
        if coin is None or btc is None:
            return None
        return bool(coin) and bool(btc)
    if spec.kind == "trend_distance":
        coin = row.get("sma_above_50")
        extension = row.get("extension_pct")
        pump = row.get("pump_24h_pct")
        if coin is None or extension is None or pump is None:
            return None
        return bool(coin) and float(extension) <= DISTANCE_X_PCT and float(pump) <= DISTANCE_Y_PCT
    if spec.kind == "smallcap":
        if spec.top_n is None:
            return None
        # Missing volume is a decision (drop), not an unknown.
        return not is_small_cap(str(row["symbol"]), str(row["snapshot_day"]), volume_by_day, spec.top_n)
    if spec.kind == "listing":
        if spec.min_age_days is None:
            return None
        age = row.get("listing_age_days")
        return listing_allows(None if age is None else float(age), bool(row.get("listing_censored")), spec.min_age_days)
    raise ValueError(f"unknown trial kind {spec.kind}")


def round_trip_cost_fraction(asset_class: str, cost_tier: str, *, small_cap: bool) -> float:
    slip = expected_slippage_pct(asset_class, cost_tier, small_cap=small_cap)
    return 2.0 * (TAKER_FEE_PCT_PER_SIDE + slip) / 100.0


def net_return(gross: float, asset_class: str, cost_tier: str, *, small_cap: bool) -> float:
    return gross - round_trip_cost_fraction(asset_class, cost_tier, small_cap=small_cap)


@dataclass
class Point:
    day: str
    window: str
    half: str
    kept: bool
    net24: float | None
    net72: float
    hit72: bool
    hit7d: bool | None
    source: str
    usdt: float | None
    rv_daily: float | None
    small_cap: bool


def score_trial(
    spec: TrialSpec,
    rows: list[dict[str, Any]],
    volume_by_day: dict[str, dict[str, float]],
) -> tuple[list[Point], int]:
    delay = spec.delay_bars
    gross72_key = f"gross_72_d{delay}"
    gross24_key = f"gross_24_d{delay}"
    hit_key = f"hit_m20_72_d{delay}"
    points: list[Point] = []
    excluded = 0
    for row in rows:
        decision = accepts(spec, row, volume_by_day)
        gross72 = row.get(gross72_key)
        hit = row.get(hit_key)
        if decision is None or gross72 is None or hit is None:
            excluded += 1
            continue
        small = is_small_cap(str(row["symbol"]), str(row["snapshot_day"]), volume_by_day, TOP_N_PRIMARY)
        asset = str(row.get("asset_class") or "other")
        gross24 = row.get(gross24_key)
        net24 = None
        if gross24 is not None:
            net24 = net_return(float(gross24), asset, spec.cost_tier, small_cap=small)
        hit7 = row.get("hit_m20_7d_d1") if delay == 1 else None
        points.append(
            Point(
                day=str(row["day"]),
                window=str(row.get("window") or ""),
                half=str(row.get("half") or ""),
                kept=bool(decision),
                net24=net24,
                net72=net_return(float(gross72), asset, spec.cost_tier, small_cap=small),
                hit72=bool(hit),
                hit7d=None if hit7 is None else bool(hit7),
                source=str(row.get("source") or ""),
                usdt=_float_or_none(row.get("usdt")),
                rv_daily=_float_or_none(row.get("rv_daily")),
                small_cap=small,
            )
        )
    return points, excluded


def _dist(nets: list[float], hits: list[bool]) -> dict[str, Any]:
    if not nets:
        return {
            "n": 0,
            "median_72": None,
            "mean_72": None,
            "positive_share_72": None,
            "hit_m20_share": None,
        }
    return {
        "n": len(nets),
        "median_72": statistics.median(nets) * 100.0,
        "mean_72": statistics.fmean(nets) * 100.0,
        "positive_share_72": 100.0 * sum(value > 0 for value in nets) / len(nets),
        "hit_m20_share": 100.0 * sum(hits) / len(hits),
    }


def _dist24(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {"n": 0, "median_24": None, "mean_24": None, "positive_share_24": None}
    return {
        "n": len(nets),
        "median_24": statistics.median(nets) * 100.0,
        "mean_24": statistics.fmean(nets) * 100.0,
        "positive_share_24": 100.0 * sum(value > 0 for value in nets) / len(nets),
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.fmean(values)


def sharpe_ratio(returns: list[float]) -> float:
    """Per-trade Sharpe: mean / sample stdev of net 72h returns. Not annualized."""
    if len(returns) < 2:
        return 0.0
    std = statistics.stdev(returns)
    if std == 0:
        return 0.0
    return statistics.fmean(returns) / std


def deflated_sharpe(returns: list[float], n_trials: int) -> float:
    """Bailey & López de Prado (2014) deflated Sharpe on the trade-return sample.

    N=1 uses SR0 = 0 (the probabilistic Sharpe ratio). Undefined inputs return 0,
    which fails the 0.95 kill rather than passing by omission.
    """
    n = len(returns)
    if n < 3 or n_trials < 1:
        return 0.0
    mean = statistics.fmean(returns)
    var = sum((value - mean) ** 2 for value in returns) / (n - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    sharpe = mean / std
    m2 = sum((value - mean) ** 2 for value in returns) / n
    if m2 <= 0:
        return 0.0
    m3 = sum((value - mean) ** 3 for value in returns) / n
    m4 = sum((value - mean) ** 4 for value in returns) / n
    skew = m3 / (m2**1.5)
    kurt = m4 / (m2**2)
    sr_var = (1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe * sharpe) / (n - 1)
    if sr_var <= 0:
        return 0.0
    sr_std = math.sqrt(sr_var)
    normal = statistics.NormalDist()
    if n_trials == 1:
        sr0 = 0.0
    else:
        z1 = normal.inv_cdf(1.0 - 1.0 / n_trials)
        z2 = normal.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
        sr0 = sr_std * ((1.0 - EULER) * z1 + EULER * z2)
    return float(normal.cdf((sharpe - sr0) / sr_std))


def _edge(kept: list[float], base: list[float]) -> float:
    """Mean kept minus mean baseline, in percentage points. Empty kept → 0."""
    if not kept or not base:
        return 0.0
    return (statistics.fmean(kept) - statistics.fmean(base)) * 100.0


def _subset_edge(points: list[Point], predicate: Callable[[Point], bool]) -> tuple[float, int, int]:
    chosen = [point for point in points if predicate(point)]
    kept = [point.net72 for point in chosen if point.kept]
    base = [point.net72 for point in chosen]
    return _edge(kept, base), len(chosen), len(kept)


def descriptive_metrics(points: list[Point], excluded: int, candidates: int) -> dict[str, Any]:
    kept = [point for point in points if point.kept]
    dropped = [point for point in points if not point.kept]
    base = points
    kept_24 = [point.net24 for point in kept if point.net24 is not None]
    drop_24 = [point.net24 for point in dropped if point.net24 is not None]
    base_24 = [point.net24 for point in base if point.net24 is not None]
    hit7_kept = [point.hit7d for point in kept if point.hit7d is not None]
    hit7_base = [point.hit7d for point in base if point.hit7d is not None]
    return {
        "n_candidates": candidates,
        "n_excluded": excluded,
        "excluded_entry_pct": (100.0 * excluded / candidates) if candidates else 0.0,
        "n_evaluable": len(points),
        "kept": {
            **_dist([point.net72 for point in kept], [point.hit72 for point in kept]),
            **_dist24([float(value) for value in kept_24]),
            "hit_m20_7d_share": (100.0 * sum(hit7_kept) / len(hit7_kept)) if hit7_kept else None,
            "hit_m20_7d_n": len(hit7_kept),
        },
        "dropped": {
            **_dist([point.net72 for point in dropped], [point.hit72 for point in dropped]),
            **_dist24([float(value) for value in drop_24]),
        },
        "b0": {
            **_dist([point.net72 for point in base], [point.hit72 for point in base]),
            **_dist24([float(value) for value in base_24]),
            "hit_m20_7d_share": (100.0 * sum(bool(v) for v in hit7_base) / len(hit7_base)) if hit7_base else None,
            "hit_m20_7d_n": len(hit7_base),
        },
    }


def edge_without_top10(points: list[Point]) -> float:
    kept = sorted((point.net72 for point in points if point.kept), reverse=True)
    base = [point.net72 for point in points]
    if len(kept) <= 10 or not base:
        return 0.0
    return (statistics.fmean(kept[10:]) - statistics.fmean(base)) * 100.0


def protocol_fields(spec: TrialSpec, points: list[Point], excluded: int, candidates: int) -> dict[str, Any]:
    metrics = descriptive_metrics(points, excluded, candidates)
    wins = 0
    windows: list[dict[str, Any]] = []
    for name, _start, _end in WINDOWS:
        edge, n_window, n_kept = _subset_edge(points, lambda point, name=name: point.window == name)
        win = edge > 0 and n_kept > 0 and n_window > 0
        wins += int(win)
        windows.append({"window": name, "edge_mean_72": edge, "n": n_window, "n_kept": n_kept, "win": win})
    half_a, n_a, kept_a = _subset_edge(points, lambda point: point.half == "A")
    half_b, n_b, kept_b = _subset_edge(points, lambda point: point.half == "B")
    kept_nets = [point.net72 for point in points if point.kept]
    base_nets = [point.net72 for point in points]
    edge = _edge(kept_nets, base_nets)
    group_ready = {
        "edge_mean_72": edge,
        "edge_half_a": half_a,
        "edge_half_b": half_b,
        "edge_without_top10": edge_without_top10(points),
        "sharpe": sharpe_ratio(kept_nets),
        "dsr": deflated_sharpe(kept_nets, TRIAL_COUNT_N),
        "n_trades": len(kept_nets),
        "walk_forward_wins": wins,
        "walk_forward_windows": 3,
        "windows": windows,
        "half_a_n": n_a,
        "half_a_kept": kept_a,
        "half_b_n": n_b,
        "half_b_kept": kept_b,
        "excluded_entry_pct": metrics["excluded_entry_pct"],
    }
    return {"metrics": metrics, "protocol_inputs": group_ready}


def attach_group_and_validate(results: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for result in results:
        grouped[str(result["group"])].append(float(result["protocol_inputs"]["edge_mean_72"]))
    signs: dict[str, set[int]] = {}
    for result in results:
        edge = float(result["protocol_inputs"]["edge_mean_72"])
        signs.setdefault(str(result["group"]), set()).add(1 if edge > 0 else -1 if edge < 0 else 0)
    for result in results:
        group = str(result["group"])
        edges = grouped[group]
        result["protocol_inputs"]["group_median_edge"] = statistics.median(edges) if edges else 0.0
        # A one-member group has no ±25% grid. Mixed signs on a real grid are a cliff.
        result["protocol_inputs"]["parameter_cliff"] = len(edges) > 1 and len(signs[group]) > 1
        result["protocol_inputs"]["parameter_grid_ran"] = len(edges) > 1
        payload = build_payload(result)
        verdict = validate_payload(payload)
        result["payload"] = payload
        result["protocol_status"] = verdict.status
        result["protocol_reasons"] = verdict.reasons


def build_payload(result: dict[str, Any]) -> dict[str, Any]:
    inputs = result["protocol_inputs"]
    spec: TrialSpec = result["spec"]
    small_cap = spec.cost_tier == "hard_stress"
    asset_class = "other"
    slip = expected_slippage_pct(asset_class, spec.cost_tier, small_cap=small_cap)
    payload: dict[str, Any] = {
        "trial_id": spec.trial_id,
        "n_trades": int(inputs["n_trades"]),
        "sharpe": float(inputs["sharpe"]),
        "dsr": float(inputs["dsr"]),
        "trial_count_n": TRIAL_COUNT_N,
        "same_bar_fill": False,
        "one_bar_delay": True,
        "point_in_time_universe": True,
        "fees_applied": True,
        "slippage_applied": True,
        "funding": "not_applicable",
        "asset_class": asset_class,
        "cost_tier": spec.cost_tier,
        "taker_fee_pct_per_side": TAKER_FEE_PCT_PER_SIDE,
        "slippage_pct_per_side": slip,
        "walk_forward_wins": int(inputs["walk_forward_wins"]),
        "walk_forward_windows": 3,
        "edge_without_top10": float(inputs["edge_without_top10"]),
        "edge_half_a": float(inputs["edge_half_a"]),
        "edge_half_b": float(inputs["edge_half_b"]),
        "group_median_edge": float(inputs["group_median_edge"]),
        "excluded_entry_pct": float(inputs["excluded_entry_pct"]),
        "holdout_entries": HOLDOUT_ENTRIES,
        "parameter_cliff": bool(inputs["parameter_cliff"]),
        "parameter_cliff_grid_pct": 25,
        "signal_against_higher_frame": False,
        "buying_into_loss": False,
        "edge_unit": "percentage_points_mean_net_72h_vs_b0",
        "parameter_grid_ran": bool(inputs["parameter_grid_ran"]),
        "cost_note": (
            "Reference slippage is the s2 schedule for a non-stable name at this tier. "
            "The replay charges that schedule per trade (stable vs other, and small-cap "
            "only changes hard stress). A flat per-side cost cancels in the mean edge."
        ),
    }
    if spec.cost_tier == "hard_stress":
        payload["small_cap"] = True
    return payload


def rank_key(result: dict[str, Any]) -> tuple[float, ...]:
    """Pre-registered sort. Higher is better. n < 100 sorts behind n >= 100."""
    metrics = result["metrics"]
    kept = metrics["kept"]
    base = metrics["b0"]
    n_kept = int(kept["n"] or 0)
    m20_base = base.get("hit_m20_share")
    m20_kept = kept.get("hit_m20_share")
    if m20_base is None or m20_kept is None:
        m20_reduction = -999.0
    else:
        m20_reduction = float(m20_base) - float(m20_kept)
    med_base = base.get("median_72")
    med_kept = kept.get("median_72")
    median_lift = -999.0 if med_base is None or med_kept is None else float(med_kept) - float(med_base)
    mean_base = base.get("mean_72")
    mean_kept = kept.get("mean_72")
    mean_lift = -999.0 if mean_base is None or mean_kept is None else float(mean_kept) - float(mean_base)
    return (
        1.0 if n_kept >= 100 else 0.0,
        float(result["protocol_inputs"]["walk_forward_wins"]),
        m20_reduction,
        median_lift,
        mean_lift,
    )


def evaluate_population(
    specs: list[TrialSpec],
    rows: list[dict[str, Any]],
    volume_by_day: dict[str, dict[str, float]],
    *,
    with_protocol: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in specs:
        points, excluded = score_trial(spec, rows, volume_by_day)
        metrics = descriptive_metrics(points, excluded, len(rows))
        result: dict[str, Any] = {
            "spec": spec,
            "trial_id": spec.trial_id,
            "kind": spec.kind,
            "group": spec.group,
            "rank": spec.rank,
            "cost_tier": spec.cost_tier,
            "delay_bars": spec.delay_bars,
            "metrics": metrics,
        }
        if with_protocol:
            fields = protocol_fields(spec, points, excluded, len(rows))
            result["protocol_inputs"] = fields["protocol_inputs"]
            result["points_for_vol"] = points if spec.trial_id == "trend-sma50" and spec.cost_tier == "base" else None
        results.append(result)
    if with_protocol:
        attach_group_and_validate(results)
        order = {spec.trial_id: index for index, spec in enumerate(specs) if spec.rank}
        ranked = [result for result in results if result["rank"]]
        ranked.sort(key=lambda item: (rank_key(item), -order[item["trial_id"]]), reverse=True)
        for index, result in enumerate(ranked, start=1):
            result["rank_position"] = index
    return results


def volatility_sizing(points: list[Point]) -> dict[str, Any]:
    """Report only. Size multiplier = min(1, target_daily_vol / realized_daily_vol)."""
    raw_pnl: list[float] = []
    scaled_pnl: list[float] = []
    multipliers: list[float] = []
    skipped = 0
    for point in points:
        if point.usdt is None or point.rv_daily is None or point.rv_daily <= 0:
            skipped += 1
            continue
        multiplier = min(1.0, TARGET_DAILY_VOL / point.rv_daily)
        raw_pnl.append(point.usdt * point.net72)
        scaled_pnl.append(point.usdt * multiplier * point.net72)
        multipliers.append(multiplier)
    if not raw_pnl:
        return {"n": 0, "skipped": skipped}
    return {
        "n": len(raw_pnl),
        "skipped": skipped,
        "target_daily_vol": TARGET_DAILY_VOL,
        "sum_pnl_usdt": sum(raw_pnl),
        "sum_scaled_pnl_usdt": sum(scaled_pnl),
        "worst_pnl_usdt": min(raw_pnl),
        "worst_scaled_pnl_usdt": min(scaled_pnl),
        "median_multiplier": statistics.median(multipliers),
        "share_scaled_down": 100.0 * sum(value < 1.0 for value in multipliers) / len(multipliers),
        "note": (
            "Report only. Not a trial and not a pass/fail. "
            "The 5 USDT loss cap in the Herman/Volrix note is not an owner rule."
        ),
    }


def source_strata(rows: list[dict[str, Any]], volume_by_day: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """Descriptive. Not a trial: a source ban was not pre-registered."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("source") or "")].append(row)
    table: list[dict[str, Any]] = []
    for source, subset in sorted(grouped.items(), key=lambda item: -len(item[1])):
        points, excluded = _score_passthrough(subset, volume_by_day)
        metrics = descriptive_metrics(points, excluded, len(subset))
        table.append(
            {
                "source": source,
                "n_rows": len(subset),
                "n_evaluable": metrics["n_evaluable"],
                "median_72": metrics["b0"]["median_72"],
                "mean_72": metrics["b0"]["mean_72"],
                "positive_share_72": metrics["b0"]["positive_share_72"],
                "hit_m20_share": metrics["b0"]["hit_m20_share"],
                "excluded": excluded,
            }
        )
    return table


def _score_passthrough(
    rows: list[dict[str, Any]],
    volume_by_day: dict[str, dict[str, float]],
) -> tuple[list[Point], int]:
    points: list[Point] = []
    excluded = 0
    for row in rows:
        gross72 = row.get("gross_72_d1")
        hit = row.get("hit_m20_72_d1")
        if gross72 is None or hit is None:
            excluded += 1
            continue
        small = is_small_cap(str(row["symbol"]), str(row["snapshot_day"]), volume_by_day, TOP_N_PRIMARY)
        asset = str(row.get("asset_class") or "other")
        gross24 = row.get("gross_24_d1")
        points.append(
            Point(
                day=str(row["day"]),
                window=str(row.get("window") or ""),
                half=str(row.get("half") or ""),
                kept=True,
                net24=None if gross24 is None else net_return(float(gross24), asset, "base", small_cap=small),
                net72=net_return(float(gross72), asset, "base", small_cap=small),
                hit72=bool(hit),
                hit7d=None if row.get("hit_m20_7d_d1") is None else bool(row.get("hit_m20_7d_d1")),
                source=str(row.get("source") or ""),
                usdt=_float_or_none(row.get("usdt")),
                rv_daily=_float_or_none(row.get("rv_daily")),
                small_cap=small,
            )
        )
    return points, excluded


def tier_and_listing_strata(
    rows: list[dict[str, Any]],
    volume_by_day: dict[str, dict[str, float]],
) -> dict[str, Any]:
    def _pack(subset: list[dict[str, Any]]) -> dict[str, Any]:
        points, excluded = _score_passthrough(subset, volume_by_day)
        metrics = descriptive_metrics(points, excluded, len(subset))
        return {
            "n_rows": len(subset),
            "n_evaluable": metrics["n_evaluable"],
            "median_72": metrics["b0"]["median_72"],
            "mean_72": metrics["b0"]["mean_72"],
            "positive_share_72": metrics["b0"]["positive_share_72"],
            "hit_m20_share": metrics["b0"]["hit_m20_share"],
        }

    small: list[dict[str, Any]] = []
    large: list[dict[str, Any]] = []
    young: list[dict[str, Any]] = []
    mid: list[dict[str, Any]] = []
    old: list[dict[str, Any]] = []
    for row in rows:
        if is_small_cap(str(row["symbol"]), str(row["snapshot_day"]), volume_by_day, TOP_N_PRIMARY):
            small.append(row)
        else:
            large.append(row)
        decision_30 = listing_allows(
            None if row.get("listing_age_days") is None else float(row["listing_age_days"]),
            bool(row.get("listing_censored")),
            30.0,
        )
        decision_90 = listing_allows(
            None if row.get("listing_age_days") is None else float(row["listing_age_days"]),
            bool(row.get("listing_censored")),
            90.0,
        )
        if decision_30 is False:
            young.append(row)
        elif decision_90 is False:
            mid.append(row)
        elif decision_90 is True:
            old.append(row)
    return {
        "top100": _pack(large),
        "small_cap": _pack(small),
        "listing_lt_30": _pack(young),
        "listing_30_to_90": _pack(mid),
        "listing_ge_90": _pack(old),
        "note": "Descriptive strata. Listing and small-cap filters above are the trials; this table is the raw split.",
    }


def csv_crosscheck(rows: list[dict[str, Any]]) -> dict[str, Any]:
    filled = [
        row
        for row in rows
        if row.get("dataset") == "decisions" and row.get("status") == "filled" and row.get("source") == "entry_sensor_15m"
    ]
    csv_vals = [float(row["csv_fwd72"]) for row in filled if row.get("csv_fwd72") is not None]
    own = [float(row["gross_72_d1"]) * 100.0 for row in filled if row.get("gross_72_d1") is not None]
    return {
        "filled_entry_sensor_15m_rows": len(filled),
        "csv_fwd72_n": len(csv_vals),
        "csv_fwd72_median": statistics.median(csv_vals) if csv_vals else None,
        "replay_gross_72_n": len(own),
        "replay_gross_72_median": statistics.median(own) if own else None,
        "attached_published_median": -4.66,
        "note": (
            "Attached median -4.66% is the deduped filled entry_sensor_15m figure in shadow_4w. "
            "Replay gross uses the next 1h open, so it will not match a same-timestamp close return."
        ),
    }


def _fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown_table(results: list[dict[str, Any]], *, protocol: bool) -> str:
    header = (
        "| filter | kept n | 72h median before | 72h median after | "
        "hit -20% before | hit -20% after | walk-forward wins | protocol |"
    )
    lines = [header, "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    ordered = sorted(
        [row for row in results if row.get("rank")],
        key=lambda row: int(row.get("rank_position") or 999),
    )
    for row in ordered:
        kept = row["metrics"]["kept"]
        base = row["metrics"]["b0"]
        wins = ""
        status = ""
        if protocol:
            wins = str(row["protocol_inputs"]["walk_forward_wins"])
            status = str(row.get("protocol_status") or "")
        lines.append(
            "| {trial} | {n} | {before} | {after} | {h0} | {h1} | {wins} | {status} |".format(
                trial=row["trial_id"],
                n=kept["n"],
                before=_fmt(base.get("median_72")),
                after=_fmt(kept.get("median_72")),
                h0=_fmt(base.get("hit_m20_share")),
                h1=_fmt(kept.get("hit_m20_share")),
                wins=wins,
                status=status,
            )
        )
    return "\n".join(lines)


def recommendation_text(ranked: list[dict[str, Any]]) -> str:
    if not ranked:
        return "No ranked filter produced a result."
    top = ranked[0]
    kept = top["metrics"]["kept"]
    status = str(top.get("protocol_status") or "UNCLEAR")
    wins = int(top["protocol_inputs"]["walk_forward_wins"])
    mean_kept = kept.get("mean_72")
    parts = [
        f"Do not change live or paper order flow.",
        f"{top['trial_id']} is first on the pre-registered rank (kept n={kept['n']}, walk-forward {wins}/3, protocol {status}).",
    ]
    if status != "PASS":
        parts.append(
            "The hold-out starts 2026-11-06, so this tape cannot prove a filter. Status is not PASS."
        )
    if int(kept["n"] or 0) < 100:
        parts.append("Kept trades are under 100, so the result is not provable.")
    if wins < 2:
        parts.append("It does not win 2 of the 3 walk-forward windows.")
    if mean_kept is not None and float(mean_kept) <= 0:
        parts.append(
            f"Kept trades still have a negative mean 72h net return ({float(mean_kept):.2f}%)."
        )
    parts.append("The spec is observe-only until the owner approves a later revision.")
    return " ".join(parts)


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "trial_id": result["trial_id"],
        "kind": result["kind"],
        "group": result["group"],
        "rank": result["rank"],
        "rank_position": result.get("rank_position"),
        "cost_tier": result["cost_tier"],
        "delay_bars": result["delay_bars"],
        "metrics": result["metrics"],
        "protocol_status": result.get("protocol_status"),
        "protocol_reasons": result.get("protocol_reasons"),
    }
    if "protocol_inputs" in result:
        payload["protocol_inputs"] = result["protocol_inputs"]
    if "payload" in result:
        payload["payload"] = result["payload"]
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_features(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def read_features(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_report(
    rows: list[dict[str, Any]],
    volume_by_day: dict[str, dict[str, float]],
) -> dict[str, Any]:
    specs = trial_specs()
    fills_protocol = [row for row in rows if row["dataset"] == "fills" and row["in_protocol"]]
    fills_owner = [row for row in rows if row["dataset"] == "fills" and row["in_owner"]]
    decisions_owner = [row for row in rows if row["dataset"] == "decisions" and row["in_owner"]]
    protocol = evaluate_population(specs, fills_protocol, volume_by_day, with_protocol=True)
    owner_fills = evaluate_population(specs, fills_owner, volume_by_day, with_protocol=False)
    owner_decisions = evaluate_population(specs, decisions_owner, volume_by_day, with_protocol=False)
    ranked = sorted(
        [row for row in protocol if row.get("rank")],
        key=lambda row: int(row.get("rank_position") or 999),
    )
    positions = {row["trial_id"]: row["rank_position"] for row in ranked}
    for bucket in (owner_fills, owner_decisions):
        for row in bucket:
            if row["trial_id"] in positions:
                row["rank_position"] = positions[row["trial_id"]]
    # Volatility sizing is on the unfiltered evaluable book, not on a filter's kept set.
    base_points, _excluded = _score_passthrough(fills_protocol, volume_by_day)
    report = {
        "registered": {
            "sma_primary": SMA_PRIMARY,
            "sma_grid": list(SMA_GRID),
            "distance_x_pct": DISTANCE_X_PCT,
            "distance_y_pct": DISTANCE_Y_PCT,
            "top_n": TOP_N_PRIMARY,
            "top_n_grid": list(TOP_N_GRID),
            "listing_age_days": list(LISTING_AGE_PRIMARY),
            "target_daily_vol": TARGET_DAILY_VOL,
            "trial_count_n": TRIAL_COUNT_N,
            "holdout_entries": HOLDOUT_ENTRIES,
            "windows": [list(item) for item in WINDOWS],
            "halves": [list(item) for item in HALVES],
            "protocol_window": [PROTOCOL_START, PROTOCOL_END],
            "owner_window": [OWNER_START, OWNER_END],
            "rank_rule": "n>=100, then walk-forward wins, then -20% share reduction, then 72h median lift, then 72h mean lift",
        },
        "counts": {
            "feature_rows": len(rows),
            "fills_protocol": len(fills_protocol),
            "fills_owner": len(fills_owner),
            "decisions_owner": len(decisions_owner),
        },
        "protocol_fills": [_public_result(row) for row in protocol],
        "owner_fills": [_public_result(row) for row in owner_fills],
        "owner_decisions": [_public_result(row) for row in owner_decisions],
        "ranking": [row["trial_id"] for row in ranked],
        "recommendation": recommendation_text(ranked),
        "volatility_sizing": volatility_sizing(base_points),
        "source_strata_protocol_fills": source_strata(fills_protocol, volume_by_day),
        "tier_strata_protocol_fills": tier_and_listing_strata(fills_protocol, volume_by_day),
        "csv_crosscheck": csv_crosscheck(rows),
        "tables": {
            "protocol_fills": markdown_table(protocol, protocol=True),
            "owner_fills": markdown_table(owner_fills, protocol=False),
            "owner_decisions": markdown_table(owner_decisions, protocol=False),
        },
    }
    return report


def _strip_points(results: list[dict[str, Any]]) -> None:
    for result in results:
        result.pop("points_for_vol", None)
        result.pop("spec", None)


def build_from_files(
    decisions_path: Path,
    fills_path: Path,
    store: CandleStore,
    *,
    network: bool,
    workers: int,
    per_second: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]], dict[str, Any]]:
    decisions = load_decisions(decisions_path)
    fills = load_fills(fills_path)
    raw = entry_universe(decisions + fills)
    pairs = sorted({gate_pair(row.symbol) for row in raw})
    pairs.append(gate_pair(f"{REGIME_BASE}/USDT"))
    ensure_candles(
        store,
        pairs,
        network=network,
        workers=workers,
        per_second=per_second,
        log=lambda line: print(line, file=sys.stderr),
    )
    features, volume = compute_features(raw, store)
    meta = {
        "decisions_rows": len(decisions),
        "fill_rows": len(fills),
        "universe_rows": len(raw),
        "decisions_sha256": file_sha256(decisions_path),
        "fills_sha256": file_sha256(fills_path),
        "adds_skipped": sum(1 for row in decisions + fills if row.is_add),
    }
    return features, volume, meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay pre-registered entry filters on paper tape")
    parser.add_argument("--decisions", type=Path, help="buy_decisions_forward_returns.csv")
    parser.add_argument("--fills", type=Path, help="orders_v2_filled_alltime.jsonl")
    parser.add_argument("--cache", type=Path, required=True, help="SQLite candle cache path")
    parser.add_argument("--out", type=Path, required=True, help="Directory for report.json and features.jsonl")
    parser.add_argument("--network", action="store_true", help="Fetch missing public OHLCV into the cache")
    parser.add_argument("--from-features", type=Path, help="Skip candle load; read a features JSONL")
    parser.add_argument("--volume", type=Path, help="volume_medians.json when using --from-features")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--per-second", type=float, default=6.0)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {}
    if args.from_features:
        features = read_features(args.from_features)
        if args.volume is None:
            raise SystemExit("--volume is required with --from-features")
        volume = json.loads(args.volume.read_text(encoding="utf-8"))
    else:
        if args.decisions is None or args.fills is None:
            raise SystemExit("--decisions and --fills are required unless --from-features is set")
        store = CandleStore(args.cache)
        try:
            features, volume, meta = build_from_files(
                args.decisions,
                args.fills,
                store,
                network=args.network,
                workers=args.workers,
                per_second=args.per_second,
            )
        finally:
            store.close()
        write_features(args.out / "features.jsonl", features)
        (args.out / "volume_medians.json").write_text(json.dumps(volume), encoding="utf-8")
    report = run_report(features, volume)
    report["inputs"] = meta
    # Drop non-JSON leftovers if any slipped through.
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.out / "report.md").write_text(
        "\n\n".join(
            [
                report["recommendation"],
                "## Protocol fills",
                report["tables"]["protocol_fills"],
                "## Owner-window fills",
                report["tables"]["owner_fills"],
                "## Owner-window decisions",
                report["tables"]["owner_decisions"],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(report["recommendation"])
    print(report["tables"]["protocol_fills"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
