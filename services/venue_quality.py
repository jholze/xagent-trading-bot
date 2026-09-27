"""Gate venue quality: 24h quote volume, spread, top-of-book (sensor-entry-guard).

Pure evaluate() is network-free for unit tests. Live fetch uses Gate bulk tickers
plus an opt-in public order-book depth call. HTTP lives in one function so #580
can wrap it. Sells must never be blocked by venue quality (callers only use this
on BUY paths).
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from logger import log

GATE_PUBLIC_REST_BASE = "https://api.gateio.ws/api/v4"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "exchange": "gate",
    "min_quote_volume_24h_usdt": 50_000.0,
    "max_spread_pct": 1.5,
    "min_top_book_usdt_per_side": 200.0,
    "min_volume_to_order_multiple": 20.0,
    "apply_to": ["entry_sensor_15m", "vol_spike_15m", "grid_new_entry"],
    "exempt_sources": ["manual"],
    "cache_ttl_sec": 90.0,
    "on_fetch_error": "block_sensor",  # block_sensor | allow
    "depth_levels": 5,
    "order_book_limit": 20,
    "book_unavailable_policy": "volume_ok",
    "order_book_timeout_sec": 5.0,
    "order_book_cache_ttl_sec": 15.0,
}

_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, "VenueMetrics"]] = {}
_book_cache: dict[str, tuple[float, tuple[float, float, bool]]] = {}


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
    capture: str = "ok"  # ok | missing | book_unavailable
    depth_bid_usdt: float = 0.0
    depth_ask_usdt: float = 0.0
    depth_parsed: bool = False
    quote_volume_present: bool = True
    size_keys_present: bool = True

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
            "depth_bid_usdt": round(self.depth_bid_usdt, 4),
            "depth_ask_usdt": round(self.depth_ask_usdt, 4),
            "depth_parsed": bool(self.depth_parsed),
            "planned_usdt": float(planned_usdt or 0),
            "volume_to_order_mult": round(vol_to_order, 4),
            "capture": self.capture,
            "quote_volume_present": bool(self.quote_volume_present),
            "size_keys_present": bool(self.size_keys_present),
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
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "metrics": asdict(self.metrics) if self.metrics else None,
            "code": self.code,
        }


def gate_public_rest_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 12.0,
) -> Any:
    """Single public Gate REST GET for this gate (no auth). Isolated for #580."""
    import requests

    if not str(path).startswith("/"):
        path = "/" + str(path)
    url = GATE_PUBLIC_REST_BASE + path
    resp = requests.get(url, params=params, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"gate {path} HTTP {resp.status_code}")
    return resp.json()


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


def _parse_optional_number(row: dict, key: str) -> tuple[float, bool]:
    """Return (value, present_and_numeric). Missing / blank / non-numeric → not present."""
    if not isinstance(row, dict) or key not in row:
        return 0.0, False
    raw = row[key]
    if raw is None or raw == "":
        return 0.0, False
    try:
        return float(raw), True
    except (TypeError, ValueError):
        return 0.0, False


def _metrics_from_dict(metrics: dict[str, Any]) -> VenueMetrics:
    return VenueMetrics(
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
        depth_bid_usdt=float(metrics.get("depth_bid_usdt") or 0),
        depth_ask_usdt=float(metrics.get("depth_ask_usdt") or 0),
        depth_parsed=bool(metrics.get("depth_parsed") or False),
        quote_volume_present=bool(metrics.get("quote_volume_present", True)),
        size_keys_present=bool(metrics.get("size_keys_present", True)),
    )


