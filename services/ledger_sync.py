"""Isolate position state per ledger scope (demo / paper / live)."""

from __future__ import annotations

import os
import shutil
import warnings

from core.models import OrderStatus
from logger import log

_RECONCILE_RECENT_HIGHS_WARNED = False
_RECONCILE_PEAK_AMOUNTS_WARNED = False

LEGACY_POSITIONS_FILE = "positions.json"


def _scope_for_trading_mode(trading_mode: str) -> str:
    from data_manager import is_demo_mode, resolve_ledger_scope

    if is_demo_mode():
        return "demo"
    return resolve_ledger_scope(trading_mode)


def migrate_legacy_positions() -> None:
    """Copy legacy positions.json into positions.paper.json once (production only)."""
    from data_manager import is_demo_mode, resolve_positions_file

    if is_demo_mode():
        return

    paper_path = resolve_positions_file("paper")
    if os.path.exists(paper_path):
        return
    if not os.path.exists(LEGACY_POSITIONS_FILE):
        return
    try:
        shutil.copy2(LEGACY_POSITIONS_FILE, paper_path)
        log(f"Migrated {LEGACY_POSITIONS_FILE} → {paper_path}", "INFO")
    except Exception as e:
        log(f"Legacy positions migration failed: {e}", "WARNING")


def _is_dca_order(order: dict) -> bool:
    signal = (order.get("signal") or "").upper()
    source = (order.get("source") or "").lower()
    return signal == "BUY_DCA" or source in ("dca", "dca_recovery", "dca_scheduled")


def _empty_order_position() -> dict:
    return {
        "amount": 0.0,
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
        "dca_rounds": 0,
        "dca_max_rounds": 0,
        "last_dca_at": None,
        "dca_total_usdt": 0.0,
        "dca_recovery_rounds": 0,
        "dca_recovery_max_rounds": 0,
        "last_dca_recovery_at": None,
        "entry_source": None,
        "entry_at": None,
        "exit_ladder_step": 0,
    }


def _reset_position_cycle(pos: dict, *, amount: float, price: float, trade_ts: str | None) -> None:
    pos["amount"] = amount
    pos["peak_amount"] = amount
    pos["sold_percent"] = 0.0
    pos["average_entry"] = price
    pos["last_buy_price"] = price
    pos["last_action"] = "BUY"
    pos["last_trade_type"] = "BUY"
    pos["last_trade_at"] = trade_ts
    pos["rsi_sell_tiers_done"] = {}
    pos["exit_ladder_step"] = 0
    pos["dca_rounds"] = 0
    pos["dca_max_rounds"] = 0
    pos["last_dca_at"] = None
    pos["dca_total_usdt"] = 0.0
    pos["dca_recovery_rounds"] = 0
    pos["dca_recovery_max_rounds"] = 0
    pos["last_dca_recovery_at"] = None
    pos["entry_source"] = None
    pos["entry_at"] = trade_ts
    pos["first_buy_at"] = trade_ts


def _build_positions_snapshot_from_orders(
    scope: str,
    tenant_id: str | None = None,
) -> dict:
    """Derive position state from cash-aware replay (same filter as Sim USDT)."""
    from core.portfolio_baseline import initial_capital
    from core.sim_ledger_replay import replay_simulated_ledger
    from data_manager import get_config, load_orders

    cfg = get_config(tenant_id)
    orders = [
        o
        for o in load_orders(scope, tenant_id=tenant_id).get("orders", [])
        if o.get("status") == OrderStatus.EXECUTED.value
    ]
    initial = initial_capital(scope=scope, config=cfg)
    snapshot = dict(
        replay_simulated_ledger(orders, initial, config=cfg, tenant_id=tenant_id)["positions"]
    )
    _reconcile_ladder_steps_in_snapshot(snapshot)
    return snapshot


def _is_partial_sell_signal(signal: str) -> bool:
    sig = (signal or "").upper()
    return "PARTIAL" in sig or sig in ("SELL_30", "SELL_20", "SELL_10")


