"""Gate venue quality: 24h quote volume, spread, top-of-book (sensor-entry-guard).

Pure evaluate() is network-free for unit tests. Live fetch uses Gate bulk tickers
plus an opt-in public order-book depth call. HTTP lives in one function so #580
can wrap it. Sells must never be blocked by venue quality (callers only use this
on BUY paths).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from logger import log

GATE_PUBLIC_REST_BASE = "https://api.gateio.ws/api/v4"

_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, "VenueMetrics"]] = {}
_book_cache: dict[str, tuple[float, tuple]] = {}

# One log code for a buy whose size was cut to the liquidity room (#671).
LIQ_GUARD_SIZE_CAPPED = "liq_guard_size_capped"
# Config errors re-arm. Floor WARNINGs stay (tenant, key) until the test reset.
_CONFIG_WARNED: set[tuple] = set()

_VENUE_KEY = "risk.venue_quality"
_BOUNDS_KEY = "risk.venue_quality_bounds"
_VENUE_REQUIRED_KEYS = (
    "enabled",
    "exchange",
    "min_quote_volume_24h_usdt",
    "max_spread_pct",
    "min_top_book_usdt_per_side",
    "order_book_limit",
    "order_book_timeout_sec",
    "ticker_timeout_sec",
    "cache_ttl_sec",
    "book_unavailable_policy",
    "on_fetch_error",
    "min_volume_to_order_multiple",
)
_BOOK_POLICIES = frozenset({"volume_ok", "block"})
_FETCH_POLICIES = frozenset({"block_sensor", "allow", "fail_open"})
_BOUND_SPECS = (
    ("order_book_timeout_sec_max", "number"),
    ("ticker_timeout_sec_max", "number"),
    ("cache_ttl_sec_min", "number"),
    ("cache_ttl_sec_max", "number"),
    ("order_book_limit_max", "int"),
)
VENUE_BOUND_PATHS = tuple(f"{_BOUNDS_KEY}.{name}" for name, _kind in _BOUND_SPECS)


@dataclass(frozen=True)
class VenueConfigError:
    config_key: str
    config_problem: str


@dataclass(frozen=True)
class VenueBounds:
    order_book_timeout_sec_max: float
    ticker_timeout_sec_max: float
    cache_ttl_sec_min: float
    cache_ttl_sec_max: float
    order_book_limit_max: int


@dataclass(frozen=True)
class VenueQualityConfig:
    enabled: bool
    exchange: str
    min_quote_volume_24h_usdt: float
    max_spread_pct: float
    min_top_book_usdt_per_side: float
    order_book_limit: int
    order_book_timeout_sec: float
    ticker_timeout_sec: float
    cache_ttl_sec: float
    book_unavailable_policy: str
    on_fetch_error: str
    min_volume_to_order_multiple: float


@dataclass(frozen=True)
class FloorProblem:
    multiple_path: str
    floor_path: str
    kind: str
    multiple: float | None
    floor: float | None
    reason: str


def _venue_path(key: str) -> str:
    return f"{_VENUE_KEY}.{key}"


def _positive_problem(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "type"
    if not math.isfinite(float(value)) or float(value) <= 0:
        return "range"
    return None


def _bounded_positive(value: Any, *, hi: float, lo: float | None = None) -> str | None:
    problem = _positive_problem(value)
    if problem:
        return problem
    number = float(value)
    if lo is not None and number < lo:
        return "range"
    if number > hi:
        return "range"
    return None


def _limit_problem(value: Any, limit_max: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "type"
    if isinstance(value, float) and not float(value).is_integer():
        return "type"
    if not math.isfinite(float(value)):
        return "range"
    number = int(value)
    if number < 1 or number > int(limit_max):
        return "range"
    return None


def _key_problem(key: str, present: bool, value: Any, bounds: VenueBounds) -> str | None:
    if not present:
        return "missing"
    if value is None:
        return "null"
    if key == "enabled":
        return None if type(value) is bool else "type"
    if key == "exchange":
        if not isinstance(value, str):
            return "type"
        return None if value == "gate" else "value"
    if key == "book_unavailable_policy":
        if not isinstance(value, str):
            return "type"
        return None if value in _BOOK_POLICIES else "value"
    if key == "on_fetch_error":
        if not isinstance(value, str):
            return "type"
        return None if value in _FETCH_POLICIES else "value"
    if key == "order_book_limit":
        return _limit_problem(value, bounds.order_book_limit_max)
    if key == "order_book_timeout_sec":
        return _bounded_positive(value, hi=bounds.order_book_timeout_sec_max)
    if key == "ticker_timeout_sec":
        return _bounded_positive(value, hi=bounds.ticker_timeout_sec_max)
    if key == "cache_ttl_sec":
        return _bounded_positive(
            value, lo=bounds.cache_ttl_sec_min, hi=bounds.cache_ttl_sec_max
        )
    return _positive_problem(value)


def parse_venue_bounds(base: Any) -> VenueBounds | VenueConfigError:
    """Pure bounds parse. Disk and tenant merge stay with the caller."""
    if not isinstance(base, dict):
        return VenueConfigError(_BOUNDS_KEY, "config_unavailable")
    risk = base.get("risk")
    if not isinstance(risk, dict) or "venue_quality_bounds" not in risk:
        return VenueConfigError(VENUE_BOUND_PATHS[0], "missing")
    block = risk.get("venue_quality_bounds")
    if block is None:
        return VenueConfigError(_BOUNDS_KEY, "null")
    if not isinstance(block, dict):
        return VenueConfigError(_BOUNDS_KEY, "type")
    parsed: dict[str, float | int] = {}
    for name, kind in _BOUND_SPECS:
        path = f"{_BOUNDS_KEY}.{name}"
        if name not in block:
            return VenueConfigError(path, "missing")
        value = block.get(name)
        if value is None:
            return VenueConfigError(path, "null")
        if isinstance(value, bool) or isinstance(value, str) or not isinstance(value, (int, float)):
            return VenueConfigError(path, "type")
        if kind == "int":
            if isinstance(value, float) and not float(value).is_integer():
                return VenueConfigError(path, "type")
            if not math.isfinite(float(value)) or float(value) <= 0:
                return VenueConfigError(path, "range")
            parsed[name] = int(value)
            continue
        number = parse_liquidity_floor(value)
        if number is None:
            return VenueConfigError(path, "range")
        parsed[name] = number
    if float(parsed["cache_ttl_sec_min"]) > float(parsed["cache_ttl_sec_max"]):
        return VenueConfigError(f"{_BOUNDS_KEY}.cache_ttl_sec_min", "range")
    return VenueBounds(
        order_book_timeout_sec_max=float(parsed["order_book_timeout_sec_max"]),
        ticker_timeout_sec_max=float(parsed["ticker_timeout_sec_max"]),
        cache_ttl_sec_min=float(parsed["cache_ttl_sec_min"]),
        cache_ttl_sec_max=float(parsed["cache_ttl_sec_max"]),
        order_book_limit_max=int(parsed["order_book_limit_max"]),
    )


def load_venue_bounds() -> VenueBounds | VenueConfigError:
    """Bounds from base config.json at call time. No cache, no tenant merge."""
    try:
        import data_manager

        loaded = data_manager._load_default_config_from_disk()
    except Exception:
        return VenueConfigError(_BOUNDS_KEY, "config_unavailable")
    if not isinstance(loaded, dict):
        return VenueConfigError(_BOUNDS_KEY, "config_unavailable")
    return parse_venue_bounds(loaded)


def validate_venue_quality(
    block: Any, bounds: VenueBounds
) -> VenueQualityConfig | VenueConfigError:
    """Pure. No I/O, no logging, no result cache."""
    if not isinstance(bounds, VenueBounds):
        return VenueConfigError(_BOUNDS_KEY, "config_unavailable")
    if block is None:
        return VenueConfigError(_VENUE_KEY, "null")
    if not isinstance(block, dict):
        return VenueConfigError(_VENUE_KEY, "type")
    values: dict[str, Any] = {}
    for key in _VENUE_REQUIRED_KEYS:
        present = key in block
        value = block.get(key) if present else None
        problem = _key_problem(key, present, value, bounds)
        if problem:
            return VenueConfigError(_venue_path(key), problem)
        values[key] = value
    return VenueQualityConfig(
        enabled=values["enabled"],
        exchange=str(values["exchange"]),
        min_quote_volume_24h_usdt=float(values["min_quote_volume_24h_usdt"]),
        max_spread_pct=float(values["max_spread_pct"]),
        min_top_book_usdt_per_side=float(values["min_top_book_usdt_per_side"]),
        order_book_limit=int(values["order_book_limit"]),
        order_book_timeout_sec=float(values["order_book_timeout_sec"]),
        ticker_timeout_sec=float(values["ticker_timeout_sec"]),
        cache_ttl_sec=float(values["cache_ttl_sec"]),
        book_unavailable_policy=str(values["book_unavailable_policy"]),
        on_fetch_error=str(values["on_fetch_error"]),
        min_volume_to_order_multiple=float(values["min_volume_to_order_multiple"]),
    )


def present_venue_quality_error(
    body: Any, bounds: VenueBounds
) -> VenueConfigError | None:
    """Stage 1: type/range/value for keys the body actually carries."""
    if not isinstance(body, dict) or not isinstance(bounds, VenueBounds):
        return None
    risk = body.get("risk")
    if not isinstance(risk, dict) or "venue_quality" not in risk:
        return None
    block = risk.get("venue_quality")
    if block is None:
        return VenueConfigError(_VENUE_KEY, "null")
    if not isinstance(block, dict):
        return VenueConfigError(_VENUE_KEY, "type")
    for key in _VENUE_REQUIRED_KEYS:
        if key not in block:
            continue
        problem = _key_problem(key, True, block.get(key), bounds)
        if problem:
            return VenueConfigError(_venue_path(key), problem)
    return None


def _tenant_for_log() -> str:
    try:
        from core.tenant_context import resolve_tenant_id

        return str(resolve_tenant_id())
    except Exception:
        return ""


def _emit_venue_config_error(err: VenueConfigError) -> None:
    tenant = _tenant_for_log()
    token = (tenant, err.config_key, err.config_problem)
    if token in _CONFIG_WARNED:
        return
    _CONFIG_WARNED.add(token)
    log(
        f"venue config tenant={tenant} config_key={err.config_key} "
        f"config_problem={err.config_problem}",
        "ERROR",
    )
    if err.config_problem != "config_unavailable":
        return
    entry = {
        "type": "venue_config_error",
        "tenant_id": tenant,
        "config_key": err.config_key,
        "config_problem": err.config_problem,
        "reason": "venue_config_missing",
    }
    try:
        from logger import log_decision
        from services.observability_store import persist_decision

        log_decision(entry)
        persist_decision(entry)
    except Exception as exc:
        log(
            f"venue config decision ledger failed tenant={tenant} "
            f"config_key={err.config_key} config_problem={err.config_problem} "
            f"error={exc}",
            "ERROR",
        )


def _rearm_venue_config() -> None:
    """Drop venue and bounds error tokens for this tenant.

    Floor WARNINGs are 2-tuples and stay until the test reset (M38).
    """
    tenant = _tenant_for_log()
    keys = {_VENUE_KEY, _BOUNDS_KEY, *(_venue_path(key) for key in _VENUE_REQUIRED_KEYS)}
    keys.update(VENUE_BOUND_PATHS)
    stale = {
        token
        for token in _CONFIG_WARNED
        if len(token) == 3 and token[0] == tenant and token[1] in keys
    }
    _CONFIG_WARNED.difference_update(stale)


# Multiples the hard lock reads from the passed config. Floors are not here:
# they are read from base config.json only (R2). No numeric default.
DEPTH_MULTIPLE_PATH = "risk.liquidity_guard.depth_to_order_multiple"
VOLUME_MULTIPLE_PATH = "risk.venue_quality.min_volume_to_order_multiple"
DEPTH_FLOOR_PATH = "risk.liquidity_guard.depth_to_order_multiple_min"
VOLUME_FLOOR_PATH = "risk.venue_quality.min_volume_to_order_multiple_min"
_FLOOR_PAIRS = (
    (DEPTH_MULTIPLE_PATH, DEPTH_FLOOR_PATH),
    (VOLUME_MULTIPLE_PATH, VOLUME_FLOOR_PATH),
)


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
    config_key: str = ""
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
    config_key: str = ""
    config_problem: str = ""
    detail_reason: str = ""
    cap_depth: float | None = None
    cap_volume: float | None = None
    liq_cap_usdt: float | None = None

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
    timeout: float,
) -> Any:
    """Single public Gate REST GET for this gate (no auth). Isolated for #580.

    ``timeout`` is required. A missing, non-finite, or non-positive value
    never reaches ``requests``.
    """
    import requests

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise ValueError("gate timeout is not a positive finite number")

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


def _venue_block(config_raw: dict) -> Any:
    risk = config_raw.get("risk")
    if not isinstance(risk, dict) or "venue_quality" not in risk:
        return {}
    return risk.get("venue_quality")


def venue_quality_config(
    config_raw: dict | None = None,
) -> VenueQualityConfig | VenueConfigError:
    """Validated venue block, or one error. Never raises.

    ``None`` resolves the current tenant config. A failed resolution is
    ``config_unavailable``, not a filled-in block.
    """
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            err = VenueConfigError(_VENUE_KEY, "config_unavailable")
            _emit_venue_config_error(err)
            return err
    if not isinstance(config_raw, dict):
        err = VenueConfigError(_VENUE_KEY, "config_unavailable")
        _emit_venue_config_error(err)
        return err
    bounds = load_venue_bounds()
    if isinstance(bounds, VenueConfigError):
        _emit_venue_config_error(bounds)
        return bounds
    result = validate_venue_quality(_venue_block(config_raw), bounds)
    if isinstance(result, VenueConfigError):
        _emit_venue_config_error(result)
        return result
    _rearm_venue_config()
    return result


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


# #641 hard liquidity lock. No off-switch and no numeric fallback.
# Floor, ±mid window and book-cache max age are read only from
# risk.liquidity_guard. A missing or invalid key blocks the buy.


def _as_finite(value: Any) -> float | None:
    """A real number, or None when the value is missing, NaN, or infinite.

    Callers fail closed. This does not substitute a default.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _positive_config_float(raw: dict, key: str) -> float | None:
    if not isinstance(raw, dict) or key not in raw:
        return None
    value = raw.get(key)
    if value is None or value == "":
        return None
    number = _as_finite(value)
    if number is None or number <= 0:
        return None
    return number


