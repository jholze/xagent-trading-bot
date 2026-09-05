import json
import os
import threading
import time
from datetime import datetime, timezone
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
# "known" | "unknown" — failed load must not look like an empty book (#318).
_positions_state: dict[tuple[str, str], str] = {}
_positions_unknown_logged: set[tuple[str, str]] = set()
_flush_unknown_logged: set[tuple[str, str]] = set()


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


def _store_for_key(key: tuple[str, str]) -> dict:
    if key[0] == DEFAULT_TENANT:
        return positions
    return _ensure_store(key)


def _activate(key: tuple[str, str]) -> None:
    global _active_key, _open_positions_count
    _active_key = key
    _open_counts.setdefault(key, 0)
    _open_positions_count = _open_counts[key]


def is_positions_state_unknown(
    tenant_id: str | None = None, scope: str | None = None
) -> bool:
    """True when the last positions load for this tenant/scope failed."""
    return _positions_state.get(_resolve_store_key(scope, tenant_id)) == "unknown"


def _mark_positions_known(key: tuple[str, str]) -> None:
    _positions_state[key] = "known"
    _positions_unknown_logged.discard(key)
    _flush_unknown_logged.discard(key)


def _mark_positions_unknown(key: tuple[str, str], err: BaseException) -> None:
    _positions_state[key] = "unknown"
    if key not in _positions_unknown_logged:
        log(
            f"Positions state unknown ({key[0]}/{key[1]}): {err}",
            "ERROR",
        )
        _positions_unknown_logged.add(key)

DUST_AMOUNT_EPSILON = 1e-12
MIN_OPEN_POSITION_USDT = 1.0

_CACHE_FIELDS = (
    "strategy_tier",
    "exit_ladder_step",
    "dca_rounds",
    "dca_max_rounds",
    "last_dca_at",
    "last_scheduled_dca_at",
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
    "lock",  # position_lock: no_auto_sell / no_evict (no_dca optional)
    "side",
    "leverage",
    "recent_low",
)


def resolve_positions_file(scope):
    if scope not in POSITIONS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    from data_manager import get_data_file, resolve_data_path

    if scope == "demo":
        return get_data_file("positions.json")
    return resolve_data_path(POSITIONS_SCOPE_FILES[scope])


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
        "last_scheduled_dca_at": raw.get("last_scheduled_dca_at"),
        "dca_total_usdt": float(raw.get("dca_total_usdt", 0) or 0),
        "dca_recovery_rounds": int(raw.get("dca_recovery_rounds", 0) or 0),
        "dca_recovery_max_rounds": int(raw.get("dca_recovery_max_rounds", 0) or 0),
        "last_dca_recovery_at": raw.get("last_dca_recovery_at"),
        "last_recovery_ref_price": float(raw.get("last_recovery_ref_price", 0) or 0),
        "last_sell_signal": raw.get("last_sell_signal"),
        "first_buy_at": raw.get("first_buy_at"),
        "entry_source": raw.get("entry_source"),
        "entry_at": raw.get("entry_at"),
        "entry_15m_vol_ratio": float(raw.get("entry_15m_vol_ratio", 0) or 0),
        "time_profit_exit_done": bool(raw.get("time_profit_exit_done", False)),
        "profit_armed_at": raw.get("profit_armed_at"),
        "trail_tp_steps": int(raw.get("trail_tp_steps", 0) or 0),
        "last_trail_tp_at": raw.get("last_trail_tp_at"),
        "profit_max_lifetime_done": bool(raw.get("profit_max_lifetime_done", False)),
        "lock": dict(raw["lock"]) if isinstance(raw.get("lock"), dict) else None,
        "side": str(raw.get("side") or "long").strip().lower() or "long",
        "leverage": float(raw.get("leverage") or 0) or None,
        "recent_low": float(raw["recent_low"]) if raw.get("recent_low") not in (None, "") else None,
    }