def _reconcile_ladder_steps_in_snapshot(snapshot: dict) -> None:
    from strategies.exit_ladder import default_ladder_tiers, reconcile_exit_ladder_step

    tiers = default_ladder_tiers()
    for pos in snapshot.values():
        partial_count = pos.pop("_partial_sell_count", None)
        reconcile_exit_ladder_step(
            pos,
            tiers,
            partial_sell_count=partial_count if partial_count else None,
        )


def count_open_positions_from_orders(
    scope: str,
    tenant_id: str | None = None,
) -> int:
    from strategies.positions import is_open_position

    snapshot = _build_positions_snapshot_from_orders(scope, tenant_id=tenant_id)
    return sum(1 for p in snapshot.values() if is_open_position(p))


def _writer_lease_blocks_position_rebuild() -> bool:
    """True when the writer lease is enabled and this process does not hold it."""
    from bus.writer_lease import lease_enabled, writer_lease_held

    return bool(lease_enabled() and not writer_lease_held())


def rebuild_positions_from_orders(
    scope: str,
    tenant_id: str | None = None,
) -> int:
    """Rebuild in-memory positions for *scope* from orders + position cache merge."""
    from data_manager import load_orders, load_positions_document
    from strategies.positions import (
        apply_positions_snapshot,
        derive_positions_from_orders_and_cache,
        flush_positions,
        is_open_position,
    )

    from core.tenant_context import resolve_tenant_id, tenant_context

    from storage.errors import LedgerUnavailable

    if _writer_lease_blocks_position_rebuild():
        log(
            f"rebuild_positions_from_orders deferred: writer lease not held "
            f"(scope={scope})",
            "INFO",
        )
        return 0

    tid = resolve_tenant_id(tenant_id)
    try:
        orders_doc = load_orders(scope, tenant_id=tid)
        order_snap = _build_positions_snapshot_from_orders(scope, tenant_id=tid)
        cache_doc = load_positions_document(scope, tenant_id=tid)
    except LedgerUnavailable as e:
        log(
            f"prune_orphan_position_cache skipped (orders/positions load failed) "
            f"tenant={tid} scope={scope}: {e}",
            "WARNING",
        )
        raise
    from strategies.positions import prune_orphan_position_cache

    if not (orders_doc.get("orders") or []):
        log(
            f"prune_orphan_position_cache skipped (orders empty) "
            f"tenant={tid} scope={scope}",
            "WARNING",
        )
        orphans = []
    else:
        cache_doc, orphans = prune_orphan_position_cache(order_snap, cache_doc)
        if orphans:
            from data_manager import save_positions_document

            save_positions_document(cache_doc, scope, tenant_id=tid)
            log(
                f"Pruned {len(orphans)} orphan position cache key(s) for tenant={tid} scope={scope}",
                "INFO",
            )
    snapshot = derive_positions_from_orders_and_cache(
        order_snap, cache_doc, tenant_id=tid
    )
    from data_manager import load_orders

    orders = [
        o
        for o in load_orders(scope, tenant_id=tid).get("orders", [])
        if o.get("status") == OrderStatus.EXECUTED.value
    ]

    with tenant_context(tid, scope=scope):
        apply_positions_snapshot(snapshot, scope=scope)
        flush_positions(scope, force=True)
    open_count = sum(1 for p in snapshot.values() if is_open_position(p))
    log(
        f"Rebuilt positions for scope={scope} from {len(orders)} filled order(s), "
        f"{open_count} open",
        "INFO",
    )
    return open_count


def activate_ledger_scope(scope: str, *, rebuild: bool = False) -> int:
    """Switch active in-memory positions to *scope*."""
    from strategies.positions import bootstrap_positions, count_open_positions

    migrate_legacy_positions()
    if rebuild:
        return rebuild_positions_from_orders(scope)
    bootstrap_positions(scope=scope)
    return count_open_positions()


