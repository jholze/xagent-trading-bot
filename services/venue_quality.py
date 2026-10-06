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
    # One fetch deep enough that a tight book on a large pair still fills
    # the ±0.5% band. If the returned book still ends inside the band, only
    # the fetched notional counts and a shortfall blocks.
    "order_book_limit": 100,
    "book_unavailable_policy": "volume_ok",
    "order_book_timeout_sec": 5.0,
    "order_book_cache_ttl_sec": 15.0,
}

_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, "VenueMetrics"]] = {}
_book_cache: dict[str, tuple[float, tuple]] = {}


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
    # Prices of levels inside the ±mid band. Empty when the book was not parsed.
    band_levels: tuple = ()

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
            "band_levels": [
                {"side": side, "price": price, "size": size}
                for side, price, size in self.band_levels
            ],
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
        # Non-200 is a failed fetch. Callers must not turn the body into a
        # measured $0 book. Gate's error object is {"label","message"}.
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("label"):
                detail = f" {body.get('label')}"
        except Exception:
            detail = ""
        raise RuntimeError(f"gate {path} HTTP {resp.status_code}{detail}")
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


# #641 hard liquidity lock. No off-switch. The 500k floor is Lena's scan
# threshold, not a backtested edge. A configured value below this is raised
# back up: lowering it takes a PR reviewed with Viktor and Jens' go.
_LIQ_FLOOR_MIN_USDT = 500_000.0
_LIQ_WINDOW_PCT_DEFAULT = 0.5


def liquidity_guard_config(config_raw: dict | None = None) -> dict[str, Any]:
    """Gate 24h quote-volume floor and ±% mid book window.

    Volume is the pair's own Gate ticker ``quote_volume``, never CMC.
    ``depth_window_pct`` is a percent of mid (0.5 means ±0.5%), not a
    fraction and not a level count.
    """
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    risk = (config_raw or {}).get("risk") or {}
    raw = risk.get("liquidity_guard") if isinstance(risk, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    try:
        floor = float(raw.get("min_quote_volume_24h_usdt") or _LIQ_FLOOR_MIN_USDT)
    except (TypeError, ValueError):
        floor = _LIQ_FLOOR_MIN_USDT
    if floor < _LIQ_FLOOR_MIN_USDT:
        floor = _LIQ_FLOOR_MIN_USDT
    try:
        window = float(raw.get("depth_window_pct", _LIQ_WINDOW_PCT_DEFAULT))
    except (TypeError, ValueError):
        window = _LIQ_WINDOW_PCT_DEFAULT
    if window <= 0:
        window = _LIQ_WINDOW_PCT_DEFAULT
    return {
        "min_quote_volume_24h_usdt": floor,
        "depth_window_pct": window,
    }


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


# Buy sources the proof test enumerates one by one. This is not an
# allowlist: ``source_applies_venue`` still checks every non-manual
# source, including one that is not in this tuple. A new buy source
# must be added here so the enumerated test fails until it is covered.
AUTOMATIC_BUY_SOURCES = (
    "auto",
    "climax_fade",
    "cmc",
    "dca",
    "dca_recovery",
    "dca_scheduled",
    "dca_sniper",
    "dca_sniper_deep",
    "dca_sniper_fund",
    "dca_sniper_ws",
    "deploy_boost",
    "entry_sensor_15m",
    "gainer_live_heat",
    "gainer_live_top",
    "gainer_rank_entry",
    "gainer_relvol",
    "gainer_signal",
    "grid",
    "grid_new_entry",
    "hermes",
    "lc",
    "mcp",
    "recovery",
    "technical",
    "vol_buy_boost",
    "vol_spike_15m",
    "webhook",
    "x",
)


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


def _best_level_price(levels: list) -> float:
    for row in levels:
        if not isinstance(row, (list, tuple)) or len(row) < 1:
            continue
        try:
            px = float(row[0])
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return 0.0


# A cached book older than this is not a book. Config cannot stretch it.
_BOOK_CACHE_MAX_AGE_SEC = 15.0


def _book_cache_max_age(cfg: dict | None) -> float:
    raw = (cfg or {}).get("order_book_cache_ttl_sec")
    try:
        ttl = float(raw if raw is not None else _BOOK_CACHE_MAX_AGE_SEC)
    except (TypeError, ValueError):
        ttl = _BOOK_CACHE_MAX_AGE_SEC
    if ttl <= 0 or ttl > _BOOK_CACHE_MAX_AGE_SEC:
        return _BOOK_CACHE_MAX_AGE_SEC
    return ttl


def _band_from_levels(
    payload: Any, *, window_pct: float = _LIQ_WINDOW_PCT_DEFAULT
) -> tuple[float, float, bool, tuple]:
    """Notional and per-level prices inside ±window_pct of mid.

    A level is ``[price, size]`` as Gate returns it. Unreadable levels are
    not a measured zero: the book is unparsed. Levels outside the band are
    ignored. An empty list is unparsed, not $0.
    """
    if not isinstance(payload, dict):
        raise TypeError("order_book payload must be a dict")
    bids = payload.get("bids")
    asks = payload.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise TypeError("order_book payload missing bids/asks lists")
    if not bids and not asks:
        return 0.0, 0.0, False, ()
    best_bid = _best_level_price(bids)
    best_ask = _best_level_price(asks)
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return 0.0, 0.0, False, ()
    mid = (best_bid + best_ask) / 2.0
    try:
        window = float(window_pct)
    except (TypeError, ValueError):
        window = _LIQ_WINDOW_PCT_DEFAULT
    if window <= 0:
        window = _LIQ_WINDOW_PCT_DEFAULT
    lo = mid * (1.0 - window / 100.0)
    hi = mid * (1.0 + window / 100.0)
    readable = 0
    kept: list[tuple[str, float, float]] = []

    def _side(levels: list, side: str) -> float:
        nonlocal readable
        total = 0.0
        for row in levels:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                px = float(row[0])
                sz = float(row[1])
            except (TypeError, ValueError):
                continue
            if px <= 0 or sz < 0:
                continue
            readable += 1
            if lo <= px <= hi:
                total += px * sz
                kept.append((side, px, sz))
        return total

    bid_d = _side(bids, "bid")
    ask_d = _side(asks, "ask")
    if readable == 0:
        return 0.0, 0.0, False, ()
    return bid_d, ask_d, True, tuple(kept)


def depth_within_mid_band(
    payload: Any, *, window_pct: float = _LIQ_WINDOW_PCT_DEFAULT
) -> tuple[float, float, bool]:
    """Sum notional on each side inside ±window_pct of mid.

    Mid is (best bid + best ask) / 2. Every readable level whose price sits
    in the band counts. There is no level-count cap. Empty or unreadable
    books are unparsed, not a measured $0.
    """
    bid_d, ask_d, parsed, _levels = _band_from_levels(payload, window_pct=window_pct)
    return bid_d, ask_d, parsed


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
    m: VenueMetrics,
    bid_d: float,
    ask_d: float,
    parsed: bool,
    levels: tuple = (),
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
        band_levels=tuple(levels or ()),
    )