def _reject_code(reasons: list[str]) -> str:
    if reasons == ["book_unavailable"] or (
        reasons and all(r == "book_unavailable" for r in reasons)
    ):
        return "book_unavailable"
    return "venue_liquidity_block"


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
                code="venue_liquidity_block",
            )
        m = _metrics_from_dict(metrics)
    else:
        m = metrics

    if m.capture == "missing":
        return VenueQualityResult(
            ok=False, reasons=["venue_capture_missing"], metrics=m, code="venue_liquidity_block"
        )

    reasons: list[str] = []
    volume_data = bool(getattr(m, "quote_volume_present", True))
    min_qv = float(cfg.get("min_quote_volume_24h_usdt") or 0)
    if min_qv > 0 and volume_data and m.quote_volume_24h_usdt < min_qv:
        reasons.append(
            f"quote_vol_24h ${m.quote_volume_24h_usdt:.0f} < min ${min_qv:.0f}"
        )

    max_spread = float(cfg.get("max_spread_pct") or 0)
    spread = m.spread_pct if m.spread_pct > 0 else compute_spread_pct(m.bid, m.ask)
    if max_spread > 0 and spread > max_spread:
        reasons.append(f"spread {spread:.2f}% > max {max_spread:.2f}%")

    min_book = float(cfg.get("min_top_book_usdt_per_side") or 0)
    depth_parsed = bool(getattr(m, "depth_parsed", False))
    if min_book > 0:
        if depth_parsed:
            bid_usdt = float(m.depth_bid_usdt or 0)
            ask_usdt = float(m.depth_ask_usdt or 0)
            if bid_usdt < min_book:
                reasons.append(f"bid book ${bid_usdt:.0f} < min ${min_book:.0f}")
            if ask_usdt < min_book:
                reasons.append(f"ask book ${ask_usdt:.0f} < min ${min_book:.0f}")
        elif m.capture != "book_unavailable":
            bid_usdt = m.top_book_bid_usdt or (m.bid * m.bid_size)
            ask_usdt = m.top_book_ask_usdt or (m.ask * m.ask_size)
            if bid_usdt < min_book:
                reasons.append(f"bid book ${bid_usdt:.0f} < min ${min_book:.0f}")
            if ask_usdt < min_book:
                reasons.append(f"ask book ${ask_usdt:.0f} < min ${min_book:.0f}")

    planned = float(planned_usdt or 0)
    k = float(cfg.get("min_volume_to_order_multiple") or 0)
    if k > 0 and planned > 0 and volume_data:
        if m.quote_volume_24h_usdt < k * planned:
            reasons.append(
                f"quote_vol ${m.quote_volume_24h_usdt:.0f} < {k:.0f}× order ${planned:.0f}"
            )

    if m.capture == "book_unavailable" and not depth_parsed:
        if not volume_data:
            reasons.append("book_unavailable")
            return VenueQualityResult(
                ok=False,
                reasons=reasons,
                metrics=m,
                code="book_unavailable",
            )
        policy = str(cfg.get("book_unavailable_policy") or "volume_ok")
        vol_ok = not any(r.startswith("quote_vol") for r in reasons)
        if policy == "volume_ok" and vol_ok and not reasons:
            log(
                f"book_unavailable_volume_ok {m.symbol} "
                f"quote_vol={m.quote_volume_24h_usdt:.0f}",
                "WARNING",
            )
            return VenueQualityResult(
                ok=True,
                reasons=["book_unavailable_volume_ok"],
                metrics=m,
                code="",
            )
        # Fail closed: only the recognised volume_ok policy may skip the book
        # check. "block", a typo, or any other value must not pass.
        # Volume is present here (absent case returned above) → contract
        # call 2: venue_liquidity_block, not book_unavailable.
        if policy != "volume_ok":
            reasons.append("book_unavailable")
            return VenueQualityResult(
                ok=False,
                reasons=reasons,
                metrics=m,
                code="venue_liquidity_block",
            )

    # Missing quote_volume is not a measured $0. Fail closed unless this is the
    # both-absent book_unavailable branch (handled above).
    if not volume_data:
        reasons.append("quote_vol_24h unavailable")

    code = _reject_code(reasons) if reasons else ""
    return VenueQualityResult(ok=len(reasons) == 0, reasons=reasons, metrics=m, code=code)


