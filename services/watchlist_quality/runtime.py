"""Runtime glue: score + soft/enforce transform for effective watchlist (fail-open)."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from logger import log
from services.watchlist_quality.config import vol_floor_t1_usd, wqe_mode
from services.watchlist_quality.engine import run_shadow_score
from services.watchlist_quality.enforce import apply_enforce_tiers, filter_new_adds_memory
from services.watchlist_quality.soft import apply_soft_watchlist
from services.watchlist_quality.venue_batch import attach_quote_volumes

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 45.0


def _open_symbols() -> set[str]:
    try:
        from strategies.positions import get_open_positions

        pos = get_open_positions() or []
        out = set()
        for p in pos:
            if isinstance(p, dict):
                s = p.get("symbol") or p.get("pair")
            else:
                s = getattr(p, "symbol", None)
            if s:
                out.add(str(s).strip())
        return out
    except Exception:
        return set()


def _cache_key(tenant_id: str, mode: str, coins: list[dict[str, Any]]) -> str:
    syms = sorted(str(c.get("symbol") or "") for c in coins if isinstance(c, dict))
    h = hashlib.sha1("|".join(syms).encode()).hexdigest()[:12]
    return f"{tenant_id}|{mode}|{len(syms)}|{h}"


def apply_wqe_to_watchlist(
    coins: list[dict[str, Any]],
    *,
    config: dict | None = None,
    base_symbols: set[str] | None = None,
    tenant_id: str = "default",
    llm_json_fn=None,
    attach_vol: bool = True,
) -> list[dict[str, Any]]:
    """Score candidates and apply soft or enforce membership rules.

    Cached briefly to avoid re-scoring every command. Fail-open → original coins.
    """
    mode = wqe_mode(config)
    if mode not in ("soft", "enforce") or not coins:
        return list(coins)

    cache_key = _cache_key(tenant_id, mode, coins)
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and now - hit[0] <= _CACHE_TTL:
        return list(hit[1])

    try:
        open_syms = _open_symbols()
        work = list(coins)
        if attach_vol:
            try:
                work = attach_quote_volumes(work, config=config)
            except Exception:
                pass

        cfg = dict(config or {})
        wq = dict(cfg.get("watchlist_quality") or {})
        ai = dict(wq.get("ai") or {})
        if llm_json_fn is None:
            ai["enabled"] = False
        wq["ai"] = ai
        cfg["watchlist_quality"] = wq

        n_in = len(work)
        summary = run_shadow_score(
            work,
            config=cfg,
            persist=True,
            open_symbols=open_syms,
            llm_json_fn=llm_json_fn,
            tenant_id=tenant_id,
        )
        try:
            from services.watchlist_quality.metrics import note_scored

            note_scored(int(summary.get("scored") or 0))
        except Exception:
            pass

        scored_rows = []
        by_sym = {
            str(c.get("symbol")): c for c in (summary.get("coins") or []) if c.get("symbol")
        }
        for c in work:
            sym = str(c.get("symbol") or "")
            row = dict(c)
            sc = by_sym.get(sym) or {}
            if sc:
                row["quality_score"] = sc.get("quality_score")
                if sc.get("quality_shadow_ai") is not None:
                    row["quality_shadow_ai"] = sc.get("quality_shadow_ai")
                row["tier_hint"] = sc.get("tier_hint")
                row["flags"] = sc.get("flags") or []
                mem = sc.get("memory") or {}
                row["hard_exclude_new_add"] = mem.get("hard_exclude_new_add")
                row["memory"] = mem
                m = sc.get("metrics") or {}
                if row.get("quote_vol_24h") is None and m.get("quote_vol_24h") is not None:
                    row["quote_vol_24h"] = m.get("quote_vol_24h")
                if not row.get("source"):
                    row["source"] = m.get("source")
            if sym in open_syms:
                row["is_open"] = True
            scored_rows.append(row)

        softed = apply_soft_watchlist(
            scored_rows,
            open_symbols=open_syms,
            min_quote_vol_usd=vol_floor_t1_usd(cfg),
            use_ai_score=True,
        )
        dropped = max(0, n_in - len(softed))
        if dropped:
            try:
                from services.watchlist_quality.metrics import note_soft_dropped

                note_soft_dropped(dropped)
            except Exception:
                pass

        if mode == "enforce":
            softed = filter_new_adds_memory(
                softed, base_symbols=base_symbols, open_symbols=open_syms
            )
            regime = "neutral"
            try:
                from services.watchlist_quality.engine import _regime_hints

                sm, pol = _regime_hints(cfg)
                if pol and str(pol).lower() in ("block", "risk_off"):
                    regime = "risk-off"
                elif sm is not None and float(sm) >= 1.0:
                    regime = "risk-on"
            except Exception:
                pass
            softed = apply_enforce_tiers(
                softed, open_symbols=open_syms, config=cfg, regime=regime
            )

        _CACHE[cache_key] = (now, softed)
        log(
            f"WQE {mode}: n_in={n_in} n_out={len(softed)} open={len(open_syms)} tenant={tenant_id}",
            "INFO",
        )
        try:
            from services.watchlist_quality.event_log import log_soft_apply

            log_soft_apply(
                mode=mode,
                n_in=n_in,
                n_out=len(softed),
                open_n=len(open_syms),
                tenant_id=tenant_id,
                config=cfg,
            )
        except Exception:
            pass
        return softed
    except Exception as e:
        log(f"WQE apply_wqe_to_watchlist fail-open: {e}", "WARNING")
        return list(coins)


def clear_runtime_cache() -> None:
    _CACHE.clear()
