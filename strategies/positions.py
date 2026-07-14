import json
import os
import threading
import time
from datetime import datetime
from decimal import Decimal

from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id, resolve_tenant_scope
from logger import log
from storage.ledger_router import (
    ORDERS_SCOPE_FILES,
    POSITIONS_SCOPE_FILES,
)
from data_manager import (
    atomic_write_json,
    load_positions_document,
    resolve_ledger_scope,
    save_positions_document,
)

# Basic lock to reduce risk of concurrent modifications (price loop + Flask).
_positions_lock = threading.RLock()

_FLUSH_DEBOUNCE_SEC = 5.0
_flush_timer: threading.Timer | None = None
_flush_timer_lock = threading.RLock()
_position_stores: dict[tuple[str, str], dict] = {}
_open_counts: dict[tuple[str, str], int] = {}
_active_key: tuple[str, str] = (DEFAULT_TENANT, "paper")
_open_positions_count = 0
positions: dict = {}


def _resolve_store_key(
    scope: str | None = None, tenant_id: str | None = None
) -> tuple[str, str]:
    tid = resolve_tenant_id(tenant_id)
    sc = scope if scope is not None else resolve_tenant_scope()
    return (tid, sc)


def _ensure_store(key: tuple[str, str]) -> dict:
    if key not in _position_stores:
        _position_stores[key] = {}
        _open_counts[key] = 0
    return _position_stores[key]


def _active_store() -> dict:
    if _active_key[0] == DEFAULT_TENANT:
        return positions
    return _ensure_store(_active_key)


def _activate(key: tuple[str, str]) -> None:
    global _active_key, _open_positions_count
    _active_key = key
    _open_counts.setdefault(key, 0)
    _open_positions_count = _open_counts[key]

DUST_AMOUNT_EPSILON = 1e-12
MIN_OPEN_POSITION_USDT = 1.0

_CACHE_FIELDS = (
    "strategy_tier",
    "exit_ladder_step",
    "dca_rounds",
    "dca_max_rounds",
    "last_dca_at",
    "dca_total_usdt",
    "dca_recovery_rounds",
    "dca_recovery_max_rounds",
    "last_dca_recovery_at",
    "last_recovery_ref_price",
    "last_sell_signal",
    "rsi_sell_tiers_done",
    "last_cmc_sell_at",
    "recent_high",
    "last_ampel",
    "last_rsi",
    "first_buy_at",
    "entry_source",
    "entry_at",
    "entry_15m_vol_ratio",
    "time_profit_exit_done",
    "profit_armed_at",
    "trail_tp_steps",
    "last_trail_tp_at",
    "profit_max_lifetime_done",
)


def resolve_positions_file(scope):
    if scope not in POSITIONS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    if scope == "demo":
        from data_manager import get_data_file

        return get_data_file("positions.json")
    return POSITIONS_SCOPE_FILES[scope]


def position_notional_usdt(pos: dict) -> float:
    amount = float(pos.get("amount", 0) or 0)
    if amount <= 0:
        return 0.0
    for key in ("average_entry", "entry_price", "last_buy_price"):
        entry = float(pos.get(key, 0) or 0)
        if entry > 0:
            return amount * entry
    return 0.0


def has_position_amount(pos: dict) -> bool:
    return float(pos.get("amount", 0) or 0) > DUST_AMOUNT_EPSILON


def is_open_position(pos: dict) -> bool:
    """True when the lot is material (BTC-sized fractions, not token-dust)."""
    if not has_position_amount(pos):
        return False
    notional = position_notional_usdt(pos)
    if notional > 0:
        return notional >= MIN_OPEN_POSITION_USDT
    return True


def get_active_scope() -> str:
    return _active_key[1]


