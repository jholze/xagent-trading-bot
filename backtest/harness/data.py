"""Inputs for the harness: candles, the filled-order tape, the book, tenant params.

Everything here is read-only. Candles come from a local JSON cache that can be
filled by a public, key-less ccxt fetch (``fetch_public_ohlcv``). Tape and book
files are exports of the paper ledger (``orders_v2`` filled rows, the
``positions`` documents, ``tenant_configs``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
TF_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}

# open_ts (unix seconds, UTC), open, high, low, close, base volume
Candle = tuple[int, float, float, float, float, float]


def pair_file(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_") + ".json"


def parse_naive_berlin(text: str) -> datetime:
    """Ledger timestamps without an offset are Europe/Berlin wall time."""
    raw = str(text).strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BERLIN)
    return moment.astimezone(UTC)


def to_naive_berlin_iso(moment: datetime) -> str:
    """Inverse of ``parse_naive_berlin``: what the live writer stores."""
    return moment.astimezone(BERLIN).replace(tzinfo=None).isoformat()


# --------------------------------------------------------------------------
# candles


class CandleCache:
    """JSON-per-symbol cache: ``<root>/<timeframe>/<BASE>_<QUOTE>.json``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._mem: dict[tuple[str, str], list[Candle]] = {}

    def path(self, symbol: str, timeframe: str) -> Path:
        return self.root / timeframe / pair_file(symbol)

    def get(self, symbol: str, timeframe: str) -> list[Candle]:
        key = (symbol, timeframe)
        if key not in self._mem:
            path = self.path(symbol, timeframe)
            rows: list[Candle] = []
            if path.exists():
                rows = [tuple(r) for r in json.loads(path.read_text())]  # type: ignore[misc]
            self._mem[key] = sorted(rows)
        return self._mem[key]

    def put(self, symbol: str, timeframe: str, candles: Iterable[Candle]) -> None:
        merged = {c[0]: c for c in self.get(symbol, timeframe)}
        for c in candles:
            merged[int(c[0])] = (int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]))
        rows = [merged[k] for k in sorted(merged)]
        path = self.path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows))
        self._mem[(symbol, timeframe)] = rows

    def import_gate_json_dir(self, directory: Path, timeframe: str = "1h") -> int:
        """Merge raw Gate v4 candlestick dumps ([t, quote_vol, c, h, l, o, base_vol, closed])."""
        count = 0
        for file in sorted(Path(directory).glob("*.json")):
            try:
                raw = json.loads(file.read_text())
            except ValueError:
                continue
            head, _, quote = file.stem.rpartition("_")
            symbol = f"{head}/{quote}"
            rows = []
            for r in raw if isinstance(raw, list) else []:
                try:
                    if len(r) > 7 and str(r[7]).lower() == "false":
                        continue  # bar not closed yet
                    rows.append((int(r[0]), float(r[5]), float(r[3]), float(r[4]), float(r[2]), float(r[6])))
                except (TypeError, ValueError, IndexError):
                    continue
            if rows:
                self.put(symbol, timeframe, rows)
                count += 1
        return count