def on_trading_mode_change(old_mode: str, new_mode: str) -> str:
    """Persist outgoing ledger and load the target ledger without cross-contamination."""
    from strategies.positions import count_open_positions, flush_positions, get_active_scope

    old_scope = _scope_for_trading_mode(old_mode)
    new_scope = _scope_for_trading_mode(new_mode)
    if old_scope == new_scope:
        return ""

    flush_positions(scope=old_scope, force=True)
    open_count = activate_ledger_scope(new_scope, rebuild=True)
    active = get_active_scope()
    if active != new_scope:
        log(f"Ledger scope mismatch after switch: {active} != {new_scope}", "WARNING")
    return (
        f"Ledger: <b>{old_scope.upper()}</b> → <b>{new_scope.upper()}</b>\n"
        f"Positionen aus Orders neu aufgebaut: <b>{open_count}</b> offen"
    )


def _lots_with_amount(*, include_dust: bool = True) -> list[dict]:
    """In-memory lots with a non-dust amount. Dust included when asked (F6b)."""
    from strategies.positions import (
        _active_store,
        _positions_lock,
        has_position_amount,
        is_open_position,
        parse_position_key,
    )

    out = []
    with _positions_lock:
        for key, pos in _active_store().items():
            if not has_position_amount(pos):
                continue
            if not include_dust and not is_open_position(pos):
                continue
            symbol, timeframe = parse_position_key(key)
            if not symbol or symbol.upper().startswith("TEST"):
                continue
            out.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "key": key,
                    "position": pos,
                }
            )
    return out


def _latest_buy_fill_iso(symbol: str, timeframe: str, scope: str, tenant_id: str | None) -> str | None:
    """Latest buy fill of this lot from orders, opening buy included (F6d b)."""
    from core.time_utils import ledger_datetime_utc
    from data_manager import load_orders

    best_dt = None
    best_raw = None
    try:
        orders = load_orders(scope, tenant_id=tenant_id).get("orders") or []
    except Exception:
        return None
    sym = str(symbol or "")
    for order in orders:
        if (order.get("status") or "") != "filled":
            continue
        if (order.get("side") or "").lower() != "buy":
            continue
        if str(order.get("symbol") or "") != sym:
            continue
        if str(order.get("timeframe") or "4h") != str(timeframe or "4h"):
            continue
        ts = (order.get("timestamps") or {}).get("filled") or (order.get("timestamps") or {}).get("created")
        dt = ledger_datetime_utc(ts)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_raw = ts
    return best_raw


def _f6_search_start(pos: dict, *, symbol: str, timeframe: str, scope: str, tenant_id: str | None):
    """Latest of last DCA fill, latest buy fill, and first_buy_at.

    Returns (iso, source) or (None, reason) when the latest DCA fill is missing.
    """
    from core.time_utils import ledger_datetime_utc, process_local_tz
    from strategies.dca import _latest_dca_fill_at

    latest = _latest_dca_fill_at(pos)
    if latest is None:
        return None, "missing_dca_time"
    marks = [("dca", ledger_datetime_utc(latest))]
    buy_raw = _latest_buy_fill_iso(symbol, timeframe, scope, tenant_id)
    buy_dt = ledger_datetime_utc(buy_raw) if buy_raw else None
    if buy_dt is not None:
        marks.append(("buy", buy_dt))
    first = ledger_datetime_utc(pos.get("first_buy_at"))
    if first is not None:
        marks.append(("first_buy", first))
    marks = [(name, dt) for name, dt in marks if dt is not None]
    if not marks:
        return None, "missing_dca_time"
    source, when = marks[0]
    for name, dt in marks[1:]:
        if dt >= when:
            source, when = name, dt
    # Naive process-local wall clock, same basis as candle fromtimestamp (C1).
    local = when.astimezone(process_local_tz()).replace(tzinfo=None)
    return local.isoformat(sep=" "), source