def _deserialize_position(raw: dict) -> dict:
    amount = Decimal(str(raw.get("amount", 0)))
    peak = float(raw.get("peak_amount", 0) or 0)
    if peak <= 0 and float(amount) > 0:
        sold = float(raw.get("sold_percent", 0) or 0)
        if 0 < sold < 1:
            peak = float(amount) / (1.0 - sold)
        else:
            peak = float(amount)
    return {
        "amount": amount,
        "peak_amount": peak,
        "sold_percent": float(raw.get("sold_percent", 0)),
        "average_entry": float(raw.get("average_entry", raw.get("entry_price", 0))),
        "realized_pnl": float(raw.get("realized_pnl", 0)),
        "last_buy_price": float(raw.get("last_buy_price", 0)),
        "last_ampel": raw.get("last_ampel", "🟡"),
        "last_rsi": float(raw.get("last_rsi", 45.0)),
        "last_action": raw.get("last_action"),
        "last_trade_at": raw.get("last_trade_at"),
        "last_trade_type": raw.get("last_trade_type"),
        "rsi_sell_tiers_done": dict(raw.get("rsi_sell_tiers_done") or {}),
        "last_cmc_sell_at": raw.get("last_cmc_sell_at"),
        "recent_high": float(raw.get("recent_high", 0)),
        "strategy_tier": raw.get("strategy_tier"),
        "exit_ladder_step": int(raw.get("exit_ladder_step", 0) or 0),
        "dca_rounds": int(raw.get("dca_rounds", 0) or 0),
        "dca_max_rounds": int(raw.get("dca_max_rounds", 0) or 0),
        "last_dca_at": raw.get("last_dca_at"),
        "dca_total_usdt": float(raw.get("dca_total_usdt", 0) or 0),
        "dca_recovery_rounds": int(raw.get("dca_recovery_rounds", 0) or 0),
        "dca_recovery_max_rounds": int(raw.get("dca_recovery_max_rounds", 0) or 0),
        "last_dca_recovery_at": raw.get("last_dca_recovery_at"),
        "last_recovery_ref_price": float(raw.get("last_recovery_ref_price", 0) or 0),
        "last_sell_signal": raw.get("last_sell_signal"),
        "first_buy_at": raw.get("first_buy_at"),
        "entry_source": raw.get("entry_source"),
        "entry_at": raw.get("entry_at"),
        "entry_15m_vol_ratio": float(raw.get("entry_15m_vol_ratio", 0) or 0) or None,
        "time_profit_exit_done": bool(raw.get("time_profit_exit_done", False)),
        "profit_armed_at": raw.get("profit_armed_at"),
        "trail_tp_steps": int(raw.get("trail_tp_steps", 0) or 0),
        "last_trail_tp_at": raw.get("last_trail_tp_at"),
        "profit_max_lifetime_done": bool(raw.get("profit_max_lifetime_done", False)),
    }


def _serialize_positions() -> dict:
    store = _active_store()
    data = {"positions": {}, "ledger_scope": _active_key[1]}
    for tf, p in store.items():
        data["positions"][tf] = {
            "amount": float(p["amount"]),
            "peak_amount": float(p.get("peak_amount", 0) or 0),
            "sold_percent": p["sold_percent"],
            "average_entry": float(p.get("average_entry", p.get("entry_price", 0))),
            "realized_pnl": float(p.get("realized_pnl", 0)),
            "last_buy_price": p["last_buy_price"],
            "last_ampel": p.get("last_ampel", "🟡"),
            "last_rsi": p.get("last_rsi", 45.0),
            "last_action": p.get("last_action"),
            "last_trade_at": p.get("last_trade_at"),
            "last_trade_type": p.get("last_trade_type"),
            "rsi_sell_tiers_done": dict(p.get("rsi_sell_tiers_done") or {}),
            "last_cmc_sell_at": p.get("last_cmc_sell_at"),
            "recent_high": float(p.get("recent_high", 0)),
            "strategy_tier": p.get("strategy_tier"),
            "exit_ladder_step": int(p.get("exit_ladder_step", 0) or 0),
            "dca_rounds": int(p.get("dca_rounds", 0) or 0),
            "dca_max_rounds": int(p.get("dca_max_rounds", 0) or 0),
            "last_dca_at": p.get("last_dca_at"),
            "dca_total_usdt": float(p.get("dca_total_usdt", 0) or 0),
            "dca_recovery_rounds": int(p.get("dca_recovery_rounds", 0) or 0),
            "dca_recovery_max_rounds": int(p.get("dca_recovery_max_rounds", 0) or 0),
            "last_dca_recovery_at": p.get("last_dca_recovery_at"),
            "last_recovery_ref_price": float(p.get("last_recovery_ref_price", 0) or 0),
            "last_sell_signal": p.get("last_sell_signal"),
            "first_buy_at": p.get("first_buy_at"),
            "entry_source": p.get("entry_source"),
            "entry_at": p.get("entry_at"),
            "entry_15m_vol_ratio": p.get("entry_15m_vol_ratio"),
            "time_profit_exit_done": bool(p.get("time_profit_exit_done", False)),
            "profit_armed_at": p.get("profit_armed_at"),
            "trail_tp_steps": int(p.get("trail_tp_steps", 0) or 0),
            "last_trail_tp_at": p.get("last_trail_tp_at"),
            "profit_max_lifetime_done": bool(p.get("profit_max_lifetime_done", False)),
        }
    return data


