"""Observe vs trade universe (option C).

Observe = broad pool for memory, WQE scoring, sensors, logs.
Trade   = open positions + base + top-N discovery for BUY scan.

Fail-open: if split disabled or errors, observe == trade == full merged list.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from logger import log

_DEFAULTS: dict[str, Any] = {
    "split_enabled": False,
    "observe_max_coins": 100,
    "trade_max_coins": 40,
    "trade_include_open_positions": True,
    "trade_include_base": True,
    "trade_rank_by": "quality_score",  # quality_score | trending_rank | as_is
}


def universe_split_config(config: dict | None = None) -> dict[str, Any]:
    raw = {}
    if isinstance(config, dict):
        sec = config.get("universe")
        if isinstance(sec, dict):
            raw = sec
    out = {**_DEFAULTS, **raw}
    out["split_enabled"] = bool(out.get("split_enabled", False))
    try:
        out["observe_max_coins"] = int(out.get("observe_max_coins") or 100)
    except (TypeError, ValueError):
        out["observe_max_coins"] = 100
    try:
        out["trade_max_coins"] = int(out.get("trade_max_coins") or 40)
    except (TypeError, ValueError):
        out["trade_max_coins"] = 40
    out["trade_include_open_positions"] = bool(
        out.get("trade_include_open_positions", True)
    )
    out["trade_include_base"] = bool(out.get("trade_include_base", True))
    rank = str(out.get("trade_rank_by") or "quality_score").strip().lower()
    if rank not in ("quality_score", "trending_rank", "as_is"):
        rank = "quality_score"
    out["trade_rank_by"] = rank
    return out


def universe_split_enabled(config: dict | None = None) -> bool:
    return bool(universe_split_config(config).get("split_enabled"))


def _sym(coin: dict | None) -> str:
    if not isinstance(coin, dict):
        return ""
    return str(coin.get("symbol") or "").strip()


def _quality_value(coin: dict, *, use_ai_score: bool) -> Any:
    """Raw quality for ranking: AI-fused shadow score when enabled, else deterministic.

    #465: ``use_ai_score`` follows ``watchlist_quality.ai.sort_by`` / ``ai.enabled``
    (``use_ai_sort_score(config)``) so the trade universe rolls back with the config.
    """
    if use_ai_score:
        q = coin.get("quality_shadow_ai")
        if q is not None:
            return q
    return coin.get("quality_score")


def rank_key_for_coin(coin: dict, rank_by: str, *, use_ai_score: bool = True) -> float:
    """Sort key ascending = better (put first)."""
    if rank_by == "as_is":
        return 0.0
    if rank_by == "trending_rank":
        try:
            r = coin.get("trending_rank")
            if r is None:
                return 10_000.0
            return float(r)
        except (TypeError, ValueError):
            return 10_000.0
    # quality_score: higher better → negate
    q = _quality_value(coin, use_ai_score=use_ai_score)
    try:
        if q is None:
            return 0.0  # unknown mid
        return -float(q)
    except (TypeError, ValueError):
        return 0.0


def apply_observe_cap(
    coins: list[dict],
    *,
    max_coins: int,
    forced_symbols: set[str] | None = None,
) -> list[dict]:
    """Cap observe list; forced symbols (positions/base) kept first."""
    if not coins:
        return []
    try:
        max_n = int(max_coins)
    except (TypeError, ValueError):
        return list(coins)
    if max_n <= 0 or len(coins) <= max_n:
        return list(coins)

    forced = {str(s).strip() for s in (forced_symbols or set()) if s}
    by_sym: dict[str, dict] = {}
    order: list[str] = []
    for c in coins:
        s = _sym(c)
        if not s or s in by_sym:
            continue
        by_sym[s] = c
        order.append(s)

    forced_list = [by_sym[s] for s in order if s in forced]
    rest = [by_sym[s] for s in order if s not in forced]
    # Keep all forced even if over max (positions must stay observable)
    if len(forced_list) >= max_n:
        return forced_list
    need = max_n - len(forced_list)
    return forced_list + rest[:need]


def select_trade_universe(
    observe_coins: list[dict],
    *,
    open_symbols: set[str] | None = None,
    base_symbols: set[str] | None = None,
    trade_max_coins: int = 40,
    include_open_positions: bool = True,
    include_base: bool = True,
    rank_by: str = "quality_score",
    quality_lookup: dict[str, float] | None = None,
    use_ai_score: bool = True,
) -> list[dict]:
    """Build trade-eligible list from observe pool.

    Always includes open positions (and optionally base) even if over trade_max.
    Remaining slots filled by ranked discovery coins. ``use_ai_score`` False ranks
    by ``quality_score`` only (ignores ``quality_shadow_ai``) — #465.
    """
    open_syms = {str(s).strip() for s in (open_symbols or set()) if s}
    base_syms = {str(s).strip() for s in (base_symbols or set()) if s}

    by_sym: dict[str, dict] = {}
    order: list[str] = []
    for c in observe_coins or []:
        if not isinstance(c, dict):
            continue
        s = _sym(c)
        if not s or s in by_sym:
            continue
        row = dict(c)
        if quality_lookup and s in quality_lookup and row.get("quality_score") is None:
            try:
                row["quality_score"] = float(quality_lookup[s])
            except (TypeError, ValueError):
                pass
        by_sym[s] = row
        order.append(s)

    forced: list[str] = []
    forced_set: set[str] = set()
    if include_open_positions:
        for s in order:
            if s in open_syms and s not in forced_set:
                forced.append(s)
                forced_set.add(s)
        # open symbols missing from observe: skip (caller should merge positions into observe)
    if include_base:
        for s in order:
            if s in base_syms and s not in forced_set:
                forced.append(s)
                forced_set.add(s)

    rest = [s for s in order if s not in forced_set]
    if rank_by != "as_is":
        rest.sort(
            key=lambda s: rank_key_for_coin(by_sym[s], rank_by, use_ai_score=use_ai_score)
        )

    try:
        max_n = int(trade_max_coins)
    except (TypeError, ValueError):
        max_n = 40
    if max_n <= 0:
        max_n = len(order)

    slots = max(0, max_n - len(forced))
    chosen = forced + rest[:slots]
    return [by_sym[s] for s in chosen if s in by_sym]


def _lot_pre_seed_source(p) -> str:
    from services.universe.core_seed import _PRE_SEED_SOURCES

    keys = ("source", "entry_source", "watchlist_source")
    if isinstance(p, dict):
        for key in keys:
            val = str(p.get(key) or "").strip()
            if val in _PRE_SEED_SOURCES:
                return val
        return ""
    for key in keys:
        val = str(getattr(p, key, None) or "").strip()
        if val in _PRE_SEED_SOURCES:
            return val
    return ""


def _open_symbols_live() -> set[str]:
    from strategies.positions import list_active_positions

    out: set[str] = set()
    for p in list_active_positions() or []:
        if isinstance(p, dict):
            s = p.get("symbol")
        else:
            s = getattr(p, "symbol", None)
        if s:
            out.add(str(s).strip())
    return out


def _open_symbols_and_lot_rows() -> tuple[set[str], dict[str, dict]]:
    """Open symbols plus a watchlist-shaped row when the lot still has a pre-seed source.

    Calls ``_open_symbols_live`` first so a failing read (or a test patch)
    propagates instead of looking like an empty open set.
    """
    symbols = _open_symbols_live()
    rows: dict[str, dict] = {}
    try:
        from services.universe.core_seed import ticker_of
        from strategies.positions import list_active_positions

        for p in list_active_positions() or []:
            if isinstance(p, dict):
                s = p.get("symbol")
                tf = p.get("timeframe")
            else:
                s = getattr(p, "symbol", None)
                tf = getattr(p, "timeframe", None)
            if not s:
                continue
            sym = str(s).strip()
            src = _lot_pre_seed_source(p)
            if not src:
                continue
            rows[sym] = {
                "symbol": sym,
                "ticker": ticker_of(sym),
                "timeframe": str(tf or "4h").strip() or "4h",
                "active": True,
                "source": src,
            }
    except Exception:
        pass
    return symbols, rows


def _quality_lookup(
    tenant_id: str = "default", *, use_ai_score: bool = True
) -> dict[str, float]:
    try:
        from services.watchlist_quality.store import load_quality_scores

        data = load_quality_scores(tenant_id=tenant_id)
        out: dict[str, float] = {}
        for c in data.get("coins") or []:
            if not isinstance(c, dict):
                continue
            s = c.get("symbol")
            if not s:
                continue
            q = _quality_value(c, use_ai_score=use_ai_score)
            if q is not None:
                try:
                    out[str(s)] = float(q)
                except (TypeError, ValueError):
                    pass
        return out
    except Exception:
        return {}


def load_observe_universe(
    tenant_id: str | None = None,
    *,
    build_merged_fn: Callable[..., list] | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Broad universe for memory / WQE / observation."""
    from data_manager import load_config, load_watchlist

    if build_merged_fn is None:
        from data_manager import build_merged_watchlist_coins

        build_merged_fn = build_merged_watchlist_coins

    cfg = config if config is not None else load_config(tenant_id=tenant_id)
    coins = list(build_merged_fn(tenant_id=tenant_id, config=cfg))
    ucfg = universe_split_config(cfg)
    if not ucfg.get("split_enabled"):
        return coins

    base_syms = {
        str(c.get("symbol") or "").strip()
        for c in (load_watchlist(tenant_id=tenant_id) or [])
        if c.get("symbol")
    }
    try:
        open_syms = _open_symbols_live()
    except Exception as e:
        log(
            f"WARNING: open-position set unread; keeping pre-seed overlay rows ({e})",
            "WARNING",
        )
        open_syms = set()
    forced = base_syms | open_syms
    # Gainer movers into observe (shadow + trade_expand) — fail-open
    try:
        from services.gainer_universe.config import gainer_universe_enabled
        from services.gainer_universe.inject import merge_gainers_into_observe
        from services.gainer_universe.store import load_gainer_state

        if gainer_universe_enabled(cfg):
            coins = merge_gainers_into_observe(
                coins,
                load_gainer_state(),
                None,
                tenant_id=tenant_id or "default",
                root_config=cfg,
            )
            # refresh forced after merge not required — cap keeps base|open
    except Exception as e:
        log(f"gainer observe inject skip: {e}", "DEBUG")

    capped = apply_observe_cap(
        coins,
        max_coins=int(ucfg.get("observe_max_coins") or 100),
        forced_symbols=forced,
    )
    if len(capped) != len(coins):
        log(
            f"universe observe cap: {len(coins)} → {len(capped)} "
            f"(max={ucfg.get('observe_max_coins')})",
            "INFO",
        )
    return capped