def _attach_order_book_depth(m: VenueMetrics, cfg: dict) -> VenueMetrics:
    limit = int(cfg.get("order_book_limit") or 100)
    timeout = float(cfg.get("order_book_timeout_sec") or 5.0)
    ttl = _book_cache_max_age(cfg)
    window = float(liquidity_guard_config().get("depth_window_pct") or _LIQ_WINDOW_PCT_DEFAULT)
    pair = _pair(m.symbol)
    now = time.time()

    with _cache_lock:
        hit = _book_cache.get(pair)
        if hit and now - hit[0] <= ttl:
            bid_d, ask_d, parsed, levels = hit[1]
            return _with_depth(m, bid_d, ask_d, parsed, levels)

    try:
        payload = gate_public_rest_get(
            "/spot/order_book",
            {"currency_pair": pair, "limit": limit},
            timeout=timeout,
        )
        # #641: band around mid, not a fixed number of book levels.
        # A failed or unreadable payload must not become a parsed $0.
        bid_d, ask_d, parsed, levels = _band_from_levels(payload, window_pct=window)
        if parsed:
            prices = " ".join(f"{side}@{price:g}" for side, price, _sz in levels)
            log(
                f"venue_band_levels {m.symbol} window=±{window:g}% {prices}",
                "INFO",
            )
        with _cache_lock:
            _book_cache[pair] = (now, (bid_d, ask_d, parsed, levels))
        return _with_depth(m, bid_d, ask_d, parsed, levels)
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
                cached = hit[1]
                book = _book_cache.get(_pair(sym))
                book_fresh = bool(
                    book and now - book[0] <= _book_cache_max_age(cfg)
                )
                if cached.depth_parsed and not book_fresh:
                    # The ticker cache outlives the book. Do not keep a
                    # depth figure past the book max age.
                    cached = replace(
                        cached,
                        depth_parsed=False,
                        depth_bid_usdt=0.0,
                        depth_ask_usdt=0.0,
                        band_levels=(),
                    )
                out[sym] = cached
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


