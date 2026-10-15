"""Shadow signal log for the 15m entry sensor (spec #687, v2.2).

Writes JSON rows only. Does not place orders or change slot, cooldown,
daily-buy, loss-brake, or rate-limiter state. Prices come from the poll
batch the loop already fetched. Control symbols are never fetched.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from logger import log

_LOCK = threading.Lock()
_OPEN: dict[str, dict] = {}
_CLOSED: set[str] = set()
_COUNTS: dict[str, int] = {}
_DAY: str | None = None

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

_EXIT_REASONS_EXCLUDED = frozenset({"no_price", "restart_unresolved"})


def _fresh_counts() -> dict[str, int]:
    return {
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


def _ensure_counts() -> None:
    global _COUNTS
    if not _COUNTS:
        _COUNTS = _fresh_counts()


def reset_for_tests() -> None:
    """Drop in-memory shadow state and the journal file."""
    global _DAY
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


def effective_stop_fraction(config_raw: dict | None, symbol: str, timeframe: str) -> float:
    """Fraction used as ``price * (1 - stop_pct)``.

    ``stop_loss_pct`` in this bot is percent (50 means 50%). The shadow row
    stores the fraction so the spec formula applies directly.
    """
    raw = config_raw or {}
    chosen = None
    for entry in raw.get("strategies") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("symbol") == symbol and str(entry.get("timeframe") or "") == str(timeframe):
            if entry.get("stop_loss_pct") is not None:
                chosen = entry.get("stop_loss_pct")
                break
    if chosen is None:
        chosen = raw.get("stop_loss_pct")
    if chosen is None:
        sensor = raw.get("entry_sensor_15m") or {}
        if isinstance(sensor, dict):
            chosen = sensor.get("stop_pct", sensor.get("stop_loss_pct"))
    if chosen is None:
        raise KeyError("stop_loss_pct")
    return float(chosen) / 100.0


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


def dry_check_gates(cache: dict | None, config_raw: dict | None = None) -> tuple[bool | None, str | None]:
    """Read-only gate projection from cached inputs.

    Does not call the active risk path and does not mutate ``cache`` or any
    counter. ``cache is None`` means there is no side-effect-free entry point.
    A missing key means that input was not cached.
    """
    if not isinstance(cache, dict):
        return _null_gate("gate_check_unavailable")
    for key in _REQUIRED_GATE_INPUTS:
        if key not in cache or cache[key] is None:
            return _null_gate("input_uncached")

    if bool(cache["loss_brake_halted"]):
        return False, "daily_loss_brake"
    if bool(cache["cooldown_blocked"]):
        return False, "trade_cooldown"
    if bool(cache["rate_limited"]):
        return False, "rate_limited"

    max_daily = float(cache["max_daily_buys"])
    if max_daily > 0 and float(cache["daily_buys_count"]) >= max_daily:
        return False, "max_daily_trades"

    max_slots = float(cache["max_open_positions"])
    if max_slots > 0 and float(cache["open_slots"]) >= max_slots:
        return False, "max_open_positions"

    sensor = (config_raw or {}).get("entry_sensor_15m") or {}
    if not isinstance(sensor, dict):
        sensor = {}
    from data.cmc_market_cap import passes_market_cap_filter

    mcap_ok, _reason = passes_market_cap_filter(float(cache["market_cap_usd"]), sensor)
    if not mcap_ok:
        return False, "market_cap"

    from services.venue_quality import liquidity_guard_config

    floor = liquidity_guard_config(config_raw).get("min_quote_volume_24h_usdt")
    if floor is None:
        return _null_gate("input_uncached")
    if float(cache["quote_volume_24h_usdt"]) < float(floor):
        return False, "venue_liquidity_block"
    return True, None


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
        return sid
    except Exception as exc:
        log(f"shadow_log_error link: {exc}", "WARNING")
        return None


def _save_journal() -> None:
    path = _state_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = list(_OPEN.values())
    # Datetimes are not JSON native.
    encoded = []
    for row in payload:
        item = dict(row)
        ts = item.get("ts")
        if isinstance(ts, datetime):
            item["ts"] = _ts(ts)
        encoded.append(item)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(encoded, handle)
    os.replace(tmp, path)


def _load_journal() -> list[dict]:
    path = _state_path()
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict) or not item.get("signal_id"):
            continue
        row = dict(item)
        ts = row.get("ts")
        if isinstance(ts, str):
            text = ts.replace("Z", "+00:00")
            row["ts"] = datetime.fromisoformat(text)
        rows.append(row)
    return rows


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
    gate_cache: dict | None = None,
    sensor_cfg: dict | None = None,
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

        signal_id = str(uuid.uuid4())
        stop_pct = effective_stop_fraction(config_raw, symbol, timeframe)
        hours = exit_hours_from(config_raw, sensor_cfg)
        fee = fee_taker_conservative_pct(config_raw)
        version, version_reason = config_version(config_raw)
        gates_passed, gate_reject_reason = dry_check_gates(gate_cache, config_raw)
        px = float(price)
        row = {
            "type": "shadow_signal",
            "signal_id": signal_id,
            "ts_utc": _ts(current),
            "symbol": symbol,
            "timeframe": timeframe,
            "price_at_signal": px,
            "price_source": price_source,
            "stop_pct": stop_pct,
            "exit_hours": hours,
            "rationale": rationale,
            "action": action,
            "sensor_policy_source": sensor_policy_source,
            "config_version": version,
            "gates_passed": gates_passed,
            "gate_reject_reason": gate_reject_reason,
        }
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
    row = {
        "type": "shadow_exit",
        "signal_id": signal_id,
        "ts_utc": _ts(now),
        "exit_price": exit_price,
        "price_source": price_source,
        "exit_reason": reason,
        "ret_net_pct": ret,
        "control": control,
        "excluded_from_evaluation": excluded,
    }
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
    if isinstance(coin, dict) and coin.get("quote_volume_24h_usdt") not in (None, ""):
        try:
            vol = float(coin["quote_volume_24h_usdt"])
        except (TypeError, ValueError):
            vol = None
    if vol is None or vol <= 0:
        from services.venue_quality import peek_cached_quote_volume

        vol = peek_cached_quote_volume(symbol, config_raw=config_raw)
    if vol is not None and vol > 0:
        info["quote_volume_24h_usdt"] = float(vol)
    return info
