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


def rank_key_for_coin(coin: dict, rank_by: str) -> float:
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
    q = coin.get("quality_shadow_ai")
    if q is None:
        q = coin.get("quality_score")
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
) -> list[dict]:
    """Build trade-eligible list from observe pool.

    Always includes open positions (and optionally base) even if over trade_max.
    Remaining slots filled by ranked discovery coins.
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
        rest.sort(key=lambda s: rank_key_for_coin(by_sym[s], rank_by))

    try:
        max_n = int(trade_max_coins)
    except (TypeError, ValueError):
        max_n = 40
    if max_n <= 0:
        max_n = len(order)

    slots = max(0, max_n - len(forced))
    chosen = forced + rest[:slots]
    return [by_sym[s] for s in chosen if s in by_sym]


def _open_symbols_live() -> set[str]:
    try:
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
    except Exception:
        return set()


def _quality_lookup(tenant_id: str = "default") -> dict[str, float]:
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
            q = c.get("quality_shadow_ai")
            if q is None:
                q = c.get("quality_score")
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
    open_syms = _open_symbols_live()
    forced = base_syms | open_syms
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
    ucfg = universe_split_config(cfg)

    if observe_coins is None:
        observe_coins = load_observe_universe(tenant_id=tenant_id, config=cfg)

    if not ucfg.get("split_enabled"):
        return list(observe_coins)

    if open_symbols is None:
        open_set = _open_symbols_live()
    else:
        open_set = {str(s).strip() for s in open_symbols if s}

    base_syms = {
        str(c.get("symbol") or "").strip()
        for c in (load_watchlist(tenant_id=tenant_id) or [])
        if c.get("symbol")
    }
    tid = tenant_id or "default"
    qlookup = (
        _quality_lookup(tid)
        if ucfg.get("trade_rank_by") == "quality_score"
        else None
    )
    trade = select_trade_universe(
        list(observe_coins),
        open_symbols=open_set,
        base_symbols=base_syms,
        trade_max_coins=int(ucfg.get("trade_max_coins") or 40),
        include_open_positions=bool(ucfg.get("trade_include_open_positions", True)),
        include_base=bool(ucfg.get("trade_include_base", True)),
        rank_by=str(ucfg.get("trade_rank_by") or "quality_score"),
        quality_lookup=qlookup,
    )
    log(
        f"universe split: observe={len(observe_coins)} trade={len(trade)} "
        f"open={len(open_set)} max_trade={ucfg.get('trade_max_coins')}",
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
    except Exception:
        return True  # fail-open