def _f2_peak_hint(market_svc, symbol: str, timeframe: str, pos: dict):
    """OHLCV hint for F2. DCA lots never search from first_buy_at without an epoch."""
    from strategies.dca import lot_has_dca_rounds

    if lot_has_dca_rounds(pos):
        epoch = pos.get("peak_epoch_at")
        if not epoch:
            return None, None
        meta = market_svc.infer_ohlcv_peak_price(
            symbol, timeframe, epoch, with_meta=True, page_to_since=True
        )
        if not isinstance(meta, dict) or not meta.get("covered") or not meta.get("price"):
            return None, None
        return float(meta["price"]), meta.get("candle_open")
    since = pos.get("first_buy_at") or pos.get("entry_at")
    meta = market_svc.infer_ohlcv_peak_price(
        symbol, timeframe, since, with_meta=True, page_to_since=bool(since)
    )
    if not isinstance(meta, dict) or not meta.get("covered") or not meta.get("price"):
        return None, None
    return float(meta["price"]), meta.get("candle_open")


def reanchor_legacy_dca_peaks(scope: str, price_map: dict | None = None) -> int:
    """F6: one-time re-anchor of legacy DCA lots missing peak_epoch_at.

    Marks changed lots so the existing flush runs in the active tenant context.
    Returns the number of lots re-anchored (V1 or V3). V2 lots are not stamped.
    """
    from datetime import datetime

    from core.tenant_context import resolve_tenant_id
    from price_fetcher import get_prices_batch
    from strategies.dca import lot_has_dca_rounds
    from strategies.positions import (
        _active_store,
        _positions_lock,
        flush_positions,
        get_key,
        has_position_amount,
        is_open_position,
    )

    tid = resolve_tenant_id()
    lots = _lots_with_amount(include_dust=True)
    prices = dict(price_map or {})
    missing = sorted({lot["symbol"] for lot in lots if float(prices.get(lot["symbol"], 0) or 0) <= 0})
    if missing:
        try:
            prices.update(get_prices_batch(missing) or {})
        except Exception as exc:
            log(f"F6 price fetch failed tenant={tid}: {exc}", "WARNING")

    from services.market_service import MarketService

    try:
        market_svc = MarketService()
    except Exception:
        market_svc = None

    changed_keys = []
    for lot in lots:
        sym = lot["symbol"]
        tf = lot["timeframe"]
        key = lot["key"]
        with _positions_lock:
            pos = _active_store().get(key)
            if not pos or not has_position_amount(pos):
                continue
            if not lot_has_dca_rounds(pos) or pos.get("peak_epoch_at"):
                continue
            snapshot = dict(pos)
        outcome = _reanchor_one_legacy_lot(
            snapshot,
            symbol=sym,
            timeframe=tf,
            scope=scope,
            tenant_id=tid,
            price=float(prices.get(sym, 0) or 0),
            market_svc=market_svc,
        )
        if outcome is None:
            continue
        with _positions_lock:
            live = _active_store().get(key)
            if not live:
                continue
            live.update(outcome["fields"])
            changed_keys.append(key)
        log(outcome["line"], "INFO")
    if changed_keys:
        flush_positions(scope=scope, force=True)
    return len(changed_keys)