def _recompute_open_count() -> None:
    """Recompute open-position counter; caller must hold _positions_lock."""
    global _open_positions_count
    store = _active_store()
    count = sum(1 for p in store.values() if is_open_position(p))
    _open_counts[_active_key] = count
    _open_positions_count = count


_DCA_ORDER_PRIORITY_FIELDS = (
    "dca_rounds",
    "dca_recovery_rounds",
    "last_dca_at",
    "last_dca_recovery_at",
    "dca_total_usdt",
)


def derive_positions_from_orders_and_cache(order_snap: dict, cache_doc: dict) -> dict:
    """Derive amounts from orders; merge cache fields; keep material cache-only lots."""
    merged = {}
    cache_positions = cache_doc.get("positions", {}) or {}
    for key, snap in order_snap.items():
        pos = dict(snap)
        cached = cache_positions.get(key) or {}
        for field in _CACHE_FIELDS:
            if field in _DCA_ORDER_PRIORITY_FIELDS:
                continue
            if field in cached and cached[field] is not None:
                pos[field] = cached[field]
        for field in _DCA_ORDER_PRIORITY_FIELDS:
            order_val = snap.get(field)
            cached_val = cached.get(field)
            if field in ("dca_rounds", "dca_recovery_rounds"):
                best = max(int(order_val or 0), int(cached_val or 0))
                if best > 0 or cached_val is not None or order_val is not None:
                    pos[field] = best
            elif order_val is not None:
                pos[field] = order_val
            elif cached_val is not None:
                pos[field] = cached_val
        merged[key] = pos

    for key, cached in cache_positions.items():
        if key in merged:
            continue
        raw = dict(cached)
        if not is_open_position(raw):
            continue
        merged[key] = raw
    return merged


def _merge_cache_fields(order_snap: dict, cache_doc: dict) -> dict:
    return derive_positions_from_orders_and_cache(order_snap, cache_doc)


def apply_positions_snapshot(snapshot: dict, scope: str = None) -> None:
    key = _resolve_store_key(scope)
    _activate(key)
    store = _active_store()
    with _positions_lock:
        store.clear()
        for pos_key, raw in snapshot.items():
            store[pos_key] = _deserialize_position(raw)
        _recompute_open_count()


def load_positions(scope: str = None, tenant_id: str | None = None):
    """Load positions: amounts from orders (source of truth), cache fields from ledger doc."""
    from services.ledger_sync import _build_positions_snapshot_from_orders

    key = _resolve_store_key(scope, tenant_id)
    target = key[1]
    _activate(key)
    store = _active_store()
    with _positions_lock:
        store.clear()
        try:
            order_snap = _build_positions_snapshot_from_orders(target, tenant_id=key[0])
            cache_doc = load_positions_document(target, tenant_id=key[0])
            merged = derive_positions_from_orders_and_cache(order_snap, cache_doc)
            for pos_key, raw in merged.items():
                store[pos_key] = _deserialize_position(raw)
            _recompute_open_count()
        except Exception as e:
            log(f"Failed to load positions ({target}): {e}", "ERROR")
        snapshot = {k: dict(v) for k, v in store.items()}
    return snapshot