def parse_liquidity_floor(value: Any) -> float | None:
    """R3: a floor is valid only as a finite int or float > 0.

    Bool and str are not numbers here. Anything else is missing.
    There is no code-constant substitute.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _format_floor_number(number: float) -> str:
    if float(number).is_integer():
        return str(int(number))
    return format(float(number), "g")


def _lookup_path(raw: dict, path: str) -> Any:
    node: Any = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _read_base_config_for_floors() -> dict | None:
    """Base config.json only, via the disk loader, at call time (R2).

    A raise propagates. The caller blocks the buy. ``None`` means the
    loader returned no dict.
    """
    import data_manager

    loaded = data_manager._load_default_config_from_disk()
    if not isinstance(loaded, dict):
        return None
    return loaded


def _warn_liquidity_floor(tenant_id: str, key: str, value: str, floor: str) -> None:
    """Floor WARNINGs do not re-arm. Token is (tenant, key), not the config triple."""
    token = (tenant_id, key)
    if token in _CONFIG_WARNED:
        return
    _CONFIG_WARNED.add(token)
    log(
        f"liquidity guard floor tenant={tenant_id} key={key} value={value} floor={floor}",
        "WARNING",
    )


def liquidity_multiple_floor_problems(
    depth_mult: float, volume_mult: float, base_cfg: dict | None
) -> list[FloorProblem]:
    """Pure floor check. No I/O, no tenant lookup, no warn-set write.

    ``base_cfg is None`` is a missing floor for both pairs, same as a disk error.
    ``below_floor`` only when ``multiple < floor``. Equality is valid.
    """
    values = {
        DEPTH_MULTIPLE_PATH: depth_mult,
        VOLUME_MULTIPLE_PATH: volume_mult,
    }
    problems: list[FloorProblem] = []
    for multiple_path, floor_path in _FLOOR_PAIRS:
        multiple = values[multiple_path]
        raw = None if not isinstance(base_cfg, dict) else _lookup_path(base_cfg, floor_path)
        present = isinstance(base_cfg, dict) and _path_present(base_cfg, floor_path)
        parsed = parse_liquidity_floor(raw) if present else None
        if parsed is None:
            problems.append(
                FloorProblem(
                    multiple_path=multiple_path,
                    floor_path=floor_path,
                    kind="floor_missing",
                    multiple=None if multiple is None else float(multiple),
                    floor=None,
                    reason=f"liquidity guard floor missing: {floor_path}",
                )
            )
            continue
        if multiple is not None and float(multiple) < parsed:
            problems.append(
                FloorProblem(
                    multiple_path=multiple_path,
                    floor_path=floor_path,
                    kind="below_floor",
                    multiple=float(multiple),
                    floor=float(parsed),
                    reason=(
                        f"{multiple_path} {_format_floor_number(float(multiple))} "
                        f"below floor {_format_floor_number(parsed)}"
                    ),
                )
            )
    return problems


def liquidity_multiple_floor_reasons(depth_mult: float, volume_mult: float) -> list[str]:
    """Live wrapper: tenant, disk read, today's reason text, one WARNING per key."""
    from core.tenant_context import resolve_tenant_id

    tenant_id = resolve_tenant_id()
    try:
        base = _read_base_config_for_floors()
    except Exception:
        base = None
    problems = liquidity_multiple_floor_problems(depth_mult, volume_mult, base)
    reasons: list[str] = []
    for problem in problems:
        reasons.append(problem.reason)
        if problem.kind == "floor_missing":
            _warn_liquidity_floor(
                tenant_id,
                problem.floor_path,
                _format_floor_number(problem.multiple),
                "missing",
            )
            continue
        _warn_liquidity_floor(
            tenant_id,
            problem.multiple_path,
            _format_floor_number(problem.multiple or 0),
            _format_floor_number(problem.floor or 0),
        )
    return reasons


