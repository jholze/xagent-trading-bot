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


def _source_cap(cfg: dict) -> int:
    """Max symbols per cycle from cmc_pro / cmc_ai config (larger wins)."""
    sources = cfg.get("sources") or {}
    caps: list[int] = []
    for key, field in (
        ("cmc_pro", "max_symbols_per_cycle"),
        ("cmc_ai", "max_coins_per_cycle"),
    ):
        try:
            caps.append(int((sources.get(key) or {}).get(field) or 40))
        except (TypeError, ValueError):
            caps.append(40)
    return max(1, max(caps) if caps else 40)


def _append_symbol(out: list[str], seen: set[str], raw: str) -> None:
    sym = normalize_symbol(raw)
    if not sym or sym in seen or sym.upper().startswith("TEST"):
        return
    seen.add(sym)
    out.append(sym)


def coin_fact_universe(
    config_raw: dict | None = None,
    *,
    list_positions_fn=None,
    load_watchlist_fn=None,
) -> list[str]:
    """Open positions ∪ active watchlist, positions first, capped."""
    cfg = coin_facts_config(config_raw)
    cap = _source_cap(cfg)
    want = list(cfg.get("universe") or ["open_positions", "watchlist"])
    out: list[str] = []
    seen: set[str] = set()

    if "open_positions" in want:
        try:
            if list_positions_fn is None:
                from strategies.positions import list_active_positions

                list_positions_fn = list_active_positions
            for lot in list_positions_fn() or []:
                _append_symbol(out, seen, str((lot or {}).get("symbol") or ""))
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
                _append_symbol(out, seen, str((coin or {}).get("symbol") or ""))
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

    def _accept(sym: str, draft: CoinFactDraft, *, url: str = "", count_pro: bool = False) -> None:
        nonlocal written, pro_written
        if draft.event_type == "ignore_target":
            return
        slug = resolve_cmc_slug(sym) or normalize_symbol(sym).split("/")[0].lower()
        eid = persist_coin_fact(
            draft, symbol=sym, slug=slug, url=url, store=store, embed=False
        )
        if not eid or eid in seen_ids:
            return
        seen_ids.add(eid)
        written += 1
        per_coin[sym] = int(per_coin.get(sym) or 0) + 1
        if count_pro:
            pro_written += 1

    # --- D8b: CMC Pro (structured JSON) first ---
    if pro_on:
        try:
            from intelligence.memory.coin_facts_cmc_pro import collect_cmc_pro_drafts

            for sym, d in collect_cmc_pro_drafts(universe, config_raw=raw):
                _accept(sym, d, count_pro=True)
        except Exception as e:
            errors.append(f"cmc_pro:{str(e)[:100]}")
            log(f"coin_facts cmc_pro failed: {e}", "WARNING")

    # --- D8: CMC AI HTML (optional) ---
    if ai_on:
        from intelligence.memory.coin_facts_cmc import build_cmc_ai_urls

        for sym in universe:
            try:
                slug = resolve_cmc_slug(sym)
                if not slug:
                    continue
                drafts = fetch_and_parse_coin(sym, fetch_fn=fetch_fn, slug=slug)
                urls = build_cmc_ai_urls(slug)
                ep_to_url = {
                    "latest_updates": urls.get("latest_updates", ""),
                    "price_analysis": urls.get("price_analysis", ""),
                    "price_prediction": urls.get("price_prediction", ""),
                }
                for i, d in enumerate(drafts):
                    if i >= max_ev:
                        break
                    ep = str((d.metadata or {}).get("endpoint") or "")
                    _accept(sym, d, url=ep_to_url.get(ep, ""))
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