def bootstrap_positions(scope: str = None) -> None:
    """Explicit startup load (not at import time)."""
    load_positions(scope=scope)


def clear_positions_memory(tenant_id: str | None = None, scope: str | None = None) -> None:
    """Reset in-memory positions and the open-position counter (tests / scope prep)."""
    key = _resolve_store_key(scope, tenant_id)
    _activate(key)
    store = _active_store()
    with _positions_lock:
        store.clear()
        _recompute_open_count()


def _cancel_flush_timer() -> None:
    global _flush_timer
    with _flush_timer_lock:
        if _flush_timer is not None:
            _flush_timer.cancel()
            _flush_timer = None


def _do_save_positions(scope: str) -> None:
    target = scope or _active_key[1]
    with _positions_lock:
        payload = _serialize_positions()
        payload["ledger_scope"] = target
        try:
            if not save_positions_document(payload, target, tenant_id=_active_key[0]):
                log(f"Failed to save positions ({target})", "ERROR")
        except Exception as e:
            log(f"Failed to save positions ({target}): {e}", "ERROR")


def flush_positions(scope: str = None, *, force: bool = False) -> None:
    """Persist positions; debounced unless force=True (trade/shutdown)."""
    global _flush_timer
    target = scope or _active_key[1]
    if force:
        _cancel_flush_timer()
        _do_save_positions(target)
        return

    def _delayed():
        _do_save_positions(target)

    with _flush_timer_lock:
        _cancel_flush_timer()
        _flush_timer = threading.Timer(_FLUSH_DEBOUNCE_SEC, _delayed)
        _flush_timer.daemon = True
        _flush_timer.start()


def save_positions(scope: str = None):
    flush_positions(scope, force=True)


def update_market_snapshot(
    symbol: str,
    timeframe: str,
    current_price: float,
    atr_pct: float = 0.0,
    *,
    peak_hint: float | None = None,
) -> bool:
    """Bump recent_high when price makes a new peak. Returns True if peak changed."""
    _activate(_resolve_store_key())
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    store = _active_store()
    changed = False
    candidate = max(float(current_price), float(peak_hint or 0))
    with _positions_lock:
        pos = store[key]
        old_high = float(pos.get("recent_high") or 0)
        new_high = max(old_high, candidate)
        if new_high > old_high:
            pos["recent_high"] = new_high
            changed = True
    if changed:
        flush_positions()
    return changed


def lock_strategy_tier(symbol: str, timeframe: str, tier: str) -> None:
    if tier not in ("stable", "volatile"):
        return
    _activate(_resolve_store_key())
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    store = _active_store()
    with _positions_lock:
        if not store[key].get("strategy_tier"):
            store[key]["strategy_tier"] = tier
    flush_positions()


def get_key(symbol, timeframe):
    return f"{symbol.replace('/', '_')}_{timeframe}"


def init_position(symbol, timeframe):
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    store = _active_store()
    with _positions_lock:
        if key not in store:
            store[key] = {
                "amount": Decimal("0"),
                "peak_amount": 0.0,
                "sold_percent": 0.0,
                "average_entry": 0.0,
                "realized_pnl": 0.0,
                "last_buy_price": 0.0,
                "last_ampel": "🟡",
                "last_rsi": 45.0,
                "last_action": None,
                "last_trade_at": None,
                "last_trade_type": None,
                "rsi_sell_tiers_done": {},
                "last_cmc_sell_at": None,
                "recent_high": 0.0,
                "strategy_tier": None,
                "exit_ladder_step": 0,
                "dca_rounds": 0,
                "dca_max_rounds": 0,
                "last_dca_at": None,
                "dca_total_usdt": 0.0,
                "dca_recovery_rounds": 0,
                "dca_recovery_max_rounds": 0,
                "last_dca_recovery_at": None,
                "last_recovery_ref_price": 0.0,
                "last_sell_signal": None,
                "first_buy_at": None,
                "entry_source": None,
                "entry_at": None,
                "entry_15m_vol_ratio": None,
                "time_profit_exit_done": False,
                "profit_armed_at": None,
                "trail_tp_steps": 0,
                "last_trail_tp_at": None,
                "profit_max_lifetime_done": False,
            }