def _position_persistable(p: dict) -> bool:
    """Skip empty init_position shells; keep lots with size or trade history."""
    if has_position_amount(p):
        return True
    if p.get("first_buy_at") or p.get("last_action"):
        return True
    if float(p.get("realized_pnl", 0) or 0) != 0:
        return True
    return False


def _serialize_positions() -> dict:
    store = _active_store()
    data = {"positions": {}, "ledger_scope": _active_key[1]}
    for tf, p in store.items():
        if not _position_persistable(p):
            continue
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
            "last_scheduled_dca_at": p.get("last_scheduled_dca_at"),
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
        lock = p.get("lock")
        if isinstance(lock, dict) and lock:
            data["positions"][tf]["lock"] = dict(lock)
        side = str(p.get("side") or "long").strip().lower()
        if side == "short":
            data["positions"][tf]["side"] = "short"
        lev = p.get("leverage")
        if lev:
            data["positions"][tf]["leverage"] = float(lev)
        if p.get("recent_low"):
            data["positions"][tf]["recent_low"] = float(p["recent_low"])
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


def derive_positions_from_orders_and_cache(
    order_snap: dict,
    cache_doc: dict,
    *,
    tenant_id: str | None = None,
) -> dict:
    """Derive amounts from orders; merge cache fields; keep material cache-only lots."""
    from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id

    tid = resolve_tenant_id(tenant_id)
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
        from strategies.exit_ladder import reconcile_exit_ladder_step

        reconcile_exit_ladder_step(pos)
        merged[key] = pos

    from data_manager import get_config
    from core.simulated_trading import uses_order_ledger_cash

    # Demo/mongo: orders replay cash — cache-only lots inflate NAV without reducing cash.
    if not uses_order_ledger_cash(get_config()):
        for key, cached in cache_positions.items():
            if key in merged:
                continue
            raw = dict(cached)
            if not is_open_position(raw):
                continue
            merged[key] = raw
    return merged


def prune_orphan_position_cache(
    order_snap: dict,
    cache_doc: dict,
) -> tuple[dict, list[str]]:
    """Remove position-cache keys with no matching order lot (order-ledger cash mode)."""
    from data_manager import get_config
    from core.simulated_trading import uses_order_ledger_cash

    if not uses_order_ledger_cash(get_config()):
        return cache_doc, []
    positions = dict(cache_doc.get("positions") or {})
    orphans = sorted(k for k in positions if k not in order_snap)
    if not orphans:
        return cache_doc, []
    for key in orphans:
        positions.pop(key, None)
    pruned = dict(cache_doc)
    pruned["positions"] = positions
    return pruned, orphans


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
    from storage.errors import LedgerUnavailable

    key = _resolve_store_key(scope, tenant_id)
    target = key[1]
    _activate(key)
    store = _active_store()
    with _positions_lock:
        try:
            order_snap = _build_positions_snapshot_from_orders(target, tenant_id=key[0])
            cache_doc = load_positions_document(target, tenant_id=key[0])
            merged = derive_positions_from_orders_and_cache(
                order_snap, cache_doc, tenant_id=key[0]
            )
            store.clear()
            for pos_key, raw in merged.items():
                store[pos_key] = _deserialize_position(raw)
            _recompute_open_count()
            _mark_positions_known(key)
        except LedgerUnavailable as e:
            _mark_positions_unknown(key, e)
        except Exception as e:
            log(f"Failed to load positions ({target}): {e}", "ERROR")
            _mark_positions_unknown(key, e)
        snapshot = {k: dict(v) for k, v in store.items()}
    return snapshot


def bootstrap_positions(scope: str = None, tenant_id: str | None = None) -> None:
    """Explicit startup load (not at import time)."""
    load_positions(scope=scope, tenant_id=tenant_id)


