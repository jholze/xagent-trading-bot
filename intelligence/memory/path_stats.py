"""Market-path episode stats for path-stats memory (offline / cron).

Pure functions + optional Mongo persistence. No DecisionEngine / trade wiring.
Kill-switch: MEMORY_PATH_STATS=0 or memory.path_stats.enabled=false.
"""

from __future__ import annotations

import os
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from intelligence.memory.models import utc_now_iso
from logger import log

COL_PATH_STATS = "memory_path_stats"

# Peak-gain first-touch bands (fraction, not percent)
DEFAULT_BANDS: tuple[float, ...] = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20)
DEFAULT_TROUGH_LOOKBACK = 48
DEFAULT_FORWARD_BARS = 24
DEFAULT_TRAIL_HIT = 0.08
DEFAULT_EXT_HIT = 0.05
MIN_OK_SAMPLES = 5


def path_stats_enabled(config: dict | None = None) -> bool:
    """Master kill-switch — default off until explicitly enabled."""
    env = (os.environ.get("MEMORY_PATH_STATS") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        mem = (config or {}).get("memory") or {}
        ps = mem.get("path_stats") or {}
        if "enabled" in ps:
            return bool(ps.get("enabled"))
    except Exception:
        pass
    # Safe default: off (rollback / no accidental writes)
    return False


def band_label(band: float) -> str:
    return f"{int(round(band * 100))}pct"


@dataclass
class PathEpisode:
    arm_index: int
    trough: float
    peak_at_arm: float
    band: float
    max_giveback: float
    hit_trail: bool
    hit_extension: bool
    end_gain_from_trough: float


@dataclass
class PathBandSummary:
    symbol: str
    timeframe: str
    band: float
    band_key: str
    n: int = 0
    median_max_giveback: float | None = None
    p_hit_trail: float | None = None
    p_hit_extension: float | None = None
    median_end_gain: float | None = None
    sample_quality: str = "thin"  # ok | thin
    tenant_id: str = "default"
    ledger_scope: str = "demo"
    as_of: str = ""
    version: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.as_of:
            self.as_of = utc_now_iso()
        self.band_key = self.band_key or band_label(self.band)
        self.sample_quality = "ok" if self.n >= MIN_OK_SAMPLES else "thin"

    def doc_id(self) -> str:
        return (
            f"{self.tenant_id}|{self.ledger_scope}|{self.symbol}|"
            f"{self.timeframe}|{self.band_key}"
        )

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["_id"] = self.doc_id()
        return d

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> PathBandSummary | None:
        if not doc:
            return None
        raw = {k: v for k, v in doc.items() if k != "_id" and k in cls.__dataclass_fields__}
        try:
            return cls(**raw)
        except Exception:
            return None


def _median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    return float(statistics.median(xs))


def extract_episodes(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    bands: Sequence[float] = DEFAULT_BANDS,
    trough_lookback: int = DEFAULT_TROUGH_LOOKBACK,
    forward_bars: int = DEFAULT_FORWARD_BARS,
    trail_hit: float = DEFAULT_TRAIL_HIT,
    ext_hit: float = DEFAULT_EXT_HIT,
) -> list[PathEpisode]:
    """Detect peak-gain first-touch episodes and measure forward outcomes.

    Trough = min(low) over [t-lookback, t]. Peak from trough index to t.
    Arm when peak/trough-1 first crosses a band threshold.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < trough_lookback + forward_bars + 2:
        return []

    highs_f = [float(x) for x in highs[:n]]
    lows_f = [float(x) for x in lows[:n]]
    closes_f = [float(x) for x in closes[:n]]
    bands_sorted = sorted(float(b) for b in bands if b > 0)

    episodes: list[PathEpisode] = []
    # Per band: last arm index to avoid re-arm spam in same climb
    last_arm_at: dict[float, int] = {b: -10**9 for b in bands_sorted}
    cooldown = max(forward_bars // 2, 6)

    for t in range(trough_lookback, n - forward_bars):
        window_start = t - trough_lookback
        trough_idx = window_start
        trough = lows_f[window_start]
        for j in range(window_start, t + 1):
            if lows_f[j] <= trough:
                trough = lows_f[j]
                trough_idx = j
        if trough <= 0:
            continue

        peak = highs_f[trough_idx]
        for j in range(trough_idx, t + 1):
            if highs_f[j] > peak:
                peak = highs_f[j]
        if peak <= trough:
            continue

        gain = peak / trough - 1.0
        # previous bar gain for first-touch
        if t == 0:
            prev_gain = 0.0
        else:
            peak_prev = highs_f[trough_idx]
            for j in range(trough_idx, t):
                if highs_f[j] > peak_prev:
                    peak_prev = highs_f[j]
            prev_gain = peak_prev / trough - 1.0 if peak_prev > trough else 0.0

        for band in bands_sorted:
            if gain < band or prev_gain >= band:
                continue
            if t - last_arm_at[band] < cooldown:
                continue
            last_arm_at[band] = t

            end = t + forward_bars
            seg_high = max(highs_f[t : end + 1])
            seg_low = min(lows_f[t : end + 1])
            max_giveback = max(0.0, 1.0 - (seg_low / peak)) if peak > 0 else 0.0
            hit_trail = seg_low <= peak * (1.0 - trail_hit)
            hit_extension = seg_high >= peak * (1.0 + ext_hit)
            end_gain = closes_f[end] / trough - 1.0

            episodes.append(
                PathEpisode(
                    arm_index=t,
                    trough=trough,
                    peak_at_arm=peak,
                    band=band,
                    max_giveback=max_giveback,
                    hit_trail=hit_trail,
                    hit_extension=hit_extension,
                    end_gain_from_trough=end_gain,
                )
            )

    return episodes


def summarize_episodes(
    symbol: str,
    timeframe: str,
    episodes: Iterable[PathEpisode],
    *,
    tenant_id: str = "default",
    ledger_scope: str = "demo",
    meta: dict[str, Any] | None = None,
) -> list[PathBandSummary]:
    by_band: dict[float, list[PathEpisode]] = {}
    for ep in episodes:
        by_band.setdefault(ep.band, []).append(ep)

    out: list[PathBandSummary] = []
    for band, eps in sorted(by_band.items()):
        givebacks = [e.max_giveback for e in eps]
        end_gains = [e.end_gain_from_trough for e in eps]
        n = len(eps)
        p_trail = sum(1 for e in eps if e.hit_trail) / n if n else None
        p_ext = sum(1 for e in eps if e.hit_extension) / n if n else None
        out.append(
            PathBandSummary(
                symbol=symbol,
                timeframe=timeframe,
                band=band,
                band_key=band_label(band),
                n=n,
                median_max_giveback=_median(givebacks),
                p_hit_trail=p_trail,
                p_hit_extension=p_ext,
                median_end_gain=_median(end_gains),
                tenant_id=tenant_id,
                ledger_scope=ledger_scope,
                meta=dict(meta or {}),
            )
        )
    return out


def compute_path_stats_for_ohlcv(
    symbol: str,
    timeframe: str,
    ohlcv_rows: Sequence[Sequence[Any]],
    *,
    tenant_id: str = "default",
    ledger_scope: str = "demo",
    bands: Sequence[float] = DEFAULT_BANDS,
    trough_lookback: int = DEFAULT_TROUGH_LOOKBACK,
    forward_bars: int = DEFAULT_FORWARD_BARS,
) -> list[PathBandSummary]:
    """ohlcv_rows: exchange-style [ts, open, high, low, close, volume?]."""
    if not ohlcv_rows:
        return []
    highs, lows, closes = [], [], []
    for row in ohlcv_rows:
        if len(row) < 5:
            continue
        highs.append(float(row[2]))
        lows.append(float(row[3]))
        closes.append(float(row[4]))
    eps = extract_episodes(
        highs,
        lows,
        closes,
        bands=bands,
        trough_lookback=trough_lookback,
        forward_bars=forward_bars,
    )
    return summarize_episodes(
        symbol,
        timeframe,
        eps,
        tenant_id=tenant_id,
        ledger_scope=ledger_scope,
        meta={"bars": len(closes), "episodes_total": len(eps)},
    )


def upsert_path_summaries(
    summaries: Sequence[PathBandSummary],
    *,
    config: dict | None = None,
    force: bool = False,
) -> int:
    """Write summaries to memory_path_stats. Returns write count. No-op if disabled."""
    if not force and not path_stats_enabled(config):
        log("path_stats write skipped (disabled)", "DEBUG")
        return 0
    try:
        from storage.mongo_client import get_database

        col = get_database()[COL_PATH_STATS]
        n = 0
        for s in summaries:
            col.replace_one({"_id": s.doc_id()}, s.to_doc(), upsert=True)
            n += 1
        return n
    except Exception as e:
        log(f"path_stats upsert failed: {e}", "WARNING")
        return 0


def get_path_summary(
    symbol: str,
    *,
    timeframe: str = "1h",
    band: float = 0.10,
    tenant_id: str = "default",
    ledger_scope: str | None = None,
    config: dict | None = None,
) -> PathBandSummary | None:
    """Read one summary. Returns None if disabled or missing (fail-open)."""
    if not path_stats_enabled(config):
        return None
    try:
        from intelligence.memory.store import resolve_memory_scope
        from storage.mongo_client import get_database

        scope = resolve_memory_scope(ledger_scope)
        _id = f"{tenant_id}|{scope}|{symbol}|{timeframe}|{band_label(band)}"
        doc = get_database()[COL_PATH_STATS].find_one({"_id": _id})
        return PathBandSummary.from_doc(doc)
    except Exception as e:
        log(f"path_stats get failed: {e}", "DEBUG")
        return None


def list_path_summaries_for_symbol(
    symbol: str,
    *,
    timeframe: str = "1h",
    tenant_id: str = "default",
    ledger_scope: str | None = None,
    config: dict | None = None,
) -> list[PathBandSummary]:
    if not path_stats_enabled(config):
        return []
    try:
        from intelligence.memory.store import resolve_memory_scope
        from storage.mongo_client import get_database

        scope = resolve_memory_scope(ledger_scope)
        prefix = f"{tenant_id}|{scope}|{symbol}|{timeframe}|"
        cur = get_database()[COL_PATH_STATS].find({"_id": {"$regex": f"^{prefix}"}})
        out = []
        for doc in cur:
            s = PathBandSummary.from_doc(doc)
            if s:
                out.append(s)
        return sorted(out, key=lambda x: x.band)
    except Exception as e:
        log(f"path_stats list failed: {e}", "DEBUG")
        return []