def _apply_hard_liquidity(
    prior: VenueQualityResult,
    metrics: VenueMetrics | dict | None,
    *,
    planned_usdt: float,
    config_raw: dict | None,
) -> VenueQualityResult:
    """Repair the #563 gate: Gate volume floor and band depth vs final size.

    Does not replace a prior hard failure with a pass. When the old
    thresholds would have allowed the buy, a failing hard rule becomes
    the result. Hard codes are also attached when the old gate already
    failed, so the tape can count ``liq_guard_*`` on its own.
    Sells must not call this.
    """
    lg = liquidity_guard_config(config_raw)
    m = prior.metrics
    if m is None and isinstance(metrics, VenueMetrics):
        m = metrics
    elif m is None and isinstance(metrics, dict):
        try:
            m = _metrics_from_dict(metrics)
        except Exception:
            m = None

    codes: list[str] = []
    reasons = list(prior.reasons)
    floor = float(lg["min_quote_volume_24h_usdt"])
    window = float(lg["depth_window_pct"])
    planned = float(planned_usdt or 0)
    qv = None
    ask_d = None
    bid_d = None

    capture = "" if m is None else str(getattr(m, "capture", "") or "")
    if m is None or capture in ("missing", "stale"):
        codes.append("liq_guard_missing_input")
    else:
        volume_known = bool(getattr(m, "quote_volume_present", True))
        if not volume_known:
            codes.append("liq_guard_missing_input")
        else:
            try:
                qv = float(m.quote_volume_24h_usdt)
            except (TypeError, ValueError):
                qv = None
            if qv is None:
                codes.append("liq_guard_missing_input")
            elif qv < floor:
                codes.append("liq_guard_volume_low")
                reasons.append(
                    f"quote_vol_24h ${qv:.0f} < liquidity floor ${floor:.0f}"
                )
        if not bool(getattr(m, "depth_parsed", False)):
            if "liq_guard_missing_input" not in codes:
                codes.append("liq_guard_missing_input")
                reasons.append("band depth unavailable")
        elif planned <= 0:
            if "liq_guard_missing_input" not in codes:
                codes.append("liq_guard_missing_input")
                reasons.append("planned size unavailable")
        else:
            try:
                bid_d = float(m.depth_bid_usdt or 0)
                ask_d = float(m.depth_ask_usdt or 0)
            except (TypeError, ValueError):
                bid_d = None
                ask_d = None
            if bid_d is None or ask_d is None:
                if "liq_guard_missing_input" not in codes:
                    codes.append("liq_guard_missing_input")
            elif ask_d < planned or bid_d < planned:
                codes.append("liq_guard_depth_lt_order")
                reasons.append(
                    f"order book too thin: band depth ask ${ask_d:.0f} "
                    f"bid ${bid_d:.0f} < order ${planned:.0f} "
                    f"(±{window:.2f}% mid)"
                )

    if not codes:
        return prior

    # When the old gate already rejected, keep that code first so a thin
    # book still reports venue_liquidity_block. Hard codes stay on the
    # list. When the old gate would have allowed the buy, the hard code
    # is primary.
    all_codes = list(codes)
    if prior.code and not prior.ok and prior.code not in all_codes:
        all_codes = [prior.code] + all_codes
    primary = all_codes[0]
    out = VenueQualityResult(ok=False, reasons=reasons, metrics=m, code=primary)
    out.guard_codes = all_codes  # type: ignore[attr-defined]
    out.planned_usdt = planned  # type: ignore[attr-defined]
    out.quote_volume_24h_usdt = qv  # type: ignore[attr-defined]
    out.depth_ask_usdt = ask_d  # type: ignore[attr-defined]
    out.depth_bid_usdt = bid_d  # type: ignore[attr-defined]
    out.depth_window_pct = window  # type: ignore[attr-defined]
    return out


def check_venue_for_buy(
    symbol: str,
    *,
    source: str = "entry_sensor_15m",
    planned_usdt: float = 0.0,
    config_raw: dict | None = None,
    metrics: VenueMetrics | dict | None = None,
) -> VenueQualityResult:
    """BUY gate (#563 repaired by #641). Call only for buy orders.

    A fetch error blocks the buy (``liq_guard_missing_input``). It does
    not raise and it does not apply to sells.
    """
    cfg = venue_quality_config(config_raw)
    if not cfg.get("enabled", True):
        # The old venue_quality.enabled flag is not an off-switch for the
        # hard lock. The lock still runs.
        pass
    if not source_applies_venue(source, cfg):
        return VenueQualityResult(ok=True, reasons=["source_exempt"])

    if metrics is None:
        try:
            metrics = get_venue_metrics(symbol, config_raw=config_raw, fetch_depth=True)
        except Exception as exc:
            log(f"liquidity guard fetch failed {symbol}: {exc}", "WARNING")
            missing = VenueMetrics(symbol=symbol, capture="missing", quote_volume_present=False)
            return VenueQualityResult(
                ok=False,
                reasons=["liq_guard_missing_input"],
                metrics=missing,
                code="liq_guard_missing_input",
            )
        if metrics.capture == "missing":
            err_pol = str(cfg.get("on_fetch_error") or "block_sensor")
            # #641: fail closed for buys. The old allow/fail_open policy
            # must not let a buy through when Gate data is missing.
            if err_pol in ("allow", "fail_open"):
                log(
                    f"liquidity guard ignoring fail-open fetch policy for {symbol}",
                    "WARNING",
                )
            prior = VenueQualityResult(
                ok=False,
                reasons=["venue_fetch_failed_block"],
                metrics=metrics,
                code="venue_liquidity_block",
            )
            return _apply_hard_liquidity(
                prior, metrics, planned_usdt=planned_usdt, config_raw=config_raw
            )

    try:
        prior = evaluate_venue_quality(metrics, cfg, planned_usdt=planned_usdt)
    except Exception as exc:
        log(f"liquidity guard evaluate failed {symbol}: {exc}", "WARNING")
        return VenueQualityResult(
            ok=False,
            reasons=["liq_guard_missing_input"],
            code="liq_guard_missing_input",
        )
    return _apply_hard_liquidity(
        prior, metrics, planned_usdt=planned_usdt, config_raw=config_raw
    )


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