def _reanchor_one_legacy_lot(
    pos: dict,
    *,
    symbol: str,
    timeframe: str,
    scope: str,
    tenant_id: str,
    price: float,
    market_svc,
) -> dict | None:
    """Return {fields, line} for one legacy DCA lot, or None when skipped.

    V2 logs a WARNING and returns None (no stamp).
    """
    from datetime import datetime

    from strategies.positions import has_position_amount, is_open_position

    if price <= 0:
        log(
            f"F6 V2 tenant={tenant_id} symbol={symbol} tf={timeframe} "
            f"reason=no_boot_price",
            "WARNING",
        )
        return None
    start, source = _f6_search_start(
        pos, symbol=symbol, timeframe=timeframe, scope=scope, tenant_id=tenant_id
    )
    if start is None:
        log(
            f"F6 V2 tenant={tenant_id} symbol={symbol} tf={timeframe} reason={source}",
            "WARNING",
        )
        return None
    candle_high = None
    candle_open = None
    if market_svc is not None:
        try:
            meta = market_svc.infer_ohlcv_peak_price(
                symbol, timeframe, start, with_meta=True, page_to_since=True
            )
        except Exception as exc:
            meta = {"covered": False, "reason": f"candle_error:{exc}"}
    else:
        meta = {"covered": False, "reason": "no_market"}
    if not isinstance(meta, dict) or not meta.get("covered"):
        reason = (meta or {}).get("reason") if isinstance(meta, dict) else "not_covered"
        log(
            f"F6 V2 tenant={tenant_id} symbol={symbol} tf={timeframe} reason={reason}",
            "WARNING",
        )
        return None
    if meta.get("price"):
        candle_high = float(meta["price"])
        candle_open = meta.get("candle_open")
    last_buy = float(pos.get("last_buy_price") or 0)
    average = float(pos.get("average_entry") or 0)
    parts = [v for v in (last_buy, average, candle_high) if v and v > 0]
    if not parts:
        log(
            f"F6 V2 tenant={tenant_id} symbol={symbol} tf={timeframe} reason=no_peak_inputs",
            "WARNING",
        )
        return None
    new_peak = max(parts)
    if candle_high is not None and candle_high >= last_buy and candle_high >= average and candle_open is not None:
        peak_at = candle_open.isoformat(sep=" ") if hasattr(candle_open, "isoformat") else str(candle_open)
    else:
        peak_at = start
    fields = {
        "recent_high": float(new_peak),
        "peak_at": peak_at,
        "peak_epoch_high": float(new_peak),
        "peak_epoch_at": start,
        "v3": False,
    }
    v3 = _v3_would_sell(
        pos,
        fields,
        price=price,
        symbol=symbol,
        timeframe=timeframe,
        tenant_id=tenant_id,
    )
    if v3:
        floor = max(v for v in (last_buy, average, price) if v and v > 0)
        boot = datetime.now().isoformat(sep=" ")
        fields = {
            "recent_high": float(floor),
            "peak_at": boot,
            "peak_epoch_high": float(floor),
            "peak_epoch_at": boot,
            "v3": True,
        }
        new_peak = floor
        peak_at = boot
    dust = has_position_amount(pos) and not is_open_position(pos)
    old_peak = float(pos.get("recent_high") or 0)
    stop_px = _resulting_stop(pos, float(fields["recent_high"]), symbol, timeframe)
    line = (
        f"F6 V4 tenant={tenant_id} symbol={symbol} tf={timeframe} "
        f"old_peak={old_peak} new_peak={new_peak} search_start={start} source={source} "
        f"candle_time={candle_open} stop={stop_px} price={price} "
        f"v3={bool(fields.get('v3'))} dust={'yes' if dust else 'no'}"
    )
    return {"fields": fields, "line": line}


def _resulting_stop(pos: dict, peak: float, symbol: str, timeframe: str) -> float:
    from strategies.trailing_stop import compute_stop_price, compute_trail_pct, trailing_config

    params = {}
    try:
        from strategies.registry import resolve_strategy_params

        params = resolve_strategy_params(
            {"symbol": symbol, "timeframe": timeframe},
            has_position=True,
            frozen_tier=pos.get("strategy_tier"),
        ) or {}
    except Exception:
        params = {}
    cfg = trailing_config(params)
    entry = float(pos.get("average_entry") or 0)
    trail = compute_trail_pct(0.0, cfg)
    if entry <= 0 or peak <= 0:
        return 0.0
    return compute_stop_price(
        entry,
        peak,
        trail,
        floor_at_entry=bool(cfg.get("floor_at_entry", True)),
        be_buffer_pct=float(cfg.get("be_buffer_pct") or 0.0),
    )


def _positive_atr(raw) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 0:
        return value
    return None


def _conservative_atr_pct(params: dict | None) -> float:
    """Smallest configured ATR (default 3.0).

    ``compute_trail_pct`` grows with ATR until ``max_trail_pct``, so the
    smallest configured value is the tightest trail this check can use.
    """
    found: list[float] = []
    params = params or {}
    for raw in (params.get("atr_pct"), params.get("atr_reference_pct")):
        value = _positive_atr(raw)
        if value is not None:
            found.append(value)
    raw_cfg: dict = {}
    try:
        from core.config import get_bot_config

        loaded = get_bot_config().raw
        if isinstance(loaded, dict):
            raw_cfg = loaded
    except Exception:
        raw_cfg = {}
    risk = raw_cfg.get("risk") if isinstance(raw_cfg.get("risk"), dict) else {}
    exit_rt = raw_cfg.get("exit_realtime") if isinstance(raw_cfg.get("exit_realtime"), dict) else {}
    for raw in (
        risk.get("atr_pct"),
        risk.get("atr_reference_pct"),
        exit_rt.get("atr_pct"),
        exit_rt.get("default_atr_pct"),
    ):
        value = _positive_atr(raw)
        if value is not None:
            found.append(value)
    if not found:
        return 3.0
    return min(found)


