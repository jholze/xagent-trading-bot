"""Gate venue quality: 24h quote volume, spread, top-of-book (sensor-entry-guard).

Pure evaluate() is network-free for unit tests. Live fetch uses Gate bulk tickers.
Sells must never be blocked by venue quality (callers only use this on BUY paths).
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from logger import log

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "exchange": "gate",
    "min_quote_volume_24h_usdt": 50_000.0,
    "max_spread_pct": 1.5,
    "min_top_book_usdt_per_side": 200.0,
    "min_volume_to_order_multiple": 20.0,
    "apply_to": ["entry_sensor_15m", "vol_spike_15m", "grid_new_entry"],
    "cache_ttl_sec": 90.0,
    "on_fetch_error": "block_sensor",  # block_sensor | allow
}

_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, "VenueMetrics"]] = {}


@dataclass(frozen=True)
class VenueMetrics:
    symbol: str
    quote_volume_24h_usdt: float = 0.0
    base_volume_24h: float = 0.0
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    spread_pct: float = 0.0
    top_book_bid_usdt: float = 0.0
    top_book_ask_usdt: float = 0.0
    exchange: str = "gate"
    capture: str = "ok"  # ok | missing

    def to_stamp(self, *, planned_usdt: float = 0.0, venue_ok: bool | None = None, reasons: list[str] | None = None) -> dict[str, Any]:
        vol_to_order = 0.0
        if planned_usdt > 0 and self.quote_volume_24h_usdt > 0:
            vol_to_order = self.quote_volume_24h_usdt / planned_usdt
        out = {
            "exchange": self.exchange,
            "quote_volume_24h_usdt": round(self.quote_volume_24h_usdt, 4),
            "base_volume_24h": round(self.base_volume_24h, 6),
            "last": self.last,
            "bid": self.bid,
            "ask": self.ask,
            "spread_pct": round(self.spread_pct, 4),
            "top_book_bid_usdt": round(self.top_book_bid_usdt, 4),
            "top_book_ask_usdt": round(self.top_book_ask_usdt, 4),
            "planned_usdt": float(planned_usdt or 0),
            "volume_to_order_mult": round(vol_to_order, 4),
            "capture": self.capture,
        }
        if venue_ok is not None:
            out["venue_ok"] = bool(venue_ok)
        if reasons is not None:
            out["venue_reasons"] = list(reasons)
        return out


@dataclass
class VenueQualityResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    metrics: VenueMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "metrics": asdict(self.metrics) if self.metrics else None,
        }


def venue_quality_config(config_raw: dict | None = None) -> dict[str, Any]:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    risk = (config_raw or {}).get("risk") or {}
    raw = risk.get("venue_quality") or {}
    if not isinstance(raw, dict):
        raw = {}
    merged = {**_DEFAULTS, **raw}
    merged["enabled"] = bool(merged.get("enabled", True))
    return merged


def compute_spread_pct(bid: float, ask: float) -> float:
    bid = float(bid or 0)
    ask = float(ask or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return 999.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 999.0
    return (ask - bid) / mid * 100.0


def evaluate_venue_quality(
    metrics: VenueMetrics | dict[str, Any] | None,
    cfg: dict | None = None,
    *,
    planned_usdt: float = 0.0,
) -> VenueQualityResult:
    """Pure venue gate. Thin → ok=False with reasons. Missing metrics → not ok."""
    cfg = {**_DEFAULTS, **(cfg or {})}
    if not cfg.get("enabled", True):
        return VenueQualityResult(ok=True, reasons=["venue_quality_disabled"])

    if metrics is None:
        return VenueQualityResult(ok=False, reasons=["venue_metrics_missing"])

    if isinstance(metrics, dict):
        if metrics.get("capture") == "missing":
            return VenueQualityResult(
                ok=False,
                reasons=["venue_capture_missing"],
                metrics=VenueMetrics(symbol=str(metrics.get("symbol") or "?"), capture="missing"),
            )
        m = VenueMetrics(
            symbol=str(metrics.get("symbol") or "?"),
            quote_volume_24h_usdt=float(metrics.get("quote_volume_24h_usdt") or 0),
            base_volume_24h=float(metrics.get("base_volume_24h") or 0),
            last=float(metrics.get("last") or 0),
            bid=float(metrics.get("bid") or 0),
            ask=float(metrics.get("ask") or 0),
            bid_size=float(metrics.get("bid_size") or 0),
            ask_size=float(metrics.get("ask_size") or 0),
            spread_pct=float(metrics.get("spread_pct") or 0),
            top_book_bid_usdt=float(metrics.get("top_book_bid_usdt") or 0),
            top_book_ask_usdt=float(metrics.get("top_book_ask_usdt") or 0),
            exchange=str(metrics.get("exchange") or "gate"),
            capture=str(metrics.get("capture") or "ok"),
        )
    else:
        m = metrics

    if m.capture == "missing":
        return VenueQualityResult(ok=False, reasons=["venue_capture_missing"], metrics=m)

    reasons: list[str] = []
    min_qv = float(cfg.get("min_quote_volume_24h_usdt") or 0)
    if min_qv > 0 and m.quote_volume_24h_usdt < min_qv:
        reasons.append(
            f"quote_vol_24h ${m.quote_volume_24h_usdt:.0f} < min ${min_qv:.0f}"
        )

    max_spread = float(cfg.get("max_spread_pct") or 0)
    spread = m.spread_pct if m.spread_pct > 0 else compute_spread_pct(m.bid, m.ask)
    if max_spread > 0 and spread > max_spread:
        reasons.append(f"spread {spread:.2f}% > max {max_spread:.2f}%")

    min_book = float(cfg.get("min_top_book_usdt_per_side") or 0)
    bid_usdt = m.top_book_bid_usdt or (m.bid * m.bid_size)
    ask_usdt = m.top_book_ask_usdt or (m.ask * m.ask_size)
    if min_book > 0:
        if bid_usdt < min_book:
            reasons.append(f"bid book ${bid_usdt:.0f} < min ${min_book:.0f}")
        if ask_usdt < min_book:
            reasons.append(f"ask book ${ask_usdt:.0f} < min ${min_book:.0f}")

    planned = float(planned_usdt or 0)
    k = float(cfg.get("min_volume_to_order_multiple") or 0)
    if k > 0 and planned > 0:
        if m.quote_volume_24h_usdt < k * planned:
            reasons.append(
                f"quote_vol ${m.quote_volume_24h_usdt:.0f} < {k:.0f}× order ${planned:.0f}"
            )

    return VenueQualityResult(ok=len(reasons) == 0, reasons=reasons, metrics=m)


def is_thin_venue_stamp(stamp: dict | None, cfg: dict | None = None) -> bool:
    """Whether a fill-time venue stamp counts as thin (for memory learning)."""
    if not stamp or stamp.get("capture") == "missing":
        return False
    if stamp.get("venue_ok") is False:
        return True
    m = VenueMetrics(
        symbol="?",
        quote_volume_24h_usdt=float(stamp.get("quote_volume_24h_usdt") or 0),
        bid=float(stamp.get("bid") or 0),
        ask=float(stamp.get("ask") or 0),
        spread_pct=float(stamp.get("spread_pct") or 0),
        top_book_bid_usdt=float(stamp.get("top_book_bid_usdt") or 0),
        top_book_ask_usdt=float(stamp.get("top_book_ask_usdt") or 0),
        capture="ok",
    )
    planned = float(stamp.get("planned_usdt") or 0)
    return not evaluate_venue_quality(m, cfg, planned_usdt=planned).ok


def source_applies_venue(source: str, cfg: dict | None = None) -> bool:
    cfg = cfg or venue_quality_config()
    apply = cfg.get("apply_to") or _DEFAULTS["apply_to"]
    src = (source or "").lower()
    if src in {str(a).lower() for a in apply}:
        return True
    # aliases
    if src in ("entry_sensor_15m", "vol_spike_15m", "entry_sensor") and any(
        "sensor" in str(a).lower() or "vol_spike" in str(a).lower() for a in apply
    ):
        return True
    return False


def _pair(symbol: str) -> str:
    return symbol.replace("/", "_").upper()


def metrics_from_gate_ticker_row(symbol: str, row: dict) -> VenueMetrics:
    last = float(row.get("last") or 0)
    bid = float(row.get("highest_bid") or 0)
    ask = float(row.get("lowest_ask") or 0)
    bid_size = float(row.get("highest_size") or 0)
    ask_size = float(row.get("lowest_size") or 0)
    qv = float(row.get("quote_volume") or 0)
    bv = float(row.get("base_volume") or 0)
    return VenueMetrics(
        symbol=symbol,
        quote_volume_24h_usdt=qv,
        base_volume_24h=bv,
        last=last,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        spread_pct=compute_spread_pct(bid, ask),
        top_book_bid_usdt=bid * bid_size,
        top_book_ask_usdt=ask * ask_size,
        exchange="gate",
        capture="ok",
    )


def fetch_gate_venue_metrics(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
    force: bool = False,
) -> dict[str, VenueMetrics]:
    """Fetch venue metrics for symbols (bulk Gate tickers + short TTL cache)."""
    cfg = venue_quality_config(config_raw)
    ttl = float(cfg.get("cache_ttl_sec") or 90)
    now = time.time()
    unique = list(dict.fromkeys(symbols))
    out: dict[str, VenueMetrics] = {}
    missing: list[str] = []

    with _cache_lock:
        for sym in unique:
            hit = _cache.get(sym)
            if not force and hit and now - hit[0] <= ttl:
                out[sym] = hit[1]
            else:
                missing.append(sym)

    if not missing:
        return out

    pairs = {_pair(s): s for s in missing}
    try:
        import requests

        resp = requests.get("https://api.gateio.ws/api/v4/spot/tickers", timeout=12)
        if resp.status_code != 200:
            raise RuntimeError(f"gate tickers HTTP {resp.status_code}")
        for item in resp.json():
            pair = item.get("currency_pair", "")
            if pair not in pairs:
                continue
            sym = pairs[pair]
            m = metrics_from_gate_ticker_row(sym, item)
            out[sym] = m
            with _cache_lock:
                _cache[sym] = (now, m)
    except Exception as e:
        log(f"venue_quality fetch failed: {e}", "WARNING")
        for sym in missing:
            if sym not in out:
                out[sym] = VenueMetrics(symbol=sym, capture="missing")

    for sym in missing:
        if sym not in out:
            out[sym] = VenueMetrics(symbol=sym, capture="missing")
    return out


def get_venue_metrics(
    symbol: str,
    *,
    config_raw: dict | None = None,
    force: bool = False,
) -> VenueMetrics:
    return fetch_gate_venue_metrics([symbol], config_raw=config_raw, force=force).get(
        symbol
    ) or VenueMetrics(symbol=symbol, capture="missing")


def check_venue_for_buy(
    symbol: str,
    *,
    source: str = "entry_sensor_15m",
    planned_usdt: float = 0.0,
    config_raw: dict | None = None,
    metrics: VenueMetrics | dict | None = None,
) -> VenueQualityResult:
    """Defense-in-depth BUY gate. Call only for buy orders."""
    cfg = venue_quality_config(config_raw)
    if not cfg.get("enabled", True):
        return VenueQualityResult(ok=True, reasons=["venue_quality_disabled"])
    if not source_applies_venue(source, cfg):
        return VenueQualityResult(ok=True, reasons=["source_not_in_apply_to"])

    if metrics is None:
        metrics = get_venue_metrics(symbol, config_raw=config_raw)
        if metrics.capture == "missing":
            err_pol = str(cfg.get("on_fetch_error") or "block_sensor")
            if err_pol in ("allow", "fail_open"):
                return VenueQualityResult(
                    ok=True, reasons=["venue_fetch_failed_allow"], metrics=metrics
                )
            return VenueQualityResult(
                ok=False, reasons=["venue_fetch_failed_block"], metrics=metrics
            )

    return evaluate_venue_quality(metrics, cfg, planned_usdt=planned_usdt)


def stamp_venue_for_fill(
    symbol: str,
    *,
    planned_usdt: float = 0.0,
    config_raw: dict | None = None,
    metrics: VenueMetrics | None = None,
) -> dict[str, Any]:
    """Build execution.venue stamp for a filled buy (always attach something)."""
    cfg = venue_quality_config(config_raw)
    m = metrics or get_venue_metrics(symbol, config_raw=config_raw)
    if m.capture == "missing":
        return {
            "capture": "missing",
            "exchange": cfg.get("exchange") or "gate",
            "symbol": symbol,
            "planned_usdt": float(planned_usdt or 0),
        }
    result = evaluate_venue_quality(m, cfg, planned_usdt=planned_usdt)
    return m.to_stamp(
        planned_usdt=planned_usdt,
        venue_ok=result.ok,
        reasons=result.reasons,
    )


def reset_venue_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