def _path_present(raw: dict, path: str) -> bool:
    node: Any = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def book_age_sec(symbol: str) -> float | None:
    """Seconds since the cached book used for this pair, if one is stored."""
    pair = _pair(symbol or "")
    with _cache_lock:
        hit = _book_cache.get(pair)
    if not hit:
        return None
    try:
        stamped = float(hit[0])
    except (TypeError, ValueError):
        return None
    return max(0.0, time.time() - stamped)


def liquidity_guard_config(config_raw: dict | None = None) -> dict[str, Any]:
    """Gate 24h quote-volume floor, ±% mid window, book-cache max age, and depth multiple.

    Volume is the pair's own Gate ticker ``quote_volume``, never CMC.
    ``depth_window_pct`` is a percent of mid, not a fraction and not a
    level count. ``depth_to_order_multiple`` has no code default.
    Each value is None when the key is missing or not a positive number.
    Callers then block the buy.
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
    floor = _positive_config_float(raw, "min_quote_volume_24h_usdt")
    window = _positive_config_float(raw, "depth_window_pct")
    max_age = _positive_config_float(raw, "order_book_cache_ttl_sec")
    depth_mult = _positive_config_float(raw, "depth_to_order_multiple")
    return {
        "min_quote_volume_24h_usdt": floor,
        "depth_window_pct": window,
        "order_book_cache_ttl_sec": max_age,
        "depth_to_order_multiple": depth_mult,
        "complete": (
            floor is not None
            and window is not None
            and max_age is not None
            and depth_mult is not None
        ),
    }


def _reject_code(reasons: list[str]) -> str:
    if reasons == ["book_unavailable"] or (
        reasons and all(r == "book_unavailable" for r in reasons)
    ):
        return "book_unavailable"
    return "venue_liquidity_block"


def evaluate_venue_quality(
    metrics: VenueMetrics | dict[str, Any] | None,
    cfg: VenueQualityConfig,
    *,
    planned_usdt: float = 0.0,
    skip_volume_multiple: bool = False,
) -> VenueQualityResult:
    """Pure venue gate. Thin → ok=False with reasons. Missing metrics → not ok."""
    if not isinstance(cfg, VenueQualityConfig):
        raise TypeError("evaluate_venue_quality requires VenueQualityConfig")
    if not cfg.enabled:
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
    min_qv = float(cfg.min_quote_volume_24h_usdt)
    if min_qv > 0 and volume_data and m.quote_volume_24h_usdt < min_qv:
        reasons.append(
            f"quote_vol_24h ${m.quote_volume_24h_usdt:.0f} < min ${min_qv:.0f}"
        )

    max_spread = float(cfg.max_spread_pct)
    spread = m.spread_pct if m.spread_pct > 0 else compute_spread_pct(m.bid, m.ask)
    if max_spread > 0 and spread > max_spread:
        reasons.append(f"spread {spread:.2f}% > max {max_spread:.2f}%")

    min_book = float(cfg.min_top_book_usdt_per_side)
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
    k = float(cfg.min_volume_to_order_multiple)
    # Buys skip this yes/no. ``cap_volume`` in the hard lock replaces it.
    # Stamp and thin-venue reads keep it, and never invent a multiple.
    if not skip_volume_multiple and k > 0 and planned > 0 and volume_data:
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
        policy = str(cfg.book_unavailable_policy)
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


def is_thin_venue_stamp(
    stamp: dict | None, cfg: VenueQualityConfig | VenueConfigError
) -> bool | None:
    """Whether a fill-time venue stamp counts as thin (for memory learning).

    Invalid config is unknown (None), never thin.
    """
    if isinstance(cfg, VenueConfigError) or not isinstance(cfg, VenueQualityConfig):
        return None
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


# Buy sources the proof test enumerates one by one. A new buy source
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


def _book_cache_max_age(config_raw: dict | None) -> float | None:
    """Seconds from config. None when the key is missing or invalid."""
    return liquidity_guard_config(config_raw).get("order_book_cache_ttl_sec")


def _band_from_levels(
    payload: Any, *, window_pct: float
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
        return 0.0, 0.0, False, ()
    if window <= 0:
        return 0.0, 0.0, False, ()
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
    payload: Any, *, window_pct: float
) -> tuple[float, float, bool]:
    """Sum notional on each side inside ±window_pct of mid.

    Mid is (best bid + best ask) / 2. Every readable level whose price sits
    in the band counts. There is no level-count cap. Empty or unreadable
    books are unparsed, not a measured $0.
    """
    bid_d, ask_d, parsed, _levels = _band_from_levels(payload, window_pct=window_pct)
    return bid_d, ask_d, parsed


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


def _attach_order_book_depth(
    m: VenueMetrics, cfg: VenueQualityConfig, *, config_raw: dict | None = None
) -> VenueMetrics:
    limit = int(cfg.order_book_limit)
    timeout = float(cfg.order_book_timeout_sec)
    lg = liquidity_guard_config(config_raw)
    ttl = lg.get("order_book_cache_ttl_sec")
    window = lg.get("depth_window_pct")
    pair = _pair(m.symbol)
    now = time.time()

    with _cache_lock:
        hit = _book_cache.get(pair)
        if (
            ttl is not None
            and window is not None
            and hit
            and now - hit[0] <= ttl
        ):
            bid_d, ask_d, parsed, levels = hit[1]
            return _with_depth(m, bid_d, ask_d, parsed, levels)

    if window is None:
        # No configured band. Do not invent one and do not keep a cached book.
        return m

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
    if isinstance(cfg, VenueConfigError):
        return {
            sym: VenueMetrics(symbol=sym, capture="missing", config_key=cfg.config_key)
            for sym in list(dict.fromkeys(symbols))
        }
    ttl = float(cfg.cache_ttl_sec)
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
                max_age = _book_cache_max_age(config_raw)
                book_fresh = bool(
                    max_age is not None and book and now - book[0] <= max_age
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
            data = gate_public_rest_get("/spot/tickers", timeout=cfg.ticker_timeout_sec)
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
            resolved = _attach_order_book_depth(m, cfg, config_raw=config_raw)
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


def _stamp_liquidity(
    out: VenueQualityResult,
    *,
    codes: list[str],
    planned: float,
    window: float | None,
    qv: float | None,
    bid_d: float | None,
    ask_d: float | None,
    cap_depth: float | None,
    cap_volume: float | None,
    liq_cap: float | None,
    book_age: float | None,
) -> VenueQualityResult:
    """Cap fields on the pass path and the reject path (#671 R1, R6)."""
    out.guard_codes = list(codes)  # type: ignore[attr-defined]
    out.planned_usdt = planned  # type: ignore[attr-defined]
    out.depth_window_pct = window  # type: ignore[attr-defined]
    out.quote_volume_24h_usdt = qv  # type: ignore[attr-defined]
    out.qv24_usdt = qv  # type: ignore[attr-defined]
    out.depth_bid_usdt = bid_d  # type: ignore[attr-defined]
    out.depth_ask_usdt = ask_d  # type: ignore[attr-defined]
    out.band_bid_usdt = bid_d  # type: ignore[attr-defined]
    out.band_ask_usdt = ask_d  # type: ignore[attr-defined]
    out.cap_depth = cap_depth  # type: ignore[attr-defined]
    out.cap_volume = cap_volume  # type: ignore[attr-defined]
    out.liq_cap_usdt = liq_cap  # type: ignore[attr-defined]
    out.book_age_sec = book_age  # type: ignore[attr-defined]
    return out


def _apply_hard_liquidity(
    prior: VenueQualityResult,
    metrics: VenueMetrics | dict | None,
    *,
    planned_usdt: float,
    config_raw: dict | None,
) -> VenueQualityResult:
    """Repair the #563 gate: Gate volume floor and the position liquidity cap.

    Depth and the 24h-volume multiple no longer reject the buy here. They
    set ``cap_depth`` and ``cap_volume`` on every result, including a pass.
    The caller cuts or rejects from ``liq_cap_usdt``. A missing book, a
    missing quote volume, or a missing positive config value is still
    ``liq_guard_missing_input``. The quote-volume floor is unchanged.
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
    floor = lg.get("min_quote_volume_24h_usdt")
    window = lg.get("depth_window_pct")
    max_age = lg.get("order_book_cache_ttl_sec")
    depth_mult = lg.get("depth_to_order_multiple")
    symbol = "" if m is None else str(getattr(m, "symbol", "") or "")
    vq = venue_quality_config(config_raw)
    planned = float(planned_usdt or 0)
    if isinstance(vq, VenueConfigError):
        codes = ["liq_guard_missing_input"]
        reasons = list(prior.reasons)
        reasons.append("venue_config_missing")
        out = VenueQualityResult(
            ok=False,
            reasons=reasons,
            metrics=m,
            code="liq_guard_missing_input",
            config_key=vq.config_key,
            config_problem=vq.config_problem,
            detail_reason="venue_config_missing",
        )
        return _stamp_liquidity(
            out,
            codes=codes,
            planned=planned,
            window=lg.get("depth_window_pct"),
            qv=None,
            bid_d=None,
            ask_d=None,
            cap_depth=None,
            cap_volume=None,
            liq_cap=None,
            book_age=book_age_sec(symbol) if symbol else None,
        )
    k = vq.min_volume_to_order_multiple
    qv = None
    ask_d = None
    bid_d = None
    if (
        floor is None
        or window is None
        or max_age is None
        or depth_mult is None
        or k is None
    ):
        codes.append("liq_guard_missing_input")
        reasons.append("liquidity guard config missing")
        out = VenueQualityResult(
            ok=False, reasons=reasons, metrics=m, code="liq_guard_missing_input"
        )
        return _stamp_liquidity(
            out,
            codes=codes,
            planned=planned,
            window=window,
            qv=None,
            bid_d=None,
            ask_d=None,
            cap_depth=None,
            cap_volume=None,
            liq_cap=None,
            book_age=book_age_sec(symbol) if symbol else None,
        )
    floor_reasons = liquidity_multiple_floor_reasons(float(depth_mult), float(k))
    if floor_reasons:
        codes.append("liq_guard_missing_input")
        reasons.extend(floor_reasons)
        out = VenueQualityResult(
            ok=False, reasons=reasons, metrics=m, code="liq_guard_missing_input"
        )
        return _stamp_liquidity(
            out,
            codes=codes,
            planned=planned,
            window=window,
            qv=None,
            bid_d=None,
            ask_d=None,
            cap_depth=None,
            cap_volume=None,
            liq_cap=None,
            book_age=book_age_sec(symbol) if symbol else None,
        )
    capture = "" if m is None else str(getattr(m, "capture", "") or "")
    if m is None or capture in ("missing", "stale"):
        codes.append("liq_guard_missing_input")
    else:
        volume_known = bool(getattr(m, "quote_volume_present", True))
        if not volume_known:
            codes.append("liq_guard_missing_input")
        else:
            qv = _as_finite(getattr(m, "quote_volume_24h_usdt", None))
            if qv is None:
                codes.append("liq_guard_missing_input")
            elif qv < float(floor):
                codes.append("liq_guard_volume_low")
                reasons.append(
                    f"quote_vol_24h ${qv:.0f} < liquidity floor ${floor:.0f}"
                )
        if not bool(getattr(m, "depth_parsed", False)):
            if "liq_guard_missing_input" not in codes:
                codes.append("liq_guard_missing_input")
                reasons.append("band depth unavailable")
        else:
            bid_d = _as_finite(getattr(m, "depth_bid_usdt", None))
            ask_d = _as_finite(getattr(m, "depth_ask_usdt", None))
            if bid_d is None or ask_d is None:
                if "liq_guard_missing_input" not in codes:
                    codes.append("liq_guard_missing_input")
            if planned <= 0 and "liq_guard_missing_input" not in codes:
                codes.append("liq_guard_missing_input")
                reasons.append("planned size unavailable")

    cap_depth = None
    cap_volume = None
    liq_cap = None
    if bid_d is not None and ask_d is not None and qv is not None and k:
        cap_depth = min(float(bid_d), float(ask_d)) / float(depth_mult)
        cap_volume = float(qv) / float(k)
        liq_cap = min(cap_depth, cap_volume)

    age = book_age_sec(symbol) if symbol else None
    if not codes:
        out = VenueQualityResult(
            ok=bool(prior.ok),
            reasons=list(prior.reasons),
            metrics=m if m is not None else prior.metrics,
            code="" if prior.ok else (prior.code or ""),
        )
        return _stamp_liquidity(
            out,
            codes=[],
            planned=planned,
            window=window,
            qv=qv,
            bid_d=bid_d,
            ask_d=ask_d,
            cap_depth=cap_depth,
            cap_volume=cap_volume,
            liq_cap=liq_cap,
            book_age=age,
        )

    # When the old gate already rejected, keep that code first so a thin
    # book still reports venue_liquidity_block. Hard codes stay on the
    # list. When the old gate would have allowed the buy, the hard code
    # is primary.
    all_codes = list(codes)
    if prior.code and not prior.ok and prior.code not in all_codes:
        all_codes = [prior.code] + all_codes
    primary = all_codes[0]
    out = VenueQualityResult(ok=False, reasons=reasons, metrics=m, code=primary)
    return _stamp_liquidity(
        out,
        codes=all_codes,
        planned=planned,
        window=window,
        qv=qv,
        bid_d=bid_d,
        ask_d=ask_d,
        cap_depth=cap_depth,
        cap_volume=cap_volume,
        liq_cap=liq_cap,
        book_age=age,
    )


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
    if isinstance(cfg, VenueConfigError):
        # Invalid config is gate-required. No HTTP.
        return VenueQualityResult(
            ok=False,
            reasons=["venue_config_missing"],
            metrics=VenueMetrics(
                symbol=symbol,
                capture="missing",
                quote_volume_present=False,
                config_key=cfg.config_key,
            ),
            code="liq_guard_missing_input",
            config_key=cfg.config_key,
            config_problem=cfg.config_problem,
            detail_reason="venue_config_missing",
        )
    # enabled=false switches off the prior soft gate only. The hard lock
    # still runs, so a missing book is fetched here the same as when enabled.
    # A human manual buy is logged by the caller, not skipped here.

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
            err_pol = str(cfg.on_fetch_error)
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
        prior = evaluate_venue_quality(
            metrics,
            cfg,
            planned_usdt=planned_usdt,
            skip_volume_multiple=True,
        )
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
    if isinstance(cfg, VenueConfigError):
        return {
            "capture": "missing",
            "symbol": symbol,
            "planned_usdt": float(planned_usdt or 0),
            "config_key": cfg.config_key,
            "config_problem": cfg.config_problem,
        }
    m = metrics or get_venue_metrics(symbol, config_raw=config_raw, fetch_depth=True)
    if m.capture == "missing":
        return {
            "capture": "missing",
            "exchange": cfg.exchange,
            "symbol": symbol,
            "planned_usdt": float(planned_usdt or 0),
        }
    result = evaluate_venue_quality(m, cfg, planned_usdt=planned_usdt)
    return m.to_stamp(
        planned_usdt=planned_usdt,
        venue_ok=result.ok,
        reasons=result.reasons,
    )


def peek_cached_quote_volume(symbol: str, *, config_raw: dict | None = None) -> float | None:
    """Return a still-fresh cached 24h quote volume. Does not fetch."""
    cfg = venue_quality_config(config_raw)
    if isinstance(cfg, VenueConfigError):
        return None
    ttl = float(cfg.cache_ttl_sec)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(symbol)
    if not hit or now - hit[0] > ttl:
        return None
    try:
        vol = float(hit[1].quote_volume_24h_usdt)
    except (TypeError, ValueError, AttributeError):
        return None
    return vol if vol > 0 else None


def reset_venue_cache_for_tests() -> None:
    _CONFIG_WARNED.clear()
    with _cache_lock:
        _cache.clear()
        _book_cache.clear()