def _v3_fail_closed(tenant_id: str, symbol: str, timeframe: str, reason: str) -> None:
    log(
        f"F6 V3 fail-closed tenant={tenant_id} symbol={symbol} tf={timeframe} reason={reason}",
        "WARNING",
    )


def _v3_would_sell(
    pos: dict,
    fields: dict,
    *,
    price: float,
    symbol: str,
    timeframe: str,
    tenant_id: str = "",
) -> bool:
    """V3 when trail stop or TTP would return a candidate on the re-anchored lot.

    An exception, or params that cannot be resolved, is also V3. The boot
    check must not treat that as "safe".
    """
    from core.models import MarketContext
    from strategies.trailing_stop import evaluate_trailing_stop
    from strategies.trailing_take_profit import evaluate_trailing_take_profit

    trial = dict(pos)
    trial.update(fields)
    entry = float(trial.get("average_entry") or 0)
    if entry <= 0 or price <= 0:
        return False
    try:
        from strategies.registry import resolve_strategy_params

        params = resolve_strategy_params(
            {"symbol": symbol, "timeframe": timeframe},
            has_position=True,
            frozen_tier=pos.get("strategy_tier"),
        )
    except Exception as exc:
        _v3_fail_closed(tenant_id, symbol, timeframe, f"params_unresolved:{exc}")
        return True
    if not params:
        _v3_fail_closed(tenant_id, symbol, timeframe, "params_unresolved")
        return True
    market = MarketContext(
        symbol=symbol,
        timeframe=timeframe,
        current_price=price,
        has_position=True,
        average_entry=entry,
        atr_pct=_conservative_atr_pct(params),
        strategy_params=params,
    )
    try:
        if evaluate_trailing_stop(market, trial, params) is not None:
            return True
        if evaluate_trailing_take_profit(market, trial, params) is not None:
            return True
    except Exception as exc:
        _v3_fail_closed(tenant_id, symbol, timeframe, f"eval_error:{exc}")
        return True
    return False


def sync_ledger_files_recent_highs(
    scope: str,
    price_map: dict | None = None,
    *,
    use_ohlcv: bool = False,
) -> bool:
    """Backfill recent_high from live marks (and optional OHLCV) for open lots.

    Ledger-internal — not exchange recovery (#314).
    """
    from strategies.positions import (
        flush_positions,
        get_position,
        has_position_amount,
        list_active_positions,
        update_market_snapshot,
    )

    open_lots = list_active_positions()
    dust_lots = _lots_with_amount(include_dust=True)
    if not open_lots and not dust_lots:
        return False

    prices = dict(price_map or {})
    missing = sorted(
        {
            p["symbol"]
            for p in list(open_lots) + list(dust_lots)
            if float(prices.get(p["symbol"], 0) or 0) <= 0
        }
    )
    if missing:
        from price_fetcher import get_prices_batch

        prices.update(get_prices_batch(missing))

    market_svc = None
    if use_ohlcv:
        from services.market_service import MarketService

        market_svc = MarketService()

    changed = False
    for lot in open_lots:
        sym = lot.get("symbol", "")
        tf = lot.get("timeframe", "4h")
        price = float(prices.get(sym, 0) or 0)
        if price <= 0 or not has_position_amount(lot):
            continue
        peak_hint = None
        high_at = None
        if market_svc is not None:
            pos = get_position(sym, tf)
            peak_hint, high_at = _f2_peak_hint(market_svc, sym, tf, pos)
        hint_kwargs = {}
        if high_at is not None and peak_hint is not None and float(peak_hint) > price:
            hint_kwargs["high_at"] = high_at
        if update_market_snapshot(sym, tf, price, peak_hint=peak_hint, **hint_kwargs):
            changed = True

    if changed:
        flush_positions(scope=scope, force=True)
        log(f"Reconciled recent_high for scope={scope}", "INFO")
    return changed


