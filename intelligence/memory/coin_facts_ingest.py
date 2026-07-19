"""Ingest coin facts into memory_market_events (#103).

LEDGER SAFETY: only memory_* via MemoryStore. Fail-open.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from intelligence.memory.coin_facts import (
    CoinFactDraft,
    coin_facts_config,
    coin_facts_enabled,
    normalize_symbol,
)
from intelligence.memory.coin_facts_cmc import fetch_and_parse_coin, resolve_cmc_slug
from intelligence.memory.event_ingest import make_event_id
from intelligence.memory.models import MarketEvent, utc_now_iso
from intelligence.memory.store import MemoryStore, memory_enabled
from logger import log

FetchFn = Callable[[str], str]


def coin_fact_universe(
    config_raw: dict | None = None,
    *,
    list_positions_fn=None,
    load_watchlist_fn=None,
) -> list[str]:
    """Open positions ∪ active watchlist, positions first, capped."""
    cfg = coin_facts_config(config_raw)
    cmc = (cfg.get("sources") or {}).get("cmc_ai") or {}
    try:
        cap = max(1, int(cmc.get("max_coins_per_cycle") or 40))
    except (TypeError, ValueError):
        cap = 40
    want = list(cfg.get("universe") or ["open_positions", "watchlist"])
    out: list[str] = []
    seen: set[str] = set()

    if "open_positions" in want:
        try:
            if list_positions_fn is None:
                from strategies.positions import list_active_positions

                list_positions_fn = list_active_positions
            for lot in list_positions_fn() or []:
                sym = normalize_symbol(str((lot or {}).get("symbol") or ""))
                if not sym or sym in seen:
                    continue
                if sym.upper().startswith("TEST"):
                    continue
                seen.add(sym)
                out.append(sym)
        except Exception as e:
            log(f"coin_facts universe positions: {e}", "DEBUG")

    if "watchlist" in want or "trending_overlay" in want:
        try:
            if load_watchlist_fn is None:
                from data_manager import load_effective_watchlist

                load_watchlist_fn = load_effective_watchlist
            for coin in load_watchlist_fn() or []:
                if not (coin or {}).get("active", True):
                    continue
                sym = normalize_symbol(str((coin or {}).get("symbol") or ""))
                if not sym or sym in seen:
                    continue
                if sym.upper().startswith("TEST"):
                    continue
                seen.add(sym)
                out.append(sym)
        except Exception as e:
            log(f"coin_facts universe watchlist: {e}", "DEBUG")

    return out[:cap]


def draft_to_event(
    draft: CoinFactDraft,
    *,
    symbol: str,
    slug: str,
    url: str = "",
    as_of: str | None = None,
) -> MarketEvent | None:
    """Map draft → MarketEvent. Skips ignore_target."""
    if draft.event_type in ("ignore_target",):
        return None
    sym = normalize_symbol(symbol)
    as_of = as_of or utc_now_iso()
    day = as_of[:10]
    key = f"{slug}|{draft.source}|{draft.event_type}|{day}|{draft.description[:80]}"
    eid = make_event_id(draft.source or "cmc_ai", key)
    meta = dict(draft.metadata or {})
    meta.setdefault("slug", slug)
    meta.setdefault("kind", "coin_fact")
    return MarketEvent(
        event_id=eid,
        timestamp=as_of,
        event_type=draft.event_type,
        symbols=[sym],
        impact_score=float(draft.impact_score),
        description=draft.description[:500],
        source=draft.source,
        url=url or "",
        metadata=meta,
    )


def persist_coin_fact(
    draft: CoinFactDraft,
    *,
    symbol: str,
    slug: str,
    url: str = "",
    store: MemoryStore | None = None,
    embed: bool = False,
) -> str:
    """Upsert one fact. Returns event_id or ''."""
    if not memory_enabled():
        return ""
    store = store or MemoryStore()
    ev = draft_to_event(draft, symbol=symbol, slug=slug, url=url)
    if ev is None:
        return ""
    if embed:
        try:
            from intelligence.memory.embeddings import embed_event

            ev.embedding = embed_event(ev.description, event_type=ev.event_type)
        except Exception:
            pass
    if store.upsert_event(ev):
        return ev.event_id
    return ""


def sync_coin_facts(
    store: MemoryStore | None = None,
    *,
    fetch_fn: FetchFn | None = None,
    config_raw: dict | None = None,
    symbols: list[str] | None = None,
    list_positions_fn=None,
    load_watchlist_fn=None,
) -> dict[str, Any]:
    """One cycle: universe → parse → persist. Fail-open stats dict."""
    raw = config_raw
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}

    if not coin_facts_enabled(raw):
        return {"enabled": False, "skipped": True, "coins": 0, "events_written": 0}

    if not memory_enabled(raw):
        return {"enabled": True, "skipped": True, "reason": "memory_disabled", "events_written": 0}

    cfg = coin_facts_config(raw)
    cmc_ai = (cfg.get("sources") or {}).get("cmc_ai") or {}
    cmc_pro = (cfg.get("sources") or {}).get("cmc_pro") or {}
    pro_on = bool(cmc_pro.get("enabled", True))
    ai_on = bool(cmc_ai.get("enabled", True))
    if not pro_on and not ai_on:
        return {
            "enabled": True,
            "skipped": True,
            "reason": "all_sources_disabled",
            "events_written": 0,
        }

    try:
        max_ev = max(1, int(cmc_ai.get("max_events_per_coin_cycle") or 8))
    except (TypeError, ValueError):
        max_ev = 8

    store = store or MemoryStore()
    universe = symbols if symbols is not None else coin_fact_universe(
        raw,
        list_positions_fn=list_positions_fn,
        load_watchlist_fn=load_watchlist_fn,
    )

    written = 0
    errors: list[str] = []
    per_coin: dict[str, int] = {}
    seen_ids: set[str] = set()
    pro_written = 0

    # --- D8b: CMC Pro (structured JSON) first ---
    if pro_on:
        try:
            from intelligence.memory.coin_facts_cmc_pro import collect_cmc_pro_drafts

            for sym, d in collect_cmc_pro_drafts(universe, config_raw=raw):
                if d.event_type == "ignore_target":
                    continue
                slug = resolve_cmc_slug(sym) or normalize_symbol(sym).split("/")[0].lower()
                eid = persist_coin_fact(
                    d, symbol=sym, slug=slug, url="", store=store, embed=False
                )
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    written += 1
                    pro_written += 1
                    per_coin[sym] = int(per_coin.get(sym) or 0) + 1
        except Exception as e:
            errors.append(f"cmc_pro:{str(e)[:100]}")
            log(f"coin_facts cmc_pro failed: {e}", "WARNING")

    # --- D8: CMC AI HTML (optional fallback / parallel) ---
    if ai_on:
        for sym in universe:
            try:
                slug = resolve_cmc_slug(sym)
                if not slug:
                    continue
                drafts = fetch_and_parse_coin(sym, fetch_fn=fetch_fn, slug=slug)
                n = 0
                for d in drafts:
                    if n >= max_ev:
                        break
                    if d.event_type == "ignore_target":
                        continue
                    from intelligence.memory.coin_facts_cmc import build_cmc_ai_urls

                    urls = build_cmc_ai_urls(slug)
                    url = ""
                    ep = (d.metadata or {}).get("endpoint") or ""
                    if ep == "latest_updates":
                        url = urls.get("latest_updates", "")
                    elif ep == "price_analysis":
                        url = urls.get("price_analysis", "")
                    elif ep == "price_prediction":
                        url = urls.get("price_prediction", "")
                    eid = persist_coin_fact(
                        d, symbol=sym, slug=slug, url=url, store=store, embed=False
                    )
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        written += 1
                        n += 1
                        per_coin[sym] = int(per_coin.get(sym) or 0) + 1
            except Exception as e:
                errors.append(f"{sym}:{str(e)[:80]}")

    return {
        "enabled": True,
        "skipped": False,
        "coins": len(universe),
        "events_written": written,
        "cmc_pro_events": pro_written,
        "per_coin": per_coin,
        "errors": errors[:10],
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