def is_thin_venue_stamp(stamp: dict | None, cfg: dict | None = None) -> bool:
    """Whether a fill-time venue stamp counts as thin (for memory learning)."""
    if not stamp or stamp.get("capture") == "missing":
        return False
    if stamp.get("capture") == "book_unavailable":
        return stamp.get("venue_ok") is False
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
        capture=str(stamp.get("capture") or "ok"),
        depth_bid_usdt=float(stamp.get("depth_bid_usdt") or 0),
        depth_ask_usdt=float(stamp.get("depth_ask_usdt") or 0),
        depth_parsed=bool(stamp.get("depth_parsed") or False),
        quote_volume_present=bool(stamp.get("quote_volume_present", True)),
        size_keys_present=bool(stamp.get("size_keys_present", True)),
    )
    planned = float(stamp.get("planned_usdt") or 0)
    return not evaluate_venue_quality(m, cfg, planned_usdt=planned).ok


def source_applies_venue(source: str, cfg: dict | None = None) -> bool:
    """True unless source is an exact ``exempt_sources`` entry.

    ``apply_to`` remains in config but is not an allowlist: an unknown source
    is evaluated. Only the exact string ``manual`` is exempt by default.
    """
    if cfg is None:
        cfg = venue_quality_config()
    exempt = cfg.get("exempt_sources")
    if exempt is None:
        exempt = _DEFAULTS["exempt_sources"]
    if isinstance(exempt, str):
        exempt = [exempt]
    if not isinstance(exempt, (list, tuple, set)):
        exempt = _DEFAULTS["exempt_sources"]
    return str(source or "") not in {str(item) for item in exempt}


def _pair(symbol: str) -> str:
    return symbol.replace("/", "_").upper()


def metrics_from_gate_ticker_row(symbol: str, row: dict) -> VenueMetrics:
    last = float(row.get("last") or 0)
    bid = float(row.get("highest_bid") or 0)
    ask = float(row.get("lowest_ask") or 0)
    bid_size, bid_size_ok = _parse_optional_number(row, "highest_size")
    ask_size, ask_size_ok = _parse_optional_number(row, "lowest_size")
    qv, qv_ok = _parse_optional_number(row, "quote_volume")
    bv = float(row.get("base_volume") or 0)
    size_ok = bid_size_ok and ask_size_ok
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
        top_book_bid_usdt=(bid * bid_size) if size_ok else 0.0,
        top_book_ask_usdt=(ask * ask_size) if size_ok else 0.0,
        exchange="gate",
        capture="ok" if size_ok else "book_unavailable",
        quote_volume_present=qv_ok,
        size_keys_present=size_ok,
    )


def depth_from_gate_order_book(
    payload: Any, *, depth_levels: int = 5
) -> tuple[float, float, bool]:
    """Sum notional of the top N bid/ask levels. Raises if the payload is unusable."""
    if not isinstance(payload, dict):
        raise TypeError("order_book payload must be a dict")
    bids = payload.get("bids")
    asks = payload.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise TypeError("order_book payload missing bids/asks lists")
    # Empty lists are unparsed, not a measured $0 book. A present level with
    # size 0 still parses (explicit zero on a present key).
    if not bids and not asks:
        return 0.0, 0.0, False
    n = max(int(depth_levels or 5), 1)

    def _side(levels: list) -> float:
        total = 0.0
        for row in levels[:n]:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                px = float(row[0])
                sz = float(row[1])
            except (TypeError, ValueError):
                continue
            if px > 0 and sz >= 0:
                total += px * sz
        return total

    return _side(bids), _side(asks), True


def _with_depth(
    m: VenueMetrics, bid_d: float, ask_d: float, parsed: bool
) -> VenueMetrics:
    cap = m.capture
    if parsed and cap == "book_unavailable":
        cap = "ok"
    return replace(
        m,
        depth_bid_usdt=float(bid_d or 0),
        depth_ask_usdt=float(ask_d or 0),
        depth_parsed=bool(parsed),
        capture=cap,
    )