def get_position(symbol, timeframe):
    init_position(symbol, timeframe)
    store = _active_store()
    with _positions_lock:
        return store[get_key(symbol, timeframe)]


def set_position_field(symbol: str, timeframe: str, field: str, value) -> None:
    """Update one position field under the positions lock."""
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    store = _active_store()
    with _positions_lock:
        store[key][field] = value


def reset_rsi_sell_tiers_if_cooled(
    symbol: str,
    timeframe: str,
    current_rsi: float,
    rsi_sell_30: float,
    rsi_sell_20: float,
    buffer: float = 5.0,
):
    """Clear sell-tier flags after RSI drops below threshold minus buffer."""
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    store = _active_store()
    changed = False
    with _positions_lock:
        pos = store[key]
        tiers = dict(pos.get("rsi_sell_tiers_done") or {})
        if tiers.get("30") and current_rsi < rsi_sell_30 - buffer:
            tiers["30"] = False
            changed = True
        if tiers.get("20") and current_rsi < rsi_sell_20 - buffer:
            tiers["20"] = False
            changed = True
        if tiers.get("tp") and current_rsi < rsi_sell_30 - buffer:
            tiers["tp"] = False
            changed = True
        if changed:
            pos["rsi_sell_tiers_done"] = tiers
    if changed:
        flush_positions()


def is_rsi_sell_tier_done(symbol: str, timeframe: str, tier: str) -> bool:
    pos = get_position(symbol, timeframe)
    return bool((pos.get("rsi_sell_tiers_done") or {}).get(tier))


def mark_time_profit_exit_done(symbol: str, timeframe: str) -> None:
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    store = _active_store()
    with _positions_lock:
        pos = store[key]
        if pos.get("time_profit_exit_done"):
            return
        pos["time_profit_exit_done"] = True
    flush_positions()


def mark_trailing_take_profit_step(symbol: str, timeframe: str, current_price: float) -> None:
    """Cooldown + reset recent_high after trail TP; sizing stays on exit ladder."""
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    with _positions_lock:
        pos = positions[key]
        pos["last_trail_tp_at"] = datetime.now().isoformat()
        pos["recent_high"] = float(current_price)
    flush_positions()


def mark_profit_max_lifetime_done(symbol: str, timeframe: str) -> None:
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    with _positions_lock:
        pos = positions[key]
        if pos.get("profit_max_lifetime_done"):
            return
        pos["profit_max_lifetime_done"] = True
    flush_positions()


def _is_dca_buy_signal(signal: str) -> bool:
    return (signal or "").upper() == "BUY_DCA"


def _is_addon_buy(old_amount, position: dict) -> bool:
    """True when adding to an existing open lot (must preserve DCA / ladder state)."""
    return float(old_amount) > 0


def sell_fraction_for_signal(
    signal: str,
    symbol: str | None = None,
    timeframe: str | None = None,
    price: float = 0.0,
    strategy_params: dict | None = None,
) -> float:
    """Map sell signal names to fraction of position to close."""
    if symbol and timeframe and strategy_params:
        from strategies.exit_ladder import resolve_sell_fraction

        ladder_frac = resolve_sell_fraction(signal, symbol, timeframe, price, strategy_params)
        if ladder_frac is not None:
            return ladder_frac

    if signal in ("SELL_STOP_FULL", "SELL_FULL"):
        return 1.0
    if signal == "SELL_STOP_PARTIAL":
        return 0.5
    if signal in ("SELL_30", "SELL_TP", "SELL_PARTIAL_30"):
        return 0.3
    if signal in ("SELL_10", "SELL_PARTIAL_10"):
        return 0.1
    if signal == "SELL_20":
        return 0.2
    if "FULL" in signal:
        return 1.0
    if "PARTIAL" in signal:
        return 0.5
    if "30" in signal:
        return 0.3
    return 0.2