def fetch_public_ohlcv(
    cache: CandleCache,
    symbols: Iterable[str],
    timeframe: str,
    since: datetime,
    until: datetime,
    *,
    exchange_id: str = "gate",
    pause_s: float = 0.12,
    log=print,
) -> dict[str, str]:
    """Fill the cache from the exchange's public OHLCV endpoint via ccxt.

    No API key is configured on the client. Returns {symbol: error} for pairs
    that could not be fetched (delisted, renamed). Already-covered ranges are
    skipped, so a re-run only fetches the gap.
    """
    import ccxt  # local import: tests never need the network

    client = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    step = TF_SECONDS[timeframe]
    start_s, end_s = int(since.timestamp()), int(until.timestamp())
    errors: dict[str, str] = {}
    for n, symbol in enumerate(sorted(set(symbols)), 1):
        have = cache.get(symbol, timeframe)
        if have and have[0][0] <= start_s + step and have[-1][0] >= end_s - 2 * step:
            continue
        cursor = start_s if not have or have[0][0] > start_s + step else have[-1][0] + step
        got: list[Candle] = []
        try:
            while cursor < end_s:
                batch = client.fetch_ohlcv(symbol, timeframe, since=cursor * 1000, limit=1000)
                if not batch:
                    break
                for ms, o, h, low, c, v in batch:
                    ts = int(ms // 1000)
                    if ts + step <= end_s:  # closed bars only
                        got.append((ts, float(o), float(h), float(low), float(c), float(v or 0.0)))
                nxt = int(batch[-1][0] // 1000) + step
                if nxt <= cursor:
                    break
                cursor = nxt
                time.sleep(pause_s)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            errors[symbol] = f"{type(exc).__name__}: {str(exc)[:120]}"
        if got:
            cache.put(symbol, timeframe, got)
        if n % 50 == 0:
            log(f"fetched {n} symbols ({timeframe}), errors {len(errors)}")
    return errors


# --------------------------------------------------------------------------
# tape, book, params


@dataclass(frozen=True)
class Fill:
    order_id: str
    tenant: str
    symbol: str
    timeframe: str
    side: str  # buy | sell
    signal: str
    source: str
    exit_source: str | None
    ts: datetime  # UTC
    price: float
    amount: float
    usdt: float

    @property
    def is_add(self) -> bool:
        return self.signal == "BUY_DCA" or self.source in ("dca", "dca_sniper")

    @property
    def lot_key(self) -> tuple[str, str, str]:
        return (self.tenant, self.symbol, self.timeframe)


def load_fills(path: Path, *, exclude_tenants: Iterable[str] = ("decisions",)) -> list[Fill]:
    """Filled long-side orders (buy/sell) from an ``orders_v2`` JSONL export.

    Shorts and covers are skipped: the harness is spot-long only.
    """
    skip = set(exclude_tenants)
    fills: list[Fill] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "filled" or row.get("side") not in ("buy", "sell"):
                continue
            if row.get("tenant_id") in skip:
                continue
            ex = row.get("execution") or {}
            try:
                price = float(ex.get("price") or 0)
                amount = float(ex.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or amount <= 0:
                continue
            ts_raw = (row.get("timestamps") or {}).get("filled") or row.get("ts_event")
            fills.append(
                Fill(
                    order_id=str(row.get("_id") or row.get("id")),
                    tenant=str(row.get("tenant_id") or "default"),
                    symbol=str(row["symbol"]),
                    timeframe=str(row.get("timeframe") or "1h"),
                    side=str(row["side"]),
                    signal=str(row.get("signal") or ""),
                    source=str(row.get("source") or ""),
                    exit_source=row.get("exit_source"),
                    ts=parse_naive_berlin(ts_raw),
                    price=price,
                    amount=amount,
                    usdt=float(ex.get("usdt") or price * amount),
                )
            )
    fills.sort(key=lambda f: (f.ts, f.order_id))
    return fills


@dataclass
class BookLot:
    tenant: str
    symbol: str
    timeframe: str
    tier: str | None
    lock_since: datetime | None
    lock_modes: tuple[str, ...] = field(default_factory=tuple)
    raw: dict = field(default_factory=dict)


def load_book(path: Path) -> dict[tuple[str, str, str], BookLot]:
    """Current ``positions`` documents: tier and lock per lot (read-only export)."""
    out: dict[tuple[str, str, str], BookLot] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            doc = json.loads(line)
            tenant = str(doc.get("tenant_id") or "default")
            for key, pos in (doc.get("positions") or {}).items():
                base_quote, _, tf = key.rpartition("_")
                head, _, quote = base_quote.rpartition("_")
                symbol = f"{head}/{quote}"
                lock = pos.get("lock") or {}
                since = None
                if lock.get("enabled") and lock.get("locked_at"):
                    try:
                        since = parse_naive_berlin(lock["locked_at"])
                    except ValueError:
                        since = None
                out[(tenant, symbol, tf)] = BookLot(
                    tenant=tenant,
                    symbol=symbol,
                    timeframe=tf,
                    tier=pos.get("strategy_tier") or None,
                    lock_since=since,
                    lock_modes=tuple(lock.get("modes") or ()) if since else (),
                    raw=pos,
                )
    return out


_TIER_BLOCK = {"volatile": "volatile_altcoin", "stable": "stable_altcoin", "mid": "mid_cap_defaults"}


@dataclass
class TenantParams:
    tenant: str
    body: dict

    def risk(self) -> dict:
        return dict(self.body.get("risk") or {})

    def strategy_params(self, tier: str) -> dict:
        """Tier block merged over the top-level stop, like the resolver's static part."""
        params: dict = {"stop_loss_pct": float(self.body.get("stop_loss_pct") or 50.0)}
        block = self.body.get(_TIER_BLOCK.get(tier, "volatile_altcoin")) or {}
        for key, value in block.items():
            if not str(key).startswith("_"):
                params[key] = value
        if params.get("partial_stop_pct") is None and params.get("partial_stop_ratio") is None:
            risk = self.risk()
            defaults = self.body.get("dry_run_defaults") or {}
            ratio = defaults.get("partial_stop_ratio", risk.get("partial_stop_ratio", 0.67))
            params["partial_stop_ratio"] = float(ratio)
        return params

    def partial_limits(self) -> dict:
        risk = self.risk()
        cmc = self.body.get("cmc") or {}
        return {
            "min_position_usdt": float(
                risk.get("min_position_usdt_for_partial_sell")
                or cmc.get("min_position_usdt_for_social_sell", 25)
            ),
            "min_notional_usdt": float(
                risk.get("min_sell_notional_usdt") or cmc.get("min_sell_notional_usdt", 15)
            ),
            "max_sold_percent": risk.get("block_partial_sell_if_sold_percent_above"),
        }


def load_tenant_params(path: Path | None, fallback_config: Path) -> dict[str, TenantParams]:
    """Tenant overlays from a ``tenant_configs`` export; ``default`` uses config.json."""
    out = {"default": TenantParams("default", json.loads(Path(fallback_config).read_text()))}
    if path and Path(path).exists():
        latest: dict[str, tuple[str, dict]] = {}
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                doc = json.loads(line)
                body = doc.get("body")
                if isinstance(body, str):
                    body = json.loads(body)
                tid = str(doc.get("tenant_id") or "")
                stamp = str(doc.get("updated_at") or "")
                if tid and isinstance(body, dict) and (tid not in latest or stamp > latest[tid][0]):
                    latest[tid] = (stamp, body)
        for tid, (_, body) in latest.items():
            out[tid] = TenantParams(tid, body)
    return out