def reconcile_recent_highs(
    scope: str,
    price_map: dict | None = None,
    *,
    use_ohlcv: bool = False,
) -> bool:
    """Deprecated alias of ``sync_ledger_files_recent_highs`` (#314)."""
    global _RECONCILE_RECENT_HIGHS_WARNED
    if not _RECONCILE_RECENT_HIGHS_WARNED:
        _RECONCILE_RECENT_HIGHS_WARNED = True
        msg = (
            "reconcile_recent_highs is deprecated; use sync_ledger_files_recent_highs "
            "(ledger-internal, not exchange recovery)"
        )
        log(msg, "WARNING")
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
    return sync_ledger_files_recent_highs(scope, price_map, use_ohlcv=use_ohlcv)


def sync_ledger_files_peak_amounts(scope: str) -> bool:
    """Backfill peak_amount and sold_percent from filled orders for open lots.

    Ledger-internal — not exchange recovery (#314).
    """
    from strategies.positions import _active_store, _positions_lock, flush_positions, has_position_amount

    from core.tenant_context import resolve_tenant_id

    tid = resolve_tenant_id()
    order_snap = _build_positions_snapshot_from_orders(scope, tenant_id=tid)
    changed = False
    store = _active_store()
    with _positions_lock:
        for key, pos in store.items():
            if not has_position_amount(pos):
                continue
            osnap = order_snap.get(key)
            if osnap:
                peak = float(osnap.get("peak_amount") or 0)
                sold = float(osnap.get("sold_percent") or 0)
            else:
                peak = float(pos.get("peak_amount") or 0)
                if peak <= 0:
                    peak = float(pos["amount"])
                sold = float(pos.get("sold_percent") or 0)
            if peak <= 0:
                continue
            if (
                float(pos.get("peak_amount") or 0) != peak
                or abs(float(pos.get("sold_percent") or 0) - sold) > 0.001
            ):
                pos["peak_amount"] = peak
                pos["sold_percent"] = sold
                changed = True
    if changed:
        flush_positions(scope=scope, force=True)
        log(f"Reconciled peak_amount for scope={scope}", "INFO")
    return changed


def reconcile_peak_amounts(scope: str) -> bool:
    """Deprecated alias of ``sync_ledger_files_peak_amounts`` (#314)."""
    global _RECONCILE_PEAK_AMOUNTS_WARNED
    if not _RECONCILE_PEAK_AMOUNTS_WARNED:
        _RECONCILE_PEAK_AMOUNTS_WARNED = True
        msg = (
            "reconcile_peak_amounts is deprecated; use sync_ledger_files_peak_amounts "
            "(ledger-internal, not exchange recovery)"
        )
        log(msg, "WARNING")
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
    return sync_ledger_files_peak_amounts(scope)