def activate_tenant_positions(
    *,
    scope: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Switch in-memory store to the active tenant before cycle / command reads.

    Satellite tenants always reload from the order ledger so RAM cannot drift from
    Mongo (out-of-process fills, missed first bootstrap, empty shell store).
    """
    key = _resolve_store_key(scope, tenant_id)
    _activate(key)
    if key[0] == DEFAULT_TENANT:
        return
    bootstrap_positions(scope=key[1], tenant_id=key[0])


def clear_positions_memory(tenant_id: str | None = None, scope: str | None = None) -> None:
    """Reset in-memory positions and the open-position counter (tests / scope prep)."""
    key = _resolve_store_key(scope, tenant_id)
    _activate(key)
    store = _active_store()
    with _positions_lock:
        store.clear()
        _recompute_open_count()
        _positions_state.pop(key, None)
        _positions_unknown_logged.discard(key)
        _flush_unknown_logged.discard(key)


def _cancel_flush_timer() -> None:
    global _flush_timer
    with _flush_timer_lock:
        if _flush_timer is not None:
            _flush_timer.cancel()
            _flush_timer = None


def _preserve_locks_from_existing_doc(payload: dict, existing: dict | None) -> dict:
    """Keep active locks from Mongo when in-memory serialize omitted them.

    Ops can set lock out-of-process; a stale bot flush must not wipe it.
    An explicit unlock (lock key present, including enabled=false) is SoT and
    must not be restored from the previous Mongo doc.
    """
    if not isinstance(payload, dict) or not isinstance(existing, dict):
        return payload
    pos_out = payload.get("positions")
    pos_old = existing.get("positions")
    if not isinstance(pos_out, dict) or not isinstance(pos_old, dict):
        return payload
    for key, row in pos_out.items():
        if not isinstance(row, dict):
            continue
        if "lock" in row:
            continue
        old_lock = (pos_old.get(key) or {}).get("lock") if isinstance(pos_old.get(key), dict) else None
        if isinstance(old_lock, dict) and old_lock.get("enabled", True):
            row["lock"] = dict(old_lock)
    return payload


def _flush_refused_unknown(key: tuple[str, str]) -> bool:
    if _positions_state.get(key) != "unknown":
        return False
    if key not in _flush_unknown_logged:
        log(
            f"flush_positions refused: positions state unknown ({key[0]}/{key[1]})",
            "ERROR",
        )
        _flush_unknown_logged.add(key)
    return True


def _do_save_positions(scope: str, *, tenant_id: str | None = None) -> None:
    from core.tenant_context import resolve_tenant_id
    from storage.errors import LedgerUnavailable

    target = scope or _active_key[1]
    tid = resolve_tenant_id(tenant_id)
    store_key = (tid, target)
    if _flush_refused_unknown(store_key):
        return
    with _positions_lock:
        prev_key = _active_key
        _activate(store_key)
        try:
            payload = _serialize_positions()
            payload["ledger_scope"] = target
            try:
                existing = load_positions_document(target, tenant_id=tid)
                payload = _preserve_locks_from_existing_doc(payload, existing)
            except LedgerUnavailable as e:
                log(f"flush aborted, positions unread ({target}): {e}", "ERROR")
                _mark_positions_unknown(store_key, e)
                return
            except Exception as e:
                log(f"lock preserve on save skip ({target}): {e}", "DEBUG")
            if not save_positions_document(payload, target, tenant_id=tid):
                log(f"Failed to save positions ({target})", "ERROR")
        except Exception as e:
            log(f"Failed to save positions ({target}): {e}", "ERROR")
        finally:
            _activate(prev_key)


def flush_positions(scope: str = None, *, force: bool = False) -> None:
    """Persist positions; debounced unless force=True (trade/shutdown)."""
    global _flush_timer
    target = scope or _active_key[1]
    pinned_tenant = _active_key[0]
    if _flush_refused_unknown((pinned_tenant, target)):
        return
    if force:
        _cancel_flush_timer()
        _do_save_positions(target, tenant_id=pinned_tenant)
        return

    def _delayed():
        _do_save_positions(target, tenant_id=pinned_tenant)

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
    key = get_key(symbol, timeframe)
    store = _active_store()
    changed = False
    candidate = max(float(current_price), float(peak_hint or 0))
    with _positions_lock:
        pos = _ensure_key(store, key)
        old_high = float(pos.get("recent_high") or 0)
        new_high = max(old_high, candidate)
        if new_high > old_high:
            pos["recent_high"] = new_high
            # Stagnant-rotation idle clock: time since last genuine progress,
            # not time since last fill (which partial-sells/DCA reset).
            pos["peak_at"] = datetime.now().isoformat()
            changed = True
    if changed:
        flush_positions()
    return changed


def lock_strategy_tier(symbol: str, timeframe: str, tier: str) -> None:
    if tier not in ("stable", "volatile"):
        return
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    store = _active_store()
    changed = False
    with _positions_lock:
        pos = _ensure_key(store, key)
        if not pos.get("strategy_tier"):
            pos["strategy_tier"] = tier
            changed = True
    if changed and _position_persistable(store[key]):
        flush_positions()



def _empty_position() -> dict:
    """Default empty lot — never raise on missing store keys."""
    return {
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
        "last_scheduled_dca_at": None,
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
        "side": "long",
        "leverage": None,
        "recent_low": None,
    }


def _ensure_key(store: dict, key: str) -> dict:
    """Return position dict for key; create empty if missing. Caller holds lock."""
    pos = store.get(key)
    if pos is None:
        pos = _empty_position()
        store[key] = pos
    return pos


def get_key(symbol, timeframe):
    return f"{symbol.replace('/', '_')}_{timeframe}"


def parse_position_key(key: str) -> tuple[str, str]:
    """Split store key ``BASE_QUOTE_tf`` → (``BASE/QUOTE``, timeframe)."""
    base, _, tf = str(key or "").rpartition("_")
    if not base or not tf:
        return "", ""
    symbol = base.replace("_", "/") if "/" not in base else base
    return symbol, tf


def _symbol_key_base(symbol: str) -> str:
    return str(symbol or "").replace("/", "_").upper()


def find_open_position_for_symbol(
    symbol: str,
    preferred_timeframe: str | None = None,
) -> tuple[str, dict] | None:
    """
    Find an open lot for *symbol* on any timeframe in the active tenant store.

    Returns ``(timeframe, position)`` or ``None``.

    Preference order:
    1. *preferred_timeframe* if that lot is open
    2. largest amount
    3. most recent ``entry_at`` / ``last_trade_at`` (stable tie-break)
    """
    _activate(_resolve_store_key())
    want = _symbol_key_base(symbol)
    if not want:
        return None

    matches: list[tuple[str, dict]] = []
    with _positions_lock:
        store = _active_store()
        for key, raw in store.items():
            sym, tf = parse_position_key(key)
            if _symbol_key_base(sym) != want:
                continue
            if not is_open_position(raw):
                continue
            # Copy under lock so callers can read safely without holding it.
            matches.append((tf, dict(raw)))

    if not matches:
        return None

    if preferred_timeframe:
        pref = str(preferred_timeframe).strip()
        for tf, pos in matches:
            if tf == pref:
                return tf, pos

    def _sort_key(item: tuple[str, dict]):
        tf, pos = item
        amount = float(pos.get("amount", 0) or 0)
        ts = str(pos.get("entry_at") or pos.get("last_trade_at") or "")
        return (amount, ts)

    matches.sort(key=_sort_key, reverse=True)
    return matches[0]


def bind_buy_timeframe(symbol: str, timeframe: str) -> str:
    """If a long lot is already open on another TF, add there (no second lot).

    Sells already hop TFs (#117). Gainer heat defaults to 1h while watchlist
    lots sit on 4h — a BUY then opened a duplicate instead of DCA.
    Short lots are left on *timeframe* so one-way risk can refuse the buy.
    """
    pref = str(timeframe or "4h").strip() or "4h"
    found = find_open_position_for_symbol(symbol, preferred_timeframe=pref)
    if not found:
        return pref
    lot_tf, pos = found
    try:
        from strategies.short_math import is_short

        if is_short(pos) and float(pos.get("amount") or 0) > 1e-12:
            return pref
    except Exception:
        pass
    if float(pos.get("amount") or 0) <= 1e-12:
        return pref
    hop = str(lot_tf or pref).strip() or pref
    if hop != pref:
        from logger import log

        log(f"buy TF hop {symbol} {pref}→{hop} (existing long)", "INFO")
    return hop


def init_position(symbol, timeframe):
    """Ensure position key exists on the active tenant store (never KeyError)."""
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    with _positions_lock:
        _ensure_key(_active_store(), key)


def get_position(symbol, timeframe):
    """Return position dict. Always ensures key exists (never KeyError)."""
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    with _positions_lock:
        return _ensure_key(_active_store(), key)


def set_position_field(symbol: str, timeframe: str, field: str, value) -> None:
    """Update one position field under the positions lock."""
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    with _positions_lock:
        _ensure_key(_active_store(), key)[field] = value


def set_position_lock(symbol: str, timeframe: str, lock: dict | None, *, persist: bool = True) -> dict:
    """Set or clear position lock; returns the updated position dict (copy of lock state)."""
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    with _positions_lock:
        pos = _ensure_key(_active_store(), key)
        if lock is None:
            pos["lock"] = {
                "enabled": False,
                "cleared_at": datetime.now(timezone.utc).isoformat(),
                "cleared_by": "unlock",
            }
        else:
            pos["lock"] = dict(lock)
        out = dict(pos.get("lock") or {}) if lock else {}
    if persist:
        flush_positions(force=True)
    return out


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
        pos = _ensure_key(store, key)
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
        pos = _ensure_key(store, key)
        if pos.get("time_profit_exit_done"):
            return
        pos["time_profit_exit_done"] = True
    flush_positions()


def mark_trailing_take_profit_step(symbol: str, timeframe: str, current_price: float) -> None:
    """Cooldown + reset recent_high after trail TP; sizing stays on exit ladder."""
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    with _positions_lock:
        pos = _ensure_key(_active_store(), key)
        pos["last_trail_tp_at"] = datetime.now().isoformat()
        pos["recent_high"] = float(current_price)
        pos["peak_at"] = datetime.now().isoformat()
    flush_positions()


def mark_profit_max_lifetime_done(symbol: str, timeframe: str) -> None:
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    store = _active_store()
    with _positions_lock:
        pos = _ensure_key(store, key)
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
    leverage: float | None = None,
):
    global _open_positions_count
    _activate(_resolve_store_key())
    key = get_key(symbol, timeframe)
    store = _active_store()
    was_open = False
    with _positions_lock:
        pos = _ensure_key(store, key)
        was_open = is_open_position(pos)
        if signal in ("BUY", "BUY_DCA") and amount_traded > 0:
            if str(pos.get("side") or "long").lower() == "short" and float(pos.get("amount") or 0) > 1e-12:
                from logger import log as _log

                _log(
                    f"one-way: refuse {signal} on short {symbol} {timeframe}",
                    "ERROR",
                )
                return
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
                if not int(pos.get("dca_max_rounds", 0) or 0):
                    from strategies.dca import dca_config as _dca_cfg

                    cfg = _dca_cfg(params)
                    pos["dca_max_rounds"] = int(cfg.get("max_rounds", 3))
                # Recovery mode: re-base trail peak so WS trail_stop does not use pre-dump high
                try:
                    from strategies.dca import (
                        reanchor_recent_high_after_dca,
                        should_reanchor_peak_on_dca,
                    )

                    if should_reanchor_peak_on_dca(params):
                        reanchor_recent_high_after_dca(pos, float(current_price))
                except Exception:
                    pass
                # Epoch peak for sniper/recovery_hold (clamp stale pre-DCA recent_high)
                try:
                    from strategies.recovery_hold import (
                        stamp_peak_epoch_on_dca,
                        recovery_hold_config,
                        set_recovery_hold,
                    )

                    rh_cfg = recovery_hold_config(params)
                    if rh_cfg.get("stamp_peak_epoch_on_dca", True):
                        stamp_peak_epoch_on_dca(pos, float(current_price))
                    if rh_cfg.get("set_on_dca"):
                        set_recovery_hold(pos, sniper_focus=False, heavy=False)
                except Exception:
                    pass
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
                pos["peak_at"] = datetime.now().isoformat()
                pos["exit_ladder_step"] = 0
                pos["last_trade_type"] = "BUY"
                pos["dca_rounds"] = 0
                pos["dca_max_rounds"] = 0
                pos["last_dca_at"] = None
                pos["last_scheduled_dca_at"] = None
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
                pos["side"] = "long"
        elif signal in ("SHORT", "SHORT_ADD") and amount_traded > 0:
            old_amount = pos["amount"]
            old_side = str(pos.get("side") or "long").lower()
            if old_side != "short" and float(old_amount or 0) > 1e-12:
                from logger import log as _log

                _log(
                    f"one-way: refuse {signal} on long {symbol} {timeframe}",
                    "ERROR",
                )
                return
            adding = old_side == "short" and float(old_amount or 0) > 0
            if adding:
                new_amount = old_amount + Decimal(str(amount_traded))
                old_average = float(pos.get("average_entry") or current_price)
                pos["average_entry"] = float(
                    (old_average * float(old_amount) + current_price * float(amount_traded))
                    / float(new_amount)
                )
                pos["amount"] = new_amount
            else:
                pos["amount"] = Decimal(str(amount_traded))
                pos["average_entry"] = current_price
                pos["peak_amount"] = float(amount_traded)
                pos["sold_percent"] = 0.0
                pos["first_buy_at"] = datetime.now().isoformat()
                pos["entry_at"] = pos["first_buy_at"]
                pos["dca_rounds"] = 0
                pos["dca_total_usdt"] = 0.0
            pos["side"] = "short"
            if leverage:
                pos["leverage"] = float(leverage)
            elif not pos.get("leverage"):
                pos["leverage"] = 2.0
            pos["last_action"] = "SHORT"
            pos["last_trade_type"] = "SHORT"
            pos["last_trade_at"] = datetime.now().isoformat()
            pos["recent_high"] = max(float(pos.get("recent_high") or 0), float(current_price))
            pos["recent_low"] = float(current_price)
            if entry_source:
                pos["entry_source"] = entry_source
        elif signal in ("COVER", "COVER_FULL") and amount_traded > 0:
            sell_amount = min(Decimal(str(amount_traded)), pos["amount"])
            pos["amount"] = pos["amount"] - sell_amount
            pos["last_action"] = "COVER"
            pos["last_trade_type"] = "COVER"
            pos["last_trade_at"] = datetime.now().isoformat()
            if float(pos["amount"] or 0) <= 1e-12:
                pos["amount"] = Decimal("0")
                pos["sold_percent"] = 1.0
                pos["side"] = "long"
                pos["leverage"] = None
            else:
                peak = float(pos.get("peak_amount") or 0) or float(sell_amount + pos["amount"])
                if peak > 0:
                    pos["sold_percent"] = 1.0 - float(pos["amount"]) / peak
        elif "SELL" in signal:
            if str(pos.get("side") or "long").lower() == "short" and float(pos.get("amount") or 0) > 1e-12:
                from logger import log as _log

                _log(
                    f"one-way: refuse {signal} on short {symbol} {timeframe}",
                    "ERROR",
                )
                return
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
    store = _active_store()
    with _positions_lock:
        return sum(
            1 for p in store.values()
            if is_open_position(p) and not _is_tail(p, cfg)
        )


def count_open_tail_slots(config_raw: dict | None = None) -> int:
    from strategies.sell_rotation_policy import is_tail_position as _is_tail, rotation_config

    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    cfg = rotation_config(config_raw)
    store = _active_store()
    with _positions_lock:
        return sum(
            1 for p in store.values()
            if is_open_position(p) and _is_tail(p, cfg)
        )


def get_total_aria():
    store = _active_store()
    with _positions_lock:
        total = Decimal("0")
        for pos in store.values():
            total += pos["amount"]
        return total


def _active_lot_from_store_key(key: str, p: dict) -> dict:
    base, _, tf = key.rpartition("_")
    symbol = base.replace("_", "/") if "/" not in base else base
    highlight = "🔥 " if p.get("last_action") == "BUY" else ""
    amount = p.get("amount", 0)
    if hasattr(amount, "__float__"):
        amount = float(amount)
    lot = {
        "symbol": symbol,
        "timeframe": tf,
        "amount": float(amount or 0),
        "average_entry": p.get("average_entry", 0),
        "entry_price": p.get("average_entry", 0),
        "last_buy_price": p.get("last_buy_price", 0),
        "realized_pnl": p.get("realized_pnl", 0),
        "peak_amount": float(p.get("peak_amount", 0) or 0),
        "sold_percent": float(p.get("sold_percent", 0)),
        "last_action": p.get("last_action"),
        "entry_source": p.get("entry_source"),
        "first_buy_at": p.get("first_buy_at"),
        "highlight": highlight,
        # overlays needed by sniper / lock / board (not order-derived)
        "strategy_tier": p.get("strategy_tier"),
        "dca_rounds": int(p.get("dca_rounds") or 0),
        "dca_heavy_used": bool(p.get("dca_heavy_used")),
        "recovery_hold": bool(p.get("recovery_hold")),
        "sniper_focus": bool(p.get("sniper_focus")),
        "peak_epoch_high": p.get("peak_epoch_high"),
        "last_dca_at": p.get("last_dca_at"),
        "recent_high": p.get("recent_high"),
        "recent_low": p.get("recent_low"),
        "side": str(p.get("side") or "long").lower() or "long",
        "leverage": p.get("leverage"),
        "lock": p.get("lock"),
        "current_price": p.get("current_price") or p.get("mark") or p.get("last_price"),
    }
    # Surface position lock for /positions, /sell, Telegram 🔒 badge
    lock = p.get("lock")
    if isinstance(lock, dict) and lock:
        lot["lock"] = dict(lock)
    return lot


def list_active_positions_from_ledger(
    scope: str | None = None,
    tenant_id: str | None = None,
) -> list[dict]:
    """Open lots from orders+cache for *tenant* (safe for async portfolio reads)."""
    from services.ledger_sync import _build_positions_snapshot_from_orders

    key = _resolve_store_key(scope, tenant_id)
    target = key[1]
    tid = key[0]
    order_snap = _build_positions_snapshot_from_orders(target, tenant_id=tid)
    cache_doc = load_positions_document(target, tenant_id=tid)
    merged = derive_positions_from_orders_and_cache(
        order_snap, cache_doc, tenant_id=tid
    )
    active: list[dict] = []
    for pos_key, raw in merged.items():
        if not is_open_position(raw):
            continue
        base, _, _tf = pos_key.rpartition("_")
        symbol = base.replace("_", "/") if "/" not in base else base
        if symbol.upper().startswith("TEST"):
            continue
        active.append(_active_lot_from_store_key(pos_key, raw))
    return active


def list_active_positions(
    tenant_id: str | None = None,
    scope: str | None = None,
):
    key = _resolve_store_key(scope, tenant_id)
    store = _store_for_key(key)
    with _positions_lock:
        active = []
        for pos_key, p in store.items():
            if not is_open_position(p):
                continue
            lot = _active_lot_from_store_key(pos_key, p)
            if lot["symbol"].upper().startswith("TEST"):
                continue
            active.append(lot)
        return active