def update_position(
    symbol,
    timeframe,
    signal,
    current_price,
    amount_traded=0,
    *,
    entry_source: str | None = None,
    entry_15m_vol_ratio: float | None = None,
):
    global _open_positions_count
    _activate(_resolve_store_key())
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    store = _active_store()
    was_open = False
    with _positions_lock:
        pos = store[key]
        was_open = is_open_position(pos)
        if signal in ("BUY", "BUY_DCA") and amount_traded > 0:
            old_amount = pos["amount"]
            old_average = pos.get("average_entry", current_price)
            new_amount = old_amount + Decimal(str(amount_traded))
            if old_amount > 0:
                pos["average_entry"] = float(
                    (old_average * float(old_amount) + current_price * float(amount_traded))
                    / float(new_amount)
                )
            else:
                pos["average_entry"] = current_price
            pos["amount"] = new_amount
            pos["last_buy_price"] = current_price
            pos["last_trade_at"] = datetime.now().isoformat()
            if _is_dca_buy_signal(signal) and _is_addon_buy(old_amount, pos):
                pos["last_action"] = "BUY_DCA"
                pos["last_trade_type"] = "BUY_DCA"
                usdt_added = current_price * float(amount_traded)
                pos["dca_rounds"] = int(pos.get("dca_rounds", 0) or 0) + 1
                pos["last_dca_at"] = datetime.now().isoformat()
                pos["last_recovery_ref_price"] = current_price
                pos["dca_total_usdt"] = float(pos.get("dca_total_usdt", 0) or 0) + usdt_added
                if not int(pos.get("dca_max_rounds", 0) or 0):
                    from strategies.dca import dca_config as _dca_cfg

                    params = None
                    try:
                        from strategies.registry import resolve_strategy_params

                        params = resolve_strategy_params(
                            {"symbol": symbol, "timeframe": timeframe},
                            has_position=True,
                            frozen_tier=pos.get("strategy_tier"),
                        )
                    except Exception:
                        params = None
                    cfg = _dca_cfg(params)
                    pos["dca_max_rounds"] = int(cfg.get("max_rounds", 3))
            elif _is_addon_buy(old_amount, pos):
                pos["last_action"] = "BUY"
                pos["last_trade_type"] = "BUY"
                if entry_source and not pos.get("entry_source"):
                    pos["entry_source"] = entry_source
                if entry_15m_vol_ratio is not None:
                    pos["entry_15m_vol_ratio"] = float(entry_15m_vol_ratio)
            else:
                pos["peak_amount"] = float(new_amount)
                pos["sold_percent"] = 0.0
                pos["last_action"] = "BUY"
                pos["rsi_sell_tiers_done"] = {}
                pos["recent_high"] = current_price
                pos["exit_ladder_step"] = 0
                pos["last_trade_type"] = "BUY"
                pos["dca_rounds"] = 0
                pos["dca_max_rounds"] = 0
                pos["last_dca_at"] = None
                pos["dca_total_usdt"] = 0.0
                pos["dca_recovery_rounds"] = 0
                pos["dca_recovery_max_rounds"] = 0
                pos["last_dca_recovery_at"] = None
                pos["last_recovery_ref_price"] = 0.0
                pos["time_profit_exit_done"] = False
                pos["profit_armed_at"] = None
                pos["trail_tp_steps"] = 0
                pos["last_trail_tp_at"] = None
                pos["profit_max_lifetime_done"] = False
                pos["first_buy_at"] = datetime.now().isoformat()
                if entry_source:
                    pos["entry_source"] = entry_source
                    pos["entry_at"] = pos["first_buy_at"]
                if entry_15m_vol_ratio is not None:
                    pos["entry_15m_vol_ratio"] = float(entry_15m_vol_ratio)
                pos["strategy_tier"] = None
        elif "SELL" in signal:
            original_amount = float(pos["amount"])
            strategy_params = None
            try:
                from strategies.registry import resolve_strategy_params

                strategy_params = resolve_strategy_params(
                    {"symbol": symbol, "timeframe": timeframe},
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier"),
                )
            except Exception:
                strategy_params = None
            if amount_traded > 0:
                sell_amount = min(Decimal(str(amount_traded)), pos["amount"])
            else:
                fraction = sell_fraction_for_signal(
                    signal, symbol, timeframe, current_price, strategy_params,
                )
                sell_amount = pos["amount"] * Decimal(str(fraction))
            peak = float(pos.get("peak_amount") or 0)
            if peak <= 0 and original_amount > 0:
                peak = original_amount
                pos["peak_amount"] = peak
            pos["amount"] -= sell_amount
            if peak > 0:
                pos["sold_percent"] = min(
                    1.0, max(0.0, 1.0 - float(pos["amount"]) / peak)
                )
            pos["last_action"] = "SELL"
            pos["last_trade_at"] = datetime.now().isoformat()
            pos["last_trade_type"] = "SELL"
            pos["last_sell_signal"] = signal
            tiers = dict(pos.get("rsi_sell_tiers_done") or {})
            if "TP" in signal.upper():
                from strategies.take_profit import mark_triggered_tier

                entry = float(pos.get("average_entry") or 0)
                gain_pct = (
                    (current_price / entry - 1) * 100 if entry > 0 else 0.0
                )
                tp_tiers = (strategy_params or {}).get("take_profit_tiers") or []
                tiers = mark_triggered_tier(tiers, gain_pct, tp_tiers)
            elif "30" in signal:
                tiers["30"] = True
            elif "20" in signal:
                tiers["20"] = True
            pos["rsi_sell_tiers_done"] = tiers
            if strategy_params:
                from strategies.exit_ladder import advance_ladder_step

                advance_ladder_step(
                    pos,
                    signal,
                    strategy_params,
                    amount_sold=float(sell_amount),
                    amount_before=original_amount,
                )
        if pos["amount"] < 0:
            pos["amount"] = Decimal("0")
        is_open_now = is_open_position(pos)
        if was_open and not is_open_now:
            _open_counts[_active_key] = max(0, _open_counts[_active_key] - 1)
            _open_positions_count = _open_counts[_active_key]
        elif not was_open and is_open_now:
            _open_counts[_active_key] += 1
            _open_positions_count = _open_counts[_active_key]
    flush_positions(force=True)