def load_trade_universe(
    tenant_id: str | None = None,
    *,
    observe_coins: list[dict] | None = None,
    open_symbols: Iterable[str] | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Trade-eligible subset for BUY scan / process_coin."""
    from data_manager import load_config, load_watchlist

    cfg = config if config is not None else load_config(tenant_id=tenant_id)
    # #631: shadow/enforce without Ersatz-Cap refuses here (fail-closed at the
    # gate). off / absent is a no-op. Shadow does not change the live set.
    from services.universe.membership_revise import (
        ERSATZ_CAP_PATH,
        assert_membership_revise_config,
        bind_ersatz_cap,
        parse_membership_revise,
    )

    assert_membership_revise_config(cfg)
    revise = parse_membership_revise(cfg)
    ucfg = universe_split_config(cfg)

    if observe_coins is None:
        observe_coins = load_observe_universe(tenant_id=tenant_id, config=cfg)

    open_set_unknown = False
    open_lot_rows: dict[str, dict] = {}
    if open_symbols is None:
        try:
            open_set, open_lot_rows = _open_symbols_and_lot_rows()
        except Exception as e:
            log(
                f"WARNING: open-position set unread; keeping pre-seed overlay rows ({e})",
                "WARNING",
            )
            open_set = set()
            open_set_unknown = True
    else:
        open_set = {str(s).strip() for s in open_symbols if s}

    base_syms = {
        str(c.get("symbol") or "").strip()
        for c in (load_watchlist(tenant_id=tenant_id) or [])
        if c.get("symbol")
    }

    if not ucfg.get("split_enabled"):
        from services.universe.core_seed import finalize_trade_members

        return finalize_trade_members(
            list(observe_coins),
            open_symbols=open_set,
            open_lot_rows=open_lot_rows,
            open_set_unknown=open_set_unknown,
            base_symbols=base_syms,
            config=cfg,
        )
    from core.tenant_context import resolve_tenant_id

    from services.watchlist_quality.config import use_ai_sort_score

    tid = resolve_tenant_id(tenant_id)
    # #465: same rollback switch as the WQE soft sort (ai.sort_by / ai.enabled)
    use_ai = use_ai_sort_score(cfg)
    ist_trade_max = int(ucfg.get("trade_max_coins") or 40)
    ist_rank_by = str(ucfg.get("trade_rank_by") or "quality_score")
    # #631 enforce clamps staged trade_max/rank to the Ersatz-Cap. Shadow keeps Ist.
    trade_max = revise.live_trade_max(ist_trade_max)
    rank_by = revise.live_rank_by(ist_rank_by)
    qlookup = (
        _quality_lookup(tid, use_ai_score=use_ai)
        if rank_by == "quality_score"
        else None
    )
    include_open = bool(ucfg.get("trade_include_open_positions", True))
    include_base = bool(ucfg.get("trade_include_base", True))
    trade = select_trade_universe(
        list(observe_coins),
        open_symbols=open_set,
        base_symbols=base_syms,
        trade_max_coins=trade_max,
        include_open_positions=include_open,
        include_base=include_base,
        rank_by=rank_by,
        quality_lookup=qlookup,
        use_ai_score=use_ai,
    )
    # Gate prev-day expand into trade (mode=trade_expand only) — fail-open
    try:
        from services.gainer_universe.config import gainer_trade_expand_enabled
        from services.gainer_universe.inject import merge_expand_into_trade
        from services.gainer_universe.store import load_gainer_state

        if gainer_trade_expand_enabled(cfg):
            before = len(trade)
            trade = merge_expand_into_trade(
                trade, load_gainer_state(), root_config=cfg, tenant_id=tid
            )
            if len(trade) != before:
                log(
                    f"gainer trade expand: {before} → {len(trade)} coins",
                    "INFO",
                )
    except Exception as e:
        log(f"gainer trade inject skip: {e}", "DEBUG")

    log(
        f"universe split: observe={len(observe_coins)} trade={len(trade)} "
        f"open={len(open_set)} max_trade={trade_max}",
        "INFO",
    )
    from services.universe.core_seed import finalize_trade_members

    trade = finalize_trade_members(
        trade,
        open_symbols=open_set,
        open_lot_rows=open_lot_rows,
        open_set_unknown=open_set_unknown,
        base_symbols=base_syms,
        config=cfg,
    )
    if revise.mode == "shadow":
        log(
            f"membership_revise shadow {ERSATZ_CAP_PATH}={revise.cap} "
            f"staged_trade_max={revise.staged_trade_max} "
            f"live_trade_max={ist_trade_max} unchanged",
            "DEBUG",
        )
    elif revise.mode == "enforce":
        forced = set(open_set)
        if include_base:
            forced |= base_syms
        before = len(trade)
        trade = bind_ersatz_cap(
            trade,
            cap=int(revise.cap or 0),
            forced_symbols=forced,
        )
        log(
            f"membership_revise enforce {ERSATZ_CAP_PATH}={revise.cap} "
            f"effective_trade_max={trade_max} rank={rank_by} "
            f"trade={before}->{len(trade)}",
            "INFO",
        )
    return trade


def is_trade_eligible(
    symbol: str,
    *,
    trade_symbols: set[str] | None = None,
    tenant_id: str | None = None,
    config: dict | None = None,
    open_symbols: set[str] | None = None,
) -> bool:
    """True if symbol may receive new BUY under split (fail-open if split off)."""
    from data_manager import load_config

    cfg = config if config is not None else load_config(tenant_id=tenant_id)
    if not universe_split_enabled(cfg):
        return True
    sym = str(symbol or "").strip()
    if not sym:
        return False
    if open_symbols and sym in open_symbols:
        return True  # DCA / manage existing always ok at this layer
    if trade_symbols is not None:
        return sym in trade_symbols
    try:
        trade = load_trade_universe(tenant_id=tenant_id, config=cfg)
        return any(_sym(c) == sym for c in trade)
    except Exception as e:
        # Fail-closed (#422): if the trade universe cannot be loaded, do not
        # let a new BUY through — the universe_trade_cap gate would be skipped.
        # Open positions were already allowed above via open_symbols.
        log(
            f"is_trade_eligible: load_trade_universe failed for {sym}, "
            f"treating as NOT eligible (fail-closed): {e}",
            "WARNING",
        )
        return False