def backfill_orders_from_trade_history(scope: str) -> int:
    """One-off: create filled orders for trades missing from the order ledger."""
    from data_manager import load_orders, load_trade_history_document, save_orders

    data = load_orders(scope)
    orders = list(data.get("orders", []))
    known_ids = {o.get("id") for o in orders if o.get("id")}
    trades = load_trade_history_document(scope).get("trades", [])
    seq = max([int(o.get("display_seq", 0)) for o in orders], default=0)
    added = 0

    for trade in trades:
        order_id = trade.get("order_id") or ""
        if not order_id or order_id in known_ids:
            continue
        ts = trade.get("timestamp", "")
        side = (trade.get("type") or "buy").lower()
        seq += 1
        orders.append(
            {
                "id": order_id,
                "display_seq": seq,
                "status": "filled",
                "side": side,
                "symbol": trade.get("symbol", ""),
                "timeframe": trade.get("timeframe", "4h"),
                "order_type": "market",
                "source": trade.get("source", "auto"),
                "signal": trade.get("signal", ""),
                "trading_mode": trade.get("mode", scope if scope != "demo" else "paper"),
                "ledger_scope": scope,
                "legacy_trade_ts": ts,
                "request": {
                    "price": float(trade.get("price", 0)),
                    "amount": float(trade.get("amount", 0)),
                    "usdt": float(trade.get("usdt_amount", 0) or 0) or None,
                },
                "risk": {
                    "approved": True,
                    "message": "Backfilled from trade history",
                    "code": "",
                    "size_multiplier": 1.0,
                },
                "execution": {
                    "price": float(trade.get("price", 0)),
                    "amount": float(trade.get("amount", 0)),
                    "usdt": float(
                        trade.get("usdt_amount") or trade.get("usdt_received") or 0
                    ),
                    "exchange_order_id": trade.get("exchange_order_id"),
                },
                "pnl": trade.get("pnl"),
                "error": None,
                "timestamps": {"created": ts or "", "updated": ts or "", "filled": ts or ""},
            }
        )
        known_ids.add(order_id)
        added += 1

    if added:
        data["orders"] = orders
        data["ledger_scope"] = scope
        save_orders(data, scope)
        log(f"Backfilled {added} order(s) from trade history for scope={scope}", "INFO")
    return added


def _preserve_legacy_cache_lots(scope: str) -> None:
    """Paper/live only: keep material cache lots not yet represented in orders SOT."""
    if scope == "demo":
        return
    from data_manager import load_positions_document
    from strategies.positions import (
        DUST_AMOUNT_EPSILON,
        _active_store,
        _deserialize_position,
        _positions_lock,
        _recompute_open_count,
    )

    from core.tenant_context import resolve_tenant_id

    tid = resolve_tenant_id()
    cache_positions = load_positions_document(scope, tenant_id=tid).get("positions", {}) or {}
    order_snap = _build_positions_snapshot_from_orders(scope, tenant_id=tid)
    store = _active_store()
    with _positions_lock:
        for key, cached in cache_positions.items():
            if key in order_snap or key in store:
                continue
            if float(cached.get("amount", 0) or 0) <= DUST_AMOUNT_EPSILON:
                continue
            store[key] = _deserialize_position(dict(cached))
        _recompute_open_count()


def sync_positions_on_startup(*, include_legacy_reanchor: bool = False) -> None:
    """Reconcile peak_amount + recent_high cache after bootstrap (no wipe/rebuild).

    F6 (legacy DCA re-anchor) runs only when the caller asks. The exit-radar
    sidecar never asks, including when the writer lease is disabled (C8).
    Order inside this call: F6 first, then the F2 hint.
    """
    from data_manager import get_config, resolve_ledger_scope

    scope = resolve_ledger_scope(get_config().get("trading_mode", "paper"))
    migrate_legacy_positions()
    _preserve_legacy_cache_lots(scope)
    sync_ledger_files_peak_amounts(scope)
    if include_legacy_reanchor:
        try:
            reanchor_legacy_dca_peaks(scope)
        except Exception as exc:
            log(f"legacy DCA re-anchor skipped for scope={scope}: {exc}", "WARNING")
    try:
        sync_ledger_files_recent_highs(scope, use_ohlcv=True)
    except Exception as exc:
        log(f"recent_high reconcile skipped for scope={scope}: {exc}", "WARNING")


def run_per_tenant_startup(scope: str, *, include_legacy_reanchor: bool = False) -> None:
    """F6a: rebuild + startup sync once per price-cycle tenant, default included.

    Each tenant runs inside ``tenant_cycle_context``, which restores the
    previous tenant on exit (R1). Not used by the exit-radar sidecar.
    """
    from core.tenant_routing import iter_price_cycle_tenants, tenant_cycle_context

    for tenant in iter_price_cycle_tenants():
        with tenant_cycle_context(tenant):
            rebuild_positions_from_orders(scope, tenant_id=tenant)
            sync_positions_on_startup(include_legacy_reanchor=include_legacy_reanchor)