def _attach_order_book_depth(m: VenueMetrics, cfg: dict) -> VenueMetrics:
    depth_levels = int(cfg.get("depth_levels") or 5)
    limit = int(cfg.get("order_book_limit") or 20)
    timeout = float(cfg.get("order_book_timeout_sec") or 5.0)
    ttl = float(cfg.get("order_book_cache_ttl_sec") or 15.0)
    pair = _pair(m.symbol)
    now = time.time()

    with _cache_lock:
        hit = _book_cache.get(pair)
        if hit and now - hit[0] <= ttl:
            bid_d, ask_d, parsed = hit[1]
            return _with_depth(m, bid_d, ask_d, parsed)

    try:
        payload = gate_public_rest_get(
            "/spot/order_book",
            {"currency_pair": pair, "limit": limit},
            timeout=timeout,
        )
        bid_d, ask_d, parsed = depth_from_gate_order_book(
            payload, depth_levels=depth_levels
        )
        with _cache_lock:
            _book_cache[pair] = (now, (bid_d, ask_d, parsed))
        return _with_depth(m, bid_d, ask_d, parsed)
    except Exception as e:
        log(f"venue_quality order_book failed {m.symbol}: {e}", "WARNING")

    try:
        payload = gate_public_rest_get(
            "/spot/tickers",
            {"currency_pair": pair},
            timeout=timeout,
        )
        row = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(row, dict):
            raise TypeError("pair ticker payload is not a row")
        one = metrics_from_gate_ticker_row(m.symbol, row)
        if one.capture == "book_unavailable":
            raise RuntimeError("pair ticker also missing size keys")
        return replace(
            m,
            bid=one.bid or m.bid,
            ask=one.ask or m.ask,
            bid_size=one.bid_size,
            ask_size=one.ask_size,
            top_book_bid_usdt=one.top_book_bid_usdt,
            top_book_ask_usdt=one.top_book_ask_usdt,
            spread_pct=one.spread_pct or m.spread_pct,
            capture="ok",
            size_keys_present=True,
        )
    except Exception as e:
        log(f"venue_quality pair ticker fallback failed {m.symbol}: {e}", "WARNING")
    return m


def fetch_gate_venue_metrics(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
    force: bool = False,
    fetch_depth: bool = False,
) -> dict[str, VenueMetrics]:
    """Fetch venue metrics for symbols (bulk Gate tickers + short TTL cache).

    ``fetch_depth`` is opt-in. Watchlist quote-volume attachment must leave it
    off so this call does not fan out per-pair order-book HTTP.
    """
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

    if missing:
        pairs = {_pair(s): s for s in missing}
        try:
            data = gate_public_rest_get("/spot/tickers", timeout=12)
            for item in data:
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

    if fetch_depth:
        for sym in unique:
            m = out.get(sym)
            if m is None or m.capture == "missing":
                continue
            if m.depth_parsed:
                continue
            resolved = _attach_order_book_depth(m, cfg)
            out[sym] = resolved
            with _cache_lock:
                _cache[sym] = (now, resolved)
    return out


def get_venue_metrics(
    symbol: str,
    *,
    config_raw: dict | None = None,
    force: bool = False,
    fetch_depth: bool = True,
) -> VenueMetrics:
    return fetch_gate_venue_metrics(
        [symbol], config_raw=config_raw, force=force, fetch_depth=fetch_depth
    ).get(symbol) or VenueMetrics(symbol=symbol, capture="missing")


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
        return VenueQualityResult(ok=True, reasons=["source_exempt"])

    if metrics is None:
        metrics = get_venue_metrics(symbol, config_raw=config_raw, fetch_depth=True)
        if metrics.capture == "missing":
            err_pol = str(cfg.get("on_fetch_error") or "block_sensor")
            if err_pol in ("allow", "fail_open"):
                return VenueQualityResult(
                    ok=True, reasons=["venue_fetch_failed_allow"], metrics=metrics
                )
            return VenueQualityResult(
                ok=False,
                reasons=["venue_fetch_failed_block"],
                metrics=metrics,
                code="venue_liquidity_block",
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
    m = metrics or get_venue_metrics(symbol, config_raw=config_raw, fetch_depth=True)
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
        _book_cache.clear()