def count_open_positions():
    with _positions_lock:
        return _open_positions_count


def count_open_full_slots(config_raw: dict | None = None) -> int:
    from strategies.sell_rotation_policy import is_tail_position as _is_tail, rotation_config

    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    cfg = rotation_config(config_raw)
    with _positions_lock:
        return sum(
            1 for p in positions.values()
            if is_open_position(p) and not _is_tail(p, cfg)
        )


def count_open_tail_slots(config_raw: dict | None = None) -> int:
    from strategies.sell_rotation_policy import is_tail_position as _is_tail, rotation_config

    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    cfg = rotation_config(config_raw)
    with _positions_lock:
        return sum(
            1 for p in positions.values()
            if is_open_position(p) and _is_tail(p, cfg)
        )


def get_total_aria():
    store = _active_store()
    with _positions_lock:
        total = Decimal("0")
        for pos in store.values():
            total += pos["amount"]
        return total


def list_active_positions():
    store = _active_store()
    with _positions_lock:
        active = []
        for key, p in store.items():
            if is_open_position(p):
                base, _, tf = key.rpartition("_")
                symbol = base.replace("_", "/") if "/" not in base else base
                if not symbol.upper().startswith("TEST"):
                    highlight = "🔥 " if p.get("last_action") == "BUY" else ""
                    active.append({
                        "symbol": symbol,
                        "timeframe": tf,
                        "amount": float(p["amount"]),
                        "average_entry": p.get("average_entry", 0),
                        "entry_price": p.get("average_entry", 0),
                        "last_buy_price": p.get("last_buy_price", 0),
                        "realized_pnl": p.get("realized_pnl", 0),
                        "peak_amount": float(p.get("peak_amount", 0) or 0),
                        "sold_percent": float(p.get("sold_percent", 0)),
                        "last_action": p.get("last_action"),
                        "highlight": highlight,
                    })
        return active