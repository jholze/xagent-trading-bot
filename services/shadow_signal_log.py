"""Shadow signal log for the 15m entry sensor (spec #687, v2.2).

Writes JSON rows only. Does not place orders or change slot, cooldown,
daily-buy, loss-brake, or rate-limiter state. Control symbols are never
fetched. Open signal symbols that left the watchlist may take one ticker
after the poll's active triggers, under the config cap and budget.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from logger import log

_LOCK = threading.Lock()
_OPEN: dict[str, dict] = {}
_CLOSED: set[str] = set()
_COUNTS: dict[str, int] = {}
_DAY: str | None = None
_COUNTS_LOADED = False

_REQUIRED_GATE_INPUTS = (
    "market_cap_usd",
    "quote_volume_24h_usdt",
    "daily_buys_count",
    "max_daily_buys",
    "loss_brake_halted",
    "open_slots",
    "max_open_positions",
    "cooldown_blocked",
    "rate_limited",
)

# Per-gate pass/fail. True means the gate would allow the buy. None means
# that gate's input is missing. Order is the first-failure order of the
# aggregate ``gates_passed`` flag.
_GATE_ORDER = (
    "daily_loss_brake",
    "trade_cooldown",
    "rate_limited",
    "max_daily_trades",
    "max_open_positions",
    "market_cap",
    "venue_liquidity",
)
_GATE_REJECT = {
    "venue_liquidity": "venue_liquidity_block",
}

# Same method the exit path uses. The row stores the fraction of that percent.
STOP_PCT_SOURCE = "strategies.dca.effective_stop_loss_thresholds"
_VENUE_NULL_REASONS = ("missing_quote_volume", "missing_floor")
# Live hard floor. No code default. A missing key is missing_floor.
LIQUIDITY_FLOOR_KEY = "min_quote_volume_24h_usdt"
MISSING_FLOOR_KEY = "risk.liquidity_guard.min_quote_volume_24h_usdt"
# Live multiple inside the merged venue config (evaluate_venue_quality).
VENUE_ORDER_MULTIPLE_KEY = "min_volume_to_order_multiple"
_FETCH_DISABLED_LOGGED = False

# Why a no_price exit had no price. Closed list. Controls are never fetched.
_NO_PRICE_REASONS = (
    "cap_reached",
    "fetch_timeout",
    "fetch_error",
    "fetch_disabled_config",
)
# Read from entry_sensor_15m. No numeric fallback: a missing or invalid key
# disables the single fetch.
_FETCH_LIMIT_KEYS = (
    "shadow_price_fetch_max_per_poll",
    "shadow_price_fetch_budget_s",
    "shadow_price_fetch_timeout_s",
)
_PRICE_FETCH_CURSOR = 0


class _CollectGates:
    """Default: build gate inputs inside the new-signal branch."""


_COLLECT_GATES = _CollectGates()

_EXIT_REASONS_EXCLUDED = frozenset({"no_price", "restart_unresolved"})


def _reason_count_key(reason: str, control: bool) -> str:
    side = "controls" if control else "signals"
    return f"no_price_{reason}_{side}"


def _fresh_counts() -> dict[str, int]:
    counts = {
        "controls_drawn": 0,
        "no_control": 0,
        "exit_signals": 0,
        "exit_controls": 0,
        "no_price_signals": 0,
        "no_price_controls": 0,
        "restart_signals": 0,
        "restart_controls": 0,
        "control_priced": 0,
        "signal_priced": 0,
    }
    for reason in _NO_PRICE_REASONS:
        counts[_reason_count_key(reason, False)] = 0
        counts[_reason_count_key(reason, True)] = 0
    return counts


def _ensure_counts() -> None:
    global _COUNTS
    if not _COUNTS:
        _COUNTS = _fresh_counts()


def reset_for_tests() -> None:
    """Drop in-memory shadow state and the journal file."""
    global _DAY, _COUNTS_LOADED, _PRICE_FETCH_CURSOR, _FETCH_DISABLED_LOGGED
    _COUNTS_LOADED = False
    _PRICE_FETCH_CURSOR = 0
    _FETCH_DISABLED_LOGGED = False
    with _LOCK:
        _OPEN.clear()
        _CLOSED.clear()
        _COUNTS.clear()
        _COUNTS.update(_fresh_counts())
        _DAY = None
        path = _state_path()
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _state_path() -> str:
    override = os.environ.get("SHADOW_SIGNAL_STATE_PATH")
    if override:
        return override
    if os.environ.get("PYTEST_RUNNING"):
        worker = os.environ.get("PYTEST_XDIST_WORKER") or "main"
        return os.path.join(tempfile.gettempdir(), f"shadow_signal_open_{worker}.json")
    return os.path.join("logs", "shadow_signal_open.json")


def _utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _ts(now: datetime) -> str:
    return _utc(now).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _day(now: datetime) -> str:
    return _utc(now).date().isoformat()


def _key(signal_id: str, control: bool) -> str:
    return f"{signal_id}|{'c' if control else 's'}"


def _emit(row: dict) -> None:
    log(json.dumps(row, separators=(",", ":"), default=str), "INFO")


def _null_gate(reason: str) -> tuple[None, str]:
    """Missing check. Never a guessed pass."""
    return None, reason


def config_version(config_raw: dict | None) -> tuple[str, str | None]:
    """#675 R3: ``<SHA>+<sha256>``. No git call. SHA from RAILWAY_GIT_COMMIT_SHA."""
    import hashlib

    payload = json.dumps(config_raw or {}, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    sha = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    if not sha:
        return f"unknown+{digest}", "sha_unavailable"
    return f"{sha}+{digest}", None


def fee_taker_conservative_pct(config_raw: dict | None) -> float | None:
    """Spot taker fee in percent points from config. Missing key → None."""
    try:
        spot = (config_raw or {})["costs"]["gate"]["spot"]
    except (KeyError, TypeError):
        return None
    if not isinstance(spot, dict) or "fee_taker_conservative_pct" not in spot:
        return None
    try:
        return float(spot["fee_taker_conservative_pct"])
    except (TypeError, ValueError):
        return None


def _stops_from_exit_path(
    config_raw: dict | None,
    symbol: str,
    timeframe: str,
    *,
    atr_pct: float | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
) -> dict:
    """Stops the live exit applies to the position this entry would open.

    The exit resolves an open lot with ``resolve_strategy_params``
    (``has_position=True``, frozen tier) and ``effective_stop_loss_thresholds``.
    Both receive the passed config. This function does not replace
    ``get_bot_config``. The tier is the one entry freezes from the ATR already
    in hand (``freeze_tier_on_entry``). Fractions so ``price * (1 - stop)`` is
    literal. ``stop_pct`` is the first threshold that path can fire.
    """
    from intelligence.volatility_classifier import volatility_tier
    from strategies.dca import effective_stop_loss_thresholds
    from strategies.registry import resolve_strategy_params

    cfg = _config_view(config_raw)
    coin = {"symbol": symbol, "timeframe": timeframe}
    try:
        va_cfg = cfg.volatile_altcoin_config or {}
    except Exception:
        va_cfg = {}
    frozen = None
    if atr_pct is not None and va_cfg.get("freeze_tier_on_entry", True):
        try:
            frozen = volatility_tier(
                coin,
                float(atr_pct),
                va_cfg,
                range_24h_pct=range_24h_pct,
                change_24h_pct=change_24h_pct,
            )
        except Exception:
            frozen = None
    resolve_args: dict = {
        "has_position": True,
        "frozen_tier": frozen,
        "cfg": cfg,
    }
    if atr_pct is not None:
        resolve_args["atr_pct"] = float(atr_pct)
    if range_24h_pct is not None:
        resolve_args["range_24h_pct"] = range_24h_pct
    if change_24h_pct is not None:
        resolve_args["change_24h_pct"] = change_24h_pct
    params = dict(resolve_strategy_params(coin, **resolve_args) or {})
    tier = params.get("volatility_tier") or frozen
    if not tier:
        try:
            from strategies.registry import _resolve_volatility_tier

            used_atr = atr_pct
            if used_atr is None:
                used_atr = resolve_strategy_params.__defaults__[1]
            tier = _resolve_volatility_tier(
                coin,
                used_atr,
                va_cfg,
                frozen_tier=frozen,
                range_24h_pct=range_24h_pct,
                change_24h_pct=change_24h_pct,
            )
        except Exception:
            tier = params.get("volatility_tier")
    raw = params.get("stop_loss_pct")
    if raw is None:
        try:
            raw = cfg.stop_loss_pct
        except Exception:
            raw = 0
    try:
        base = float(raw or 0)
    except (TypeError, ValueError):
        base = 0.0
    fresh = {
        "dca_rounds": 0,
        "dca_recovery_rounds": 0,
        "last_dca_at": None,
        "last_dca_recovery_at": None,
    }
    full, partial, in_grace = effective_stop_loss_thresholds(fresh, params, base, cfg=cfg)
    full_f = float(full)
    partial_f = None if partial is None else float(partial)
    if in_grace or partial_f is None:
        first = full_f
    else:
        first = min(full_f, partial_f)
    used_ratio = None
    if params.get("partial_stop_pct") is None and partial_f is not None and full_f:
        used_ratio = partial_f / full_f
    return {
        "stop_pct": first / 100.0,
        "stop_full_pct": full_f / 100.0,
        "stop_partial_pct": None if partial_f is None else partial_f / 100.0,
        "stop_pct_source": STOP_PCT_SOURCE,
        "partial_stop_ratio": used_ratio,
        "volatility_tier": params.get("volatility_tier") or tier,
    }


def effective_stop_fraction(config_raw: dict | None, symbol: str, timeframe: str) -> float:
    """Fraction used as ``price * (1 - stop_pct)``."""
    return float(_stops_from_exit_path(config_raw, symbol, timeframe)["stop_pct"])


def exit_hours_from(config_raw: dict | None, sensor_cfg: dict | None = None) -> float:
    sensor = sensor_cfg if isinstance(sensor_cfg, dict) else {}
    if "shadow_exit_hours" not in sensor:
        block = (config_raw or {}).get("entry_sensor_15m") or {}
        sensor = block if isinstance(block, dict) else {}
    if "shadow_exit_hours" not in sensor:
        raise KeyError("shadow_exit_hours")
    return float(sensor["shadow_exit_hours"])


def frozen_stop_pct(open_row: dict) -> float:
    """Stop frozen at signal time. Exit checks must not re-read config."""
    return float(open_row["stop_pct"])


def ret_net_pct(entry: float, exit_price: float, fee_pct: float | None) -> float | None:
    """Percent-point net return: gross minus the fee on each side."""
    if fee_pct is None:
        return None
    ent = float(entry)
    if ent <= 0:
        return None
    gross = (float(exit_price) / ent - 1.0) * 100.0
    return gross - (2.0 * float(fee_pct))


def _exit_ret(entry: float, exit_price: float, fee_pct: float | None, *, control: bool) -> float | None:
    return ret_net_pct(entry, exit_price, fee_pct)


def _publish_gates(gates: dict) -> dict:
    """Logged per-gate map. Identity, so a mutation can blank every gate."""
    return dict(gates)


def _gate_missing(cache: dict, key: str) -> bool:
    return key not in cache or cache[key] is None


def project_gates(cache: dict | None, config_raw: dict | None = None) -> dict:
    """One true/false/null per gate. Null only when that gate's input is missing.

    Does not mutate ``cache`` and does not call the active risk path.
    """
    gates = {name: None for name in _GATE_ORDER}
    if not isinstance(cache, dict):
        return _publish_gates(gates)

    if not _gate_missing(cache, "loss_brake_halted"):
        gates["daily_loss_brake"] = not bool(cache["loss_brake_halted"])
    if not _gate_missing(cache, "cooldown_blocked"):
        gates["trade_cooldown"] = not bool(cache["cooldown_blocked"])
    if not _gate_missing(cache, "rate_limited"):
        gates["rate_limited"] = not bool(cache["rate_limited"])
    if not _gate_missing(cache, "max_daily_buys") and not _gate_missing(cache, "daily_buys_count"):
        max_daily = float(cache["max_daily_buys"])
        blocked = max_daily > 0 and float(cache["daily_buys_count"]) >= max_daily
        gates["max_daily_trades"] = not blocked
    if not _gate_missing(cache, "max_open_positions") and not _gate_missing(cache, "open_slots"):
        max_slots = float(cache["max_open_positions"])
        blocked = max_slots > 0 and float(cache["open_slots"]) >= max_slots
        gates["max_open_positions"] = not blocked

    sensor = (config_raw or {}).get("entry_sensor_15m") or {}
    if not isinstance(sensor, dict):
        sensor = {}
    if not _gate_missing(cache, "market_cap_usd"):
        from data.cmc_market_cap import passes_market_cap_filter

        mcap_ok, _reason = passes_market_cap_filter(float(cache["market_cap_usd"]), sensor)
        gates["market_cap"] = bool(mcap_ok)
    if not _gate_missing(cache, "quote_volume_24h_usdt"):
        from services.venue_quality import liquidity_guard_config, venue_quality_config

        floor = liquidity_guard_config(config_raw).get(LIQUIDITY_FLOOR_KEY)
        if floor is None:
            gates["venue_liquidity"] = None
        elif _gate_missing(cache, "planned_order_usdt"):
            gates["venue_liquidity"] = None
        else:
            volume = float(cache["quote_volume_24h_usdt"])
            ok = volume >= float(floor)
            merged = venue_quality_config(config_raw)
            try:
                multiple = float(merged.get(VENUE_ORDER_MULTIPLE_KEY) or 0)
            except (TypeError, ValueError):
                multiple = 0.0
            planned = float(cache["planned_order_usdt"])
            if multiple > 0 and planned > 0 and volume < multiple * planned:
                ok = False
            gates["venue_liquidity"] = ok
    return _publish_gates(gates)


def venue_defaults_used(config_raw: dict | None) -> list[str]:
    """venue_quality keys filled from ``_DEFAULTS`` because raw omitted them.

    Informational. Never a null reason. ``_DEFAULTS`` itself is unchanged.
    """
    from services.venue_quality import _DEFAULTS

    risk = (config_raw or {}).get("risk") if isinstance(config_raw, dict) else None
    raw = risk.get("venue_quality") if isinstance(risk, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return sorted(key for key in _DEFAULTS if key not in raw)


def _planned_order_usdt(
    config_raw: dict | None,
    *,
    volatility_tier: str | None,
    sensor_cfg: dict | None,
) -> float | None:
    """Order size the sensor path would plan. Config is an argument.

    Uses ``resolve_sensor_usdt``. No budget reservation, no positions lock,
    no DCA or slot counters, no writes. A missing ``max_usdt_per_trade`` on
    the passed raw cannot be sized from cache and returns None.
    """
    raw = config_raw if isinstance(config_raw, dict) else {}
    if "max_usdt_per_trade" not in raw:
        return None
    try:
        base = float(raw["max_usdt_per_trade"])
    except (TypeError, ValueError):
        return None
    if isinstance(raw.get("max_usdt_per_trade"), bool) or base <= 0:
        return None
    from strategies.sensor_entry_policy import resolve_sensor_usdt

    mode = str(raw.get("trading_mode") or "")
    cfg = sensor_cfg if isinstance(sensor_cfg, dict) else {}
    if not cfg:
        block = raw.get("entry_sensor_15m")
        cfg = block if isinstance(block, dict) else {}
    return float(
        resolve_sensor_usdt(
            mode,
            volatility_tier=str(volatility_tier or ""),
            max_usdt_per_trade=base,
            cfg=cfg,
        )
    )


def venue_liquidity_null_reason(cache: dict | None, config_raw: dict | None) -> str | None:
    """Why the venue gate is null. Closed list, and only for that gate.

    Missing quote volume wins when both inputs are absent, because the floor
    is not consulted until a volume is present.
    """
    if not isinstance(cache, dict) or _gate_missing(cache, "quote_volume_24h_usdt"):
        return "missing_quote_volume"
    from services.venue_quality import liquidity_guard_config

    if liquidity_guard_config(config_raw).get(LIQUIDITY_FLOOR_KEY) is None:
        return "missing_floor"
    return None


def _venue_null_fields(cache: dict | None, config_raw: dict | None, gates: dict) -> dict:
    """One reason field, and only when the venue gate itself is null."""
    if not isinstance(gates, dict) or gates.get("venue_liquidity") is not None:
        return {}
    reason = venue_liquidity_null_reason(cache, config_raw)
    if reason not in _VENUE_NULL_REASONS:
        return {}
    fields = {"venue_liquidity_null_reason": reason}
    if reason == "missing_floor":
        fields["venue_liquidity_missing_key"] = MISSING_FLOOR_KEY
    return fields


def dry_check_gates(cache: dict | None, config_raw: dict | None = None) -> tuple[bool | None, str | None]:
    """Aggregate of :func:`project_gates`.

    Does not call the active risk path and does not mutate ``cache`` or any
    counter. ``cache is None`` means there is no side-effect-free entry point.
    A missing input nulls only that gate; the aggregate is null when any gate
    is null and none has failed.
    """
    if not isinstance(cache, dict):
        return _null_gate("gate_check_unavailable")
    gates = project_gates(cache, config_raw)
    for name in _GATE_ORDER:
        if gates[name] is False:
            return False, _GATE_REJECT.get(name, name)
    if any(gates[name] is None for name in _GATE_ORDER):
        return _null_gate("input_uncached")
    return True, None


def _config_view(config_raw: dict | None):
    from core.config import BotConfig

    return BotConfig(raw=config_raw or {})


def _parse_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _peek_max_daily_buys(config_raw: dict | None) -> int | None:
    try:
        from risk.risk_manager import RiskManager

        rm = RiskManager.__new__(RiskManager)
        rm.config = _config_view(config_raw)
        return int(RiskManager._effective_max_daily_buys(rm))
    except Exception:
        return None


def _peek_daily_buys() -> int | None:
    """Filled buys from the warm orders cache. A cold cache is missing.

    Does not call ``load_orders``. A miss stays null.
    """
    try:
        import time

        from risk.risk_manager import RiskManager
        from services.order_service import (
            _ORDERS_READ_CACHE,
            _ORDERS_READ_CACHE_TTL,
            OrderService,
        )

        svc = OrderService()
        cached = _ORDERS_READ_CACHE.get(svc._cache_key())
        if not cached or (time.time() - float(cached[0])) >= float(_ORDERS_READ_CACHE_TTL):
            return None
        doc = cached[1] if isinstance(cached[1], dict) else None
        if not isinstance(doc, dict):
            return None
        rm = RiskManager.__new__(RiskManager)
        return sum(
            1
            for _ in RiskManager._iter_daily_filled_orders(
                rm, side="buy", orders_doc=doc
            )
        )
    except Exception:
        return None


def _peek_loss_brake_halted() -> bool | None:
    """In-memory halt only. Does not read or reconcile trade history."""
    try:
        from risk.risk_manager import _daily_loss_halt_memory

        mem = _daily_loss_halt_memory()
        if mem is None:
            return False
        until = _parse_utc(mem)
        if until is None:
            return None
        return until > datetime.now(timezone.utc)
    except Exception:
        return None


def _peek_open_slots(config_raw: dict | None) -> int | None:
    """Open full slots for the current tenant. No lock, no new store.

    Reads a snapshot of the store that already exists for this tenant. The
    process-wide active store can belong to someone else, and creating an
    empty store takes the positions lock.
    """
    try:
        from core.tenant_context import DEFAULT_TENANT
        from strategies import positions as posmod
        from strategies.sell_rotation_policy import is_tail_position, rotation_config

        if posmod.is_positions_state_unknown():
            return None
        key = posmod._resolve_store_key()
        if key[0] == DEFAULT_TENANT:
            live = posmod.positions
        else:
            live = posmod._position_stores.get(key)
            if live is None:
                return None
        snapshot = dict(live)
        cfg = rotation_config(config_raw if isinstance(config_raw, dict) else {})
        return sum(
            1
            for pos in snapshot.values()
            if posmod.is_open_position(pos)
            and posmod.counts_toward_open_cap(pos)
            and not is_tail_position(pos, cfg)
        )
    except Exception:
        return None


def _peek_max_open_positions(config_raw: dict | None) -> int | None:
    try:
        return int(_config_view(config_raw).max_open_positions)
    except Exception:
        return None


def _peek_cooldown_blocked(symbol: str, timeframe: str, config_raw: dict | None) -> bool | None:
    """Buy cooldown from the position already in memory. Does not create a lot."""
    try:
        from core.tenant_context import DEFAULT_TENANT
        from strategies import positions as posmod

        if posmod.is_positions_state_unknown():
            return None
        key = posmod._resolve_store_key()
        if key[0] == DEFAULT_TENANT:
            store = posmod.positions
        else:
            store = posmod._position_stores.get(key)
            if store is None:
                return None
        pos = store.get(posmod.get_key(symbol, timeframe))
        if not isinstance(pos, dict):
            return False
        if str(pos.get("last_trade_type") or "").upper() != "BUY":
            return False
        last_ts = _parse_utc(pos.get("last_trade_at"))
        if last_ts is None:
            return False if not pos.get("last_trade_at") else None
        cfg = _config_view(config_raw)
        params = cfg.strategy_params(symbol, timeframe) or {}
        hours = params.get("min_hours_between_buys")
        if hours is None:
            hours = cfg.trade_cooldown_hours
        elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
        return elapsed < float(hours)
    except Exception:
        return None


def _peek_rate_limited(config_raw: dict | None) -> bool | None:
    """Eviction cap from RAM plus a still-fresh orders cache.

    Does not refresh the cache, does not prune ``_EVICT_TS``, and does not
    call the exchange. A cold cache leaves this input missing.
    """
    try:
        import time

        from risk.slot_eviction import slot_eviction_section
        from risk.slot_eviction_runtime import _EVICT_TS, _is_slot_evict_order
        from services.order_service import (
            _ORDERS_READ_CACHE,
            _ORDERS_READ_CACHE_TTL,
            OrderService,
            order_event_ts,
        )

        risk = (config_raw or {}).get("risk") if isinstance(config_raw, dict) else None
        cfg = slot_eviction_section(risk if isinstance(risk, dict) else None)
        if "max_evictions_per_hour" not in cfg or "max_evictions_per_day" not in cfg:
            return None
        max_h = int(cfg["max_evictions_per_hour"])
        max_d = int(cfg["max_evictions_per_day"])
        svc = OrderService()
        cached = _ORDERS_READ_CACHE.get(svc._cache_key())
        if not cached or (time.time() - float(cached[0])) >= float(_ORDERS_READ_CACHE_TTL):
            return None
        orders = (cached[1] or {}).get("orders") if isinstance(cached[1], dict) else None
        now = datetime.now()
        hour_n = 0
        day_n = 0
        for order in orders or []:
            if not _is_slot_evict_order(order):
                continue
            ts = order_event_ts(order)
            if ts is None:
                continue
            ts_naive = ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
            age_s = (now - ts_naive).total_seconds()
            if age_s <= 86400:
                day_n += 1
            if age_s <= 3600:
                hour_n += 1
        now_mono = time.time()
        ram = list(_EVICT_TS)
        n_hour = max(hour_n, sum(1 for t in ram if now_mono - t < 3600))
        n_day = max(day_n, sum(1 for t in ram if now_mono - t < 86400))
        return (max_h > 0 and n_hour >= max_h) or (max_d > 0 and n_day >= max_d)
    except Exception:
        return None


def snapshot_gate_inputs(
    *,
    symbol: str,
    timeframe: str,
    market_cap_usd: float | None,
    quote_volume_24h_usdt: float | None,
    config_raw: dict | None,
) -> dict:
    """Gate inputs the process already holds. No exchange call, no risk evaluate."""
    cache: dict = {}
    if market_cap_usd is not None:
        cache["market_cap_usd"] = market_cap_usd
    if quote_volume_24h_usdt is not None:
        cache["quote_volume_24h_usdt"] = quote_volume_24h_usdt

    def _store(key: str, value) -> None:
        if value is not None:
            cache[key] = value

    _store("max_daily_buys", _peek_max_daily_buys(config_raw))
    _store("daily_buys_count", _peek_daily_buys())
    _store("loss_brake_halted", _peek_loss_brake_halted())
    _store("open_slots", _peek_open_slots(config_raw))
    _store("max_open_positions", _peek_max_open_positions(config_raw))
    _store("cooldown_blocked", _peek_cooldown_blocked(symbol, timeframe, config_raw))
    _store("rate_limited", _peek_rate_limited(config_raw))
    return cache


def control_seed_from_signal_id(signal_id: str) -> int:
    return int.from_bytes(uuid.UUID(str(signal_id)).bytes[:8], "big")


def choose_control_symbol(universe: list[str], seed: int) -> str | None:
    """Seeded draw. Empty universe draws nothing."""
    if not universe:
        return None
    ordered = list(universe)
    return random.Random(int(seed)).choice(ordered)


def _blocked_symbols() -> set[str]:
    blocked: set[str] = set()
    for row in _OPEN.values():
        sym = row.get("symbol")
        if sym:
            blocked.add(str(sym))
    return blocked


def _cached_entry_filters_pass(
    info: dict | None,
    sensor_cfg: dict,
    config_raw: dict | None,
) -> bool:
    """Liquidity and market cap on cached fields only. Missing → not eligible."""
    if not isinstance(info, dict):
        return False
    if "market_cap_usd" not in info or info.get("market_cap_usd") is None:
        return False
    if "quote_volume_24h_usdt" not in info or info.get("quote_volume_24h_usdt") is None:
        return False
    from data.cmc_market_cap import passes_market_cap_filter

    ok, _reason = passes_market_cap_filter(float(info["market_cap_usd"]), sensor_cfg or {})
    if not ok:
        return False
    from services.venue_quality import liquidity_guard_config

    floor = liquidity_guard_config(config_raw).get("min_quote_volume_24h_usdt")
    if floor is None:
        return False
    try:
        return float(info["quote_volume_24h_usdt"]) >= float(floor)
    except (TypeError, ValueError):
        return False


def control_universe(
    signal_symbol: str,
    price_batch: dict | None,
    candidate_cache: dict | None,
    sensor_cfg: dict | None,
    config_raw: dict | None,
) -> list[str]:
    """Symbols in the price batch that pass cached entry filters, sorted."""
    batch = price_batch or {}
    cache = candidate_cache or {}
    sensor = sensor_cfg or {}
    blocked = _blocked_symbols()
    out: list[str] = []
    for sym in sorted(batch):
        if sym == signal_symbol or sym in blocked:
            continue
        try:
            px = float(batch.get(sym) or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if not _cached_entry_filters_pass(cache.get(sym), sensor, config_raw):
            continue
        out.append(sym)
    return out


def _positive_price(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def _find_open_signal(symbol: str) -> dict | None:
    for row in _OPEN.values():
        if row.get("control"):
            continue
        if row.get("symbol") == symbol:
            return row
    return None


def lookup_open_signal_id(symbol: str, now: datetime | None = None) -> str | None:
    """Open shadow id inside the exit window, else None. Information only."""
    current = _utc(now)
    with _LOCK:
        row = _find_open_signal(symbol)
        if not row:
            return None
        deadline = row["ts"] + timedelta(hours=float(row["exit_hours"]))
        if current > deadline:
            return None
        return str(row["signal_id"])


def _persist_shadow_link(order: Any, result: Any, sid: str | None, now: datetime | None) -> None:
    """Write the buy link on the decision log. Not a risk input."""
    symbol = getattr(order, "symbol", None)
    order_id = None
    if result is not None:
        order_id = getattr(result, "order_id", None) or None
    if not order_id:
        order_id = getattr(order, "order_id", None) or None
    source = getattr(order, "source", None) or None
    record = {
        "type": "shadow_link",
        "signal_id": sid,
        "shadow_signal_id": sid,
        "symbol": symbol,
        "order_id": order_id,
        "ts_utc": _ts(_utc(now)),
        "source": source,
    }
    _emit(record)
    from logger import log_decision

    log_decision(
        {
            "type": "shadow_link",
            "shadow_signal_id": sid,
            "signal_id": sid,
            "symbol": symbol,
            "order_id": order_id,
            "source": source,
            "action": "BUY",
        }
    )


def attach_shadow_signal_id(order: Any, result: Any = None, now: datetime | None = None) -> str | None:
    """Stamp the open shadow id onto a buy. Does not read or write risk state."""
    try:
        if str(getattr(order, "type", "") or "").upper() != "BUY":
            return None
        sid = lookup_open_signal_id(getattr(order, "symbol", "") or "", now=now)
        try:
            object.__setattr__(order, "shadow_signal_id", sid)
        except Exception:
            pass
        if result is not None:
            try:
                object.__setattr__(result, "shadow_signal_id", sid)
            except Exception:
                pass
        _persist_shadow_link(order, result, sid, now)
        return sid
    except Exception as exc:
        log(f"shadow_log_error link: {exc}", "WARNING")
        return None


def _encode_open_rows() -> list[dict]:
    encoded = []
    for row in _OPEN.values():
        item = dict(row)
        ts = item.get("ts")
        if isinstance(ts, datetime):
            item["ts"] = _ts(ts)
        encoded.append(item)
    return encoded


def _save_journal() -> None:
    path = _state_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _ensure_counts()
    payload = {
        "open": _encode_open_rows(),
        "counts": {key: int(value) for key, value in _COUNTS.items()},
        "day": _DAY,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def _read_journal_payload() -> tuple[list, dict | None, str | None]:
    path = _state_path()
    if not path or not os.path.isfile(path):
        return [], None, None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return [], None, None
    if isinstance(data, list):
        return data, None, None
    if isinstance(data, dict):
        rows = data.get("open") if isinstance(data.get("open"), list) else []
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else None
        day = data.get("day")
        return rows, counts, str(day) if day else None
    return [], None, None


def _load_journal() -> list[dict]:
    raw_rows, _counts, _day = _read_journal_payload()
    rows = []
    for item in raw_rows:
        if not isinstance(item, dict) or not item.get("signal_id"):
            continue
        row = dict(item)
        ts = row.get("ts")
        if isinstance(ts, str):
            text = ts.replace("Z", "+00:00")
            row["ts"] = datetime.fromisoformat(text)
        rows.append(row)
    return rows


def _restore_persisted_counters() -> None:
    """Load daily counts once after a restart. Later in-process updates win."""
    global _DAY, _COUNTS, _COUNTS_LOADED
    if _COUNTS_LOADED:
        return
    _COUNTS_LOADED = True
    _rows, counts, day = _read_journal_payload()
    if isinstance(counts, dict):
        fresh = _fresh_counts()
        for key in fresh:
            if key in counts:
                try:
                    fresh[key] = int(counts[key])
                except (TypeError, ValueError):
                    pass
        _COUNTS = fresh
    if day and _DAY is None:
        _DAY = day


def drop_process_memory() -> None:
    """Drop RAM shadow state and keep the journal. Simulates a restart."""
    global _DAY, _COUNTS_LOADED
    _COUNTS_LOADED = False
    with _LOCK:
        _OPEN.clear()
        _CLOSED.clear()
        _COUNTS.clear()
        _DAY = None


def _account_control(row: dict) -> None:
    _ensure_counts()
    if row.get("control_symbol") is None or row.get("reason") == "no_control":
        _COUNTS["no_control"] += 1
    else:
        _COUNTS["controls_drawn"] += 1


def _account_exit(row: dict) -> None:
    _ensure_counts()
    control = bool(row.get("control"))
    reason = str(row.get("exit_reason") or "")
    if control:
        _COUNTS["exit_controls"] += 1
    else:
        _COUNTS["exit_signals"] += 1
    if reason == "no_price":
        _COUNTS["no_price_controls" if control else "no_price_signals"] += 1
        detail = row.get("no_price_reason")
        if detail in _NO_PRICE_REASONS:
            _COUNTS[_reason_count_key(str(detail), control)] += 1
    elif reason == "restart_unresolved":
        _COUNTS["restart_controls" if control else "restart_signals"] += 1
    elif reason in ("stop", "4h"):
        _COUNTS["control_priced" if control else "signal_priced"] += 1


def build_daily_summary(day: str) -> dict:
    _ensure_counts()
    signal_exits = int(_COUNTS["exit_signals"])
    control_exits = int(_COUNTS["exit_controls"])
    no_price_signals = int(_COUNTS["no_price_signals"])
    no_price_controls = int(_COUNTS["no_price_controls"])
    drawn_with_result = int(_COUNTS["control_priced"])

    def _rate(num: int, den: int) -> float | None:
        if den <= 0:
            return None
        return num / den

    return {
        "type": "shadow_control_daily",
        "day": day,
        "controls_drawn": int(_COUNTS["controls_drawn"]),
        "no_control": int(_COUNTS["no_control"]),
        "no_price_signals": no_price_signals,
        "no_price_controls": no_price_controls,
        "restart_unresolved_signals": int(_COUNTS["restart_signals"]),
        "restart_unresolved_controls": int(_COUNTS["restart_controls"]),
        "no_price_rate_signals": _rate(no_price_signals, signal_exits),
        "no_price_rate_controls": _rate(no_price_controls, control_exits),
        "drawn_with_result": drawn_with_result,
        "no_price_by_reason": {
            "signals": {
                reason: int(_COUNTS[_reason_count_key(reason, False)])
                for reason in _NO_PRICE_REASONS
            },
            "controls": {
                reason: int(_COUNTS[_reason_count_key(reason, True)])
                for reason in _NO_PRICE_REASONS
            },
        },
    }


def note_poll_clock(now: datetime | None = None) -> dict | None:
    """Emit at most one ``shadow_control_daily`` line when the UTC day rolls."""
    global _DAY
    current = _utc(now)
    day = _day(current)
    with _LOCK:
        if _DAY is None:
            _DAY = day
            return None
        if day == _DAY:
            return None
        summary = build_daily_summary(_DAY)
        _emit(summary)
        _COUNTS.clear()
        _COUNTS.update(_fresh_counts())
        _DAY = day
        return summary


def _touch_day(now: datetime) -> None:
    global _DAY
    if _DAY is None:
        _DAY = _day(now)


def _parse_fetch_limits(sensor_cfg: dict | None) -> dict | None:
    """Config values for the off-watchlist ticker. Missing or invalid → None."""
    cfg = sensor_cfg if isinstance(sensor_cfg, dict) else {}
    parsed: dict[str, float] = {}
    for key in _FETCH_LIMIT_KEYS:
        if key not in cfg:
            return None
        raw = cfg[key]
        if isinstance(raw, bool) or raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0 or value != value or value == float("inf"):
            return None
        parsed[key] = value
    max_n = parsed["shadow_price_fetch_max_per_poll"]
    if abs(max_n - round(max_n)) > 1e-9:
        return None
    parsed["shadow_price_fetch_max_per_poll"] = int(round(max_n))
    return parsed


def _fetch_disabled_message() -> str:
    keys = ", ".join(f"entry_sensor_15m.{key}" for key in _FETCH_LIMIT_KEYS)
    return f"shadow price fetch disabled: {keys} missing or invalid"


def _log_fetch_disabled_once() -> None:
    """One ERROR for a disabled fetch. Later polls in this process stay quiet."""
    global _FETCH_DISABLED_LOGGED
    if _FETCH_DISABLED_LOGGED:
        return
    _FETCH_DISABLED_LOGGED = True
    log(_fetch_disabled_message(), "ERROR")


def _is_fetch_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return any(
        cls.__name__ in {"Timeout", "ConnectTimeout", "ReadTimeout"}
        for cls in type(exc).__mro__
    )


def _stamp_open_reason(signal_id: str, control: bool, reason: str | None) -> None:
    row = _OPEN.get(_key(signal_id, control))
    if row is None:
        return
    if reason is None:
        row.pop("no_price_reason", None)
    else:
        row["no_price_reason"] = reason


def _select_shadow_fetches(
    ordered: list[dict], cap: int, cursor: int
) -> tuple[list[dict], int]:
    """Nearest exit first. Remaining slots rotate across polls."""
    if cap <= 0 or not ordered:
        return [], cursor
    chosen = [ordered[0]]
    rest = ordered[1:]
    if cap == 1 or not rest:
        return chosen, cursor
    start = cursor % len(rest)
    rotated = rest[start:] + rest[:start]
    take = rotated[: cap - 1]
    chosen.extend(take)
    return chosen, (start + len(take)) % len(rest)


def fill_offwatch_signal_prices(
    prices: dict | None,
    *,
    watchlist: set[str] | None,
    sensor_cfg: dict | None,
) -> dict:
    """Price open signals that left the watchlist. Controls are never fetched.

    Count, budget, and per-call timeout come only from ``sensor_cfg``. If any
    of those keys is missing or invalid, nothing is fetched, missing rows are
    marked ``fetch_disabled_config``, and an ERROR is logged.
    """
    global _PRICE_FETCH_CURSOR
    batch = dict(prices or {})
    watched = {str(sym) for sym in (watchlist or set())}
    limits = _parse_fetch_limits(sensor_cfg)
    with _LOCK:
        signals: list[dict] = []
        controls: list[dict] = []
        for row in _OPEN.values():
            sym = str(row.get("symbol") or "")
            if not sym or sym in watched or _positive_price(batch.get(sym)) is not None:
                if sym and _positive_price(batch.get(sym)) is not None:
                    _stamp_open_reason(str(row.get("signal_id")), bool(row.get("control")), None)
                continue
            item = {
                "signal_id": str(row.get("signal_id")),
                "symbol": sym,
                "control": bool(row.get("control")),
                "exit_at": row["ts"] + timedelta(hours=float(row["exit_hours"])),
            }
            if item["control"]:
                controls.append(item)
            else:
                signals.append(item)
        signals.sort(key=lambda item: (item["exit_at"], item["symbol"], item["signal_id"]))
        if limits is None:
            if signals or controls:
                _log_fetch_disabled_once()
            for item in signals + controls:
                _stamp_open_reason(item["signal_id"], item["control"], "fetch_disabled_config")
            return batch
        cap = int(limits["shadow_price_fetch_max_per_poll"])
        chosen, _PRICE_FETCH_CURSOR = _select_shadow_fetches(
            signals, cap, _PRICE_FETCH_CURSOR
        )
        chosen_ids = {item["signal_id"] for item in chosen}
        for item in signals:
            if item["signal_id"] not in chosen_ids:
                _stamp_open_reason(item["signal_id"], False, "cap_reached")
        # Controls never take a fetch slot, so a missing batch price is the cap.
        for item in controls:
            _stamp_open_reason(item["signal_id"], True, "cap_reached")
        budget = float(limits["shadow_price_fetch_budget_s"])
        timeout_s = float(limits["shadow_price_fetch_timeout_s"])
    deadline = time.monotonic() + budget
    updates: dict[str, float] = {}
    outcomes: dict[str, str | None] = {}
    for item in chosen:
        sym = item["symbol"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outcomes[item["signal_id"]] = "cap_reached"
            continue
        call_timeout = min(timeout_s, remaining)
        try:
            from price_fetcher import _fetch_gate_single

            raw_px = _fetch_gate_single(sym, timeout=call_timeout)
        except Exception as exc:
            outcomes[item["signal_id"]] = (
                "fetch_timeout" if _is_fetch_timeout(exc) else "fetch_error"
            )
            continue
        px = _positive_price(raw_px)
        if px is None:
            outcomes[item["signal_id"]] = "fetch_error"
            continue
        updates[sym] = px
        outcomes[item["signal_id"]] = None
    with _LOCK:
        for item in chosen:
            reason = outcomes.get(item["signal_id"])
            _stamp_open_reason(item["signal_id"], False, reason)
        for sym, px in updates.items():
            batch[sym] = px
    return batch


def open_signal_symbols() -> list[str]:
    """Symbols of open shadow signals. Control symbols are not included."""
    with _LOCK:
        out: list[str] = []
        for row in _OPEN.values():
            if row.get("control"):
                continue
            sym = row.get("symbol")
            if sym and sym not in out:
                out.append(str(sym))
        return out


def _still_track(symbol: str, watchlist: set[str] | None) -> bool:
    """Open signals stay tracked after they leave the watchlist."""
    return True


def _control_price(symbol: str, prices: dict) -> float | None:
    """Control prices come only from the batch already fetched."""
    return _positive_price((prices or {}).get(symbol))


def _signal_row_price(symbol: str, prices: dict) -> float | None:
    return _positive_price((prices or {}).get(symbol))


def log_shadow_trigger(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    price_source: str,
    rationale: str,
    action: str,
    sensor_policy_source: str,
    config_raw: dict,
    price_batch: dict | None = None,
    candidate_cache: dict | None = None,
    gate_cache: dict | None | _CollectGates = _COLLECT_GATES,
    market_cap_usd: float | None = None,
    quote_volume_24h_usdt: float | None = None,
    sensor_cfg: dict | None = None,
    atr_pct: float | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Write one ``shadow_signal`` row, unless this symbol already has one open."""
    current = _utc(now)
    with _LOCK:
        _touch_day(current)
        existing = _find_open_signal(symbol)
        if existing:
            existing["repeat_count"] = int(existing.get("repeat_count") or 0) + 1
            _save_journal()
            return None

        # Gate inputs only for a new id. Repeats must not touch the ledger.
        # An explicit None stays unavailable (gate_check_unavailable).
        if gate_cache is _COLLECT_GATES:
            gate_cache = snapshot_gate_inputs(
                symbol=symbol,
                timeframe=timeframe,
                market_cap_usd=market_cap_usd,
                quote_volume_24h_usdt=quote_volume_24h_usdt,
                config_raw=config_raw,
            )

        signal_id = str(uuid.uuid4())
        stops = _stops_from_exit_path(
            config_raw,
            symbol,
            timeframe,
            atr_pct=atr_pct,
            range_24h_pct=range_24h_pct,
            change_24h_pct=change_24h_pct,
        )
        stop_pct = stops["stop_pct"]
        try:
            hours = exit_hours_from(config_raw, sensor_cfg)
        except KeyError:
            log("shadow_exit_hours missing: shadow signal not logged", "ERROR")
            return None
        fee = fee_taker_conservative_pct(config_raw)
        version, version_reason = config_version(config_raw)
        planned = None
        gate_for_check = gate_cache
        if isinstance(gate_cache, dict):
            planned = _planned_order_usdt(
                config_raw,
                volatility_tier=stops.get("volatility_tier"),
                sensor_cfg=sensor_cfg,
            )
            gate_for_check = dict(gate_cache)
            gate_for_check["planned_order_usdt"] = planned
        gates_passed, gate_reject_reason = dry_check_gates(gate_for_check, config_raw)
        gates = project_gates(gate_for_check, config_raw)
        px = float(price)
        cached_mcap = None
        cached_vol = None
        if isinstance(gate_cache, dict):
            cached_mcap = gate_cache.get("market_cap_usd")
            cached_vol = gate_cache.get("quote_volume_24h_usdt")
        row = {
            "type": "shadow_signal",
            "signal_id": signal_id,
            "ts_utc": _ts(current),
            "symbol": symbol,
            "timeframe": timeframe,
            "price_at_signal": px,
            "price_source": price_source,
            "stop_pct": stop_pct,
            "stop_full_pct": stops["stop_full_pct"],
            "stop_partial_pct": stops["stop_partial_pct"],
            "stop_pct_source": stops["stop_pct_source"],
            "partial_stop_ratio": stops.get("partial_stop_ratio"),
            "volatility_tier": stops.get("volatility_tier"),
            "exit_hours": hours,
            "rationale": rationale,
            "action": action,
            "sensor_policy_source": sensor_policy_source,
            "config_version": version,
            "gates": gates,
            "gates_passed": gates_passed,
            "gate_reject_reason": gate_reject_reason,
            "market_cap_usd": cached_mcap,
            "quote_volume_24h_usdt": cached_vol,
            "planned_order_usdt": planned,
            "venue_defaults_used": venue_defaults_used(config_raw),
        }
        row.update(_venue_null_fields(gate_for_check, config_raw, gates))
        if version_reason:
            row["config_version_reason"] = version_reason
        open_row = {
            "signal_id": signal_id,
            "control": False,
            "symbol": symbol,
            "timeframe": timeframe,
            "ts": current,
            "price_at_signal": px,
            "price_source": price_source,
            "stop_pct": stop_pct,
            "stop_full_pct": stops["stop_full_pct"],
            "stop_partial_pct": stops["stop_partial_pct"],
            "stop_pct_source": stops["stop_pct_source"],
            "exit_hours": hours,
            "fee_pct": fee,
            "repeat_count": 0,
        }
        _OPEN[_key(signal_id, False)] = open_row
        _emit(row)
        _write_control_locked(
            open_row,
            config_raw=config_raw,
            price_batch=price_batch or {},
            candidate_cache=candidate_cache or {},
            sensor_cfg=sensor_cfg or (config_raw or {}).get("entry_sensor_15m") or {},
            now=current,
        )
        _save_journal()
        return row


def _write_control_locked(
    signal_row: dict,
    *,
    config_raw: dict,
    price_batch: dict,
    candidate_cache: dict,
    sensor_cfg: dict,
    now: datetime,
) -> dict:
    signal_id = signal_row["signal_id"]
    seed = control_seed_from_signal_id(signal_id)
    universe = control_universe(
        signal_row["symbol"],
        price_batch,
        candidate_cache,
        sensor_cfg,
        config_raw,
    )
    drawn = choose_control_symbol(universe, seed)
    if not universe or drawn is None:
        row = {
            "type": "shadow_control",
            "signal_id": signal_id,
            "control": True,
            "control_symbol": None,
            "control_seed": seed,
            "universe_size": len(universe),
            "price_at_signal": None,
            "price_source": signal_row.get("price_source"),
            "stop_pct": signal_row["stop_pct"],
            "stop_full_pct": signal_row.get("stop_full_pct"),
            "stop_partial_pct": signal_row.get("stop_partial_pct"),
            "stop_pct_source": signal_row.get("stop_pct_source"),
            "exit_hours": signal_row["exit_hours"],
            "reason": "no_control",
        }
        _emit(row)
        _account_control(row)
        return row

    px = _positive_price(price_batch.get(drawn))
    row = {
        "type": "shadow_control",
        "signal_id": signal_id,
        "control": True,
        "control_symbol": drawn,
        "control_seed": seed,
        "universe_size": len(universe),
        "price_at_signal": px,
        "price_source": signal_row.get("price_source"),
        "stop_pct": signal_row["stop_pct"],
        "stop_full_pct": signal_row.get("stop_full_pct"),
        "stop_partial_pct": signal_row.get("stop_partial_pct"),
        "stop_pct_source": signal_row.get("stop_pct_source"),
        "exit_hours": signal_row["exit_hours"],
    }
    _emit(row)
    _account_control(row)
    _OPEN[_key(signal_id, True)] = {
        "signal_id": signal_id,
        "control": True,
        "symbol": drawn,
        "signal_symbol": signal_row["symbol"],
        "timeframe": signal_row.get("timeframe"),
        "ts": signal_row["ts"],
        "price_at_signal": px,
        "price_source": signal_row.get("price_source"),
        "stop_pct": signal_row["stop_pct"],
        "exit_hours": signal_row["exit_hours"],
        "fee_pct": signal_row.get("fee_pct"),
        "repeat_count": 0,
    }
    return row


def _write_exit(
    open_row: dict,
    *,
    reason: str,
    exit_price: float | None,
    price_source: str | None,
    now: datetime,
) -> dict | None:
    control = bool(open_row.get("control"))
    signal_id = str(open_row["signal_id"])
    key = _key(signal_id, control)
    if key in _CLOSED:
        return None
    _CLOSED.add(key)
    _OPEN.pop(key, None)
    excluded = reason in _EXIT_REASONS_EXCLUDED
    ret = None
    if exit_price is not None and not excluded:
        ret = _exit_ret(
            float(open_row["price_at_signal"]),
            float(exit_price),
            open_row.get("fee_pct"),
            control=control,
        )
    excluded_reason = None
    if ret is None and not excluded:
        excluded = True
        excluded_reason = "ret_net_unavailable"
    row = {
        "type": "shadow_exit",
        "signal_id": signal_id,
        "ts_utc": _ts(now),
        "exit_price": exit_price,
        "price_source": price_source,
        "exit_reason": reason,
        "ret_net_pct": ret,
        "control": control,
        "no_price": reason == "no_price",
        "excluded_from_evaluation": excluded,
    }
    if excluded_reason:
        row["excluded_reason"] = excluded_reason
    if reason == "no_price":
        stamped = open_row.get("no_price_reason")
        if stamped is None:
            # The caller passed a batch with no price and no fetch classification.
            stamped = "fetch_error"
        row["no_price_reason"] = stamped
    if not control:
        row["repeat_count"] = int(open_row.get("repeat_count") or 0)
    _emit(row)
    _account_exit(row)
    return row


def check_open_exits(
    prices: dict | None,
    *,
    price_source: str,
    watchlist: set[str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """One exit per open signal and per open control, from the poll batch."""
    current = _utc(now)
    batch = prices or {}
    written: list[dict] = []
    with _LOCK:
        _touch_day(current)
        rows = list(_OPEN.values())
        for open_row in rows:
            symbol = str(open_row.get("symbol") or "")
            if not _still_track(symbol, watchlist):
                continue
            if open_row.get("control"):
                px = _control_price(symbol, batch)
            else:
                px = _signal_row_price(symbol, batch)
            deadline = open_row["ts"] + timedelta(hours=float(open_row["exit_hours"]))
            past = current >= deadline
            reason = None
            exit_price = None
            entry_px = open_row.get("price_at_signal")
            if px is not None and entry_px is not None:
                stop_level = float(entry_px) * (1.0 - frozen_stop_pct(open_row))
                if px <= stop_level:
                    reason = "stop"
                    exit_price = px
                elif past:
                    reason = "4h"
                    exit_price = px
            elif past:
                reason = "no_price"
                exit_price = None
            if reason is None:
                continue
            exit_row = _write_exit(
                open_row,
                reason=reason,
                exit_price=exit_price,
                price_source=price_source if exit_price is not None else None,
                now=current,
            )
            if exit_row is not None:
                written.append(exit_row)
        if written:
            _save_journal()
    return written


def resolve_restart(now: datetime | None = None) -> list[dict]:
    """Startup: one ``restart_unresolved`` exit per open signal and control."""
    current = _utc(now)
    written: list[dict] = []
    with _LOCK:
        _restore_persisted_counters()
        _touch_day(current)
        seen: set[str] = set()
        loaded = _load_journal()
        combined = list(_OPEN.values()) + loaded
        for open_row in combined:
            if isinstance(open_row.get("ts"), str):
                text = str(open_row["ts"]).replace("Z", "+00:00")
                open_row = dict(open_row)
                open_row["ts"] = datetime.fromisoformat(text)
            control = bool(open_row.get("control"))
            key = _key(str(open_row.get("signal_id")), control)
            if key in seen or key in _CLOSED:
                continue
            seen.add(key)
            if key not in _OPEN:
                _OPEN[key] = open_row
            exit_row = _write_exit(
                _OPEN.get(key) or open_row,
                reason="restart_unresolved",
                exit_price=None,
                price_source=None,
                now=current,
            )
            if exit_row is not None:
                written.append(exit_row)
        _OPEN.clear()
        _save_journal()
    return written


def candidate_info_from_coin(symbol: str, coin: dict | None, config_raw: dict | None = None) -> dict:
    """Cached market cap and quote volume only. Never fetches."""
    info: dict[str, float] = {}
    mcap = None
    if isinstance(coin, dict):
        for key in ("market_cap_usd", "market_cap"):
            raw = coin.get(key)
            if raw not in (None, ""):
                try:
                    mcap = float(raw)
                except (TypeError, ValueError):
                    mcap = None
                break
    if mcap is None or mcap <= 0:
        from data.cmc_market_cap import peek_cached_market_cap_usd

        mcap = peek_cached_market_cap_usd(symbol)
    if mcap is not None and mcap > 0:
        info["market_cap_usd"] = float(mcap)

    vol = None
    if isinstance(coin, dict):
        from services.watchlist_quality.venue_batch import QUOTE_VOL_24H_KEY

        if coin.get(QUOTE_VOL_24H_KEY) not in (None, ""):
            try:
                vol = float(coin[QUOTE_VOL_24H_KEY])
            except (TypeError, ValueError):
                vol = None
    if vol is None or vol <= 0:
        from services.venue_quality import peek_cached_quote_volume

        vol = peek_cached_quote_volume(symbol, config_raw=config_raw)
    if vol is not None and vol > 0:
        info["quote_volume_24h_usdt"] = float(vol)
    return info
