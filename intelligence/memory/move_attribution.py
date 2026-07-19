"""Large price moves → candidate trigger attribution (memory only).

When a coin in the open book / watchlist moves hard, look for nearby
memory events (news, social, unlocks, macro pressure, coin facts) and
write a linking MarketEvent for RAG / audit / policy context.

LEDGER SAFETY: never writes orders/positions. Fail-open.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from intelligence.memory.embeddings import embed_event, embed_text
from intelligence.memory.event_ingest import make_event_id
from intelligence.memory.models import MarketEvent, utc_now_iso
from intelligence.memory.store import MemoryStore, memory_enabled
from logger import log

EVT_PRICE_MOVE = "price_move"
EVT_MOVE_LINK = "price_move_attribution"

# Event types that often explain idiosyncratic moves
_TRIGGER_TYPES = frozenset(
    {
        "token_unlock",
        "unlock",
        "structure_risk",
        "profit_taking_narrative",
        "volume_breakout",
        "cmc_social",
        "cmc_trending",
        "cmc_quote_extreme",
        "lc_social_spike",
        "lc_social_fade",
        "lc_sentiment_extreme",
        "macro_pressure",
        "macro_window",
        "macro_scheduled",
        "session_pressure",
        "pm_pressure",
        "pm_mispricing",
        "pm_prob_move",
        "news",
        "rss",
        "coin_fact",
        "dca_decision",
    }
)


@dataclass
class MoveSnap:
    symbol: str
    chg_24h: float
    vol_chg_24h: float = 0.0
    price: float = 0.0
    source: str = ""
    vs_btc: float | None = None  # chg_24h - btc_chg_24h when known


@dataclass
class TriggerHit:
    event_id: str
    event_type: str
    score: float
    description: str
    source: str = ""
    hours_delta: float | None = None


@dataclass
class AttributionResult:
    moves_seen: int = 0
    moves_large: int = 0
    attributions_written: int = 0
    links_found: int = 0
    symbols: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "moves_seen": self.moves_seen,
            "moves_large": self.moves_large,
            "attributions_written": self.attributions_written,
            "links_found": self.links_found,
            "symbols": list(self.symbols)[:40],
            "errors": list(self.errors)[:5],
        }


def move_attribution_config(config: dict | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from core.config import get_bot_config

            config = get_bot_config().raw
        except Exception:
            config = {}
    mem = (config or {}).get("memory") or {}
    raw = dict(mem.get("move_attribution") or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "abs_chg_24h_pct": float(raw.get("abs_chg_24h_pct", 12.0)),
        "rel_btc_pct": float(raw.get("rel_btc_pct", 8.0)),
        "lookback_hours": float(raw.get("lookback_hours", 72.0)),
        "max_symbols_per_cycle": int(raw.get("max_symbols_per_cycle", 30)),
        "max_triggers": int(raw.get("max_triggers", 5)),
        "index_rag": bool(raw.get("index_rag", True)),
        "min_trigger_score": float(raw.get("min_trigger_score", 0.12)),
        "prefer_idiosyncratic": bool(raw.get("prefer_idiosyncratic", True)),
    }


def move_attribution_enabled(config: dict | None = None) -> bool:
    if os.environ.get("MEMORY_MOVE_ATTRIBUTION", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    return bool(move_attribution_config(config).get("enabled", True))


def is_large_move(
    snap: MoveSnap,
    *,
    abs_chg: float = 12.0,
    rel_btc: float = 8.0,
    prefer_idiosyncratic: bool = True,
) -> bool:
    """True if absolute move is large, or move is large vs BTC."""
    a = abs(float(snap.chg_24h or 0))
    if a >= abs_chg:
        if prefer_idiosyncratic and snap.vs_btc is not None:
            # still large absolute; keep
            return True
        return True
    if snap.vs_btc is not None and abs(float(snap.vs_btc)) >= rel_btc:
        return True
    return False


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:32] if "T" in s else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _base(sym: str) -> str:
    s = str(sym or "").upper().replace("-", "/")
    return s.split("/")[0] if s else ""


def score_trigger_for_move(
    *,
    move: MoveSnap,
    event: MarketEvent,
    now: datetime | None = None,
    lookback_hours: float = 72.0,
) -> float:
    """Heuristic relevance of a memory event to a price move (0..1+)."""
    now = now or datetime.now(timezone.utc)
    e_ts = _parse_ts(event.timestamp)
    if e_ts is None:
        return 0.0
    hours = abs((now - e_ts).total_seconds()) / 3600.0
    if hours > lookback_hours:
        return 0.0

    score = 0.0
    et = str(event.event_type or "").lower()
    desc = str(event.description or "").lower()
    bases = {_base(s) for s in (event.symbols or [])}
    move_base = _base(move.symbol)

    # Symbol match
    if move.symbol in (event.symbols or []) or move_base in bases:
        score += 0.45
    elif bases and move_base:
        # weak global events
        if bases <= {"BTC", "ETH"} or "BTC/USDT" in (event.symbols or []):
            score += 0.08
        else:
            return 0.0
    else:
        # text mention
        if move_base and move_base.lower() in desc:
            score += 0.35
        else:
            return 0.0

    if et in _TRIGGER_TYPES:
        score += 0.2
    if et in ("token_unlock", "unlock", "structure_risk"):
        score += 0.15
    if et.startswith("lc_") or et.startswith("cmc_"):
        score += 0.1
    if et in ("macro_pressure", "macro_window", "pm_pressure"):
        score += 0.05 if abs(move.chg_24h) > 15 else 0.12

    # Direction alignment (sell-ish events with dump)
    impact = float(event.impact_score or 0)
    if move.chg_24h <= -abs_threshold_soft() and impact < -0.15:
        score += 0.12
    if move.chg_24h >= abs_threshold_soft() and impact > 0.15:
        score += 0.12

    # Recency decay
    recency = max(0.0, 1.0 - hours / max(lookback_hours, 1.0))
    score *= 0.55 + 0.45 * recency

    # Semantic nudge (hash embedding — cheap)
    try:
        q = f"{move.symbol} {move.chg_24h:+.1f}% move trigger {et}"
        from intelligence.memory.embeddings import cosine

        score += 0.15 * max(0.0, cosine(embed_text(q), event.embedding or embed_event(desc, event_type=et)))
    except Exception:
        pass

    return float(score)


def abs_threshold_soft() -> float:
    return 8.0


def find_triggers(
    store: MemoryStore,
    move: MoveSnap,
    *,
    lookback_hours: float = 72.0,
    max_triggers: int = 5,
    min_score: float = 0.12,
    now: datetime | None = None,
) -> list[TriggerHit]:
    """Pull candidate events for a move and rank them."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates: list[MarketEvent] = []

    # Symbol-scoped recent events
    try:
        candidates.extend(
            store.list_events(symbol=move.symbol, since_iso=since, limit=80) or []
        )
    except Exception:
        pass
    # Broader scan (macro / news often not symbol-tagged well)
    try:
        for e in store.list_events(since_iso=since, limit=120) or []:
            if e.event_id in {c.event_id for c in candidates}:
                continue
            et = str(e.event_type or "")
            if et in _TRIGGER_TYPES or et.startswith("macro") or et.startswith("pm_"):
                candidates.append(e)
    except Exception:
        pass

    # Vector similar_events fail-open
    try:
        from intelligence.memory.retriever import similar_events

        direction = "pump" if move.chg_24h >= 0 else "dump"
        q = (
            f"{move.symbol} {direction} {move.chg_24h:+.1f}% 24h "
            f"unlock news social funding macro"
        )
        for e in similar_events(q, symbol=move.symbol, k=8, store=store) or []:
            if e.event_id not in {c.event_id for c in candidates}:
                candidates.append(e)
    except Exception:
        pass

    scored: list[TriggerHit] = []
    for e in candidates:
        if str(e.event_type or "") in (EVT_PRICE_MOVE, EVT_MOVE_LINK):
            continue  # don't self-link
        sc = score_trigger_for_move(
            move=move, event=e, now=now, lookback_hours=lookback_hours
        )
        if sc < min_score:
            continue
        e_ts = _parse_ts(e.timestamp)
        hours = None
        if e_ts:
            hours = abs((now - e_ts).total_seconds()) / 3600.0
        scored.append(
            TriggerHit(
                event_id=e.event_id,
                event_type=str(e.event_type or ""),
                score=round(sc, 4),
                description=str(e.description or "")[:200],
                source=str(e.source or ""),
                hours_delta=round(hours, 2) if hours is not None else None,
            )
        )
    scored.sort(key=lambda t: t.score, reverse=True)
    return scored[: max(1, int(max_triggers))]


def build_attribution_event(
    move: MoveSnap,
    triggers: list[TriggerHit],
    *,
    as_of: str | None = None,
) -> MarketEvent:
    as_of = as_of or utc_now_iso()
    direction = "pump" if move.chg_24h >= 0 else "dump"
    top = triggers[0] if triggers else None
    if top:
        link_txt = (
            f"top_trigger={top.event_type} score={top.score:.2f} "
            f"({top.description[:80]})"
        )
    else:
        link_txt = "no strong trigger found in lookback window"
    vs = ""
    if move.vs_btc is not None:
        vs = f" vsBTC={move.vs_btc:+.1f}pp"
    desc = (
        f"Large {direction} {move.symbol} {move.chg_24h:+.1f}% 24h{vs} "
        f"(src={move.source or '?'}). Attribution: {link_txt}"
    )
    impact = max(-1.0, min(1.0, move.chg_24h / 40.0))
    meta = {
        "kind": "move_attribution",
        "chg_24h": round(move.chg_24h, 3),
        "vol_chg_24h": round(move.vol_chg_24h, 3),
        "price": move.price,
        "vs_btc": move.vs_btc,
        "move_source": move.source,
        "triggers": [
            {
                "event_id": t.event_id,
                "event_type": t.event_type,
                "score": t.score,
                "source": t.source,
                "hours_delta": t.hours_delta,
                "description": t.description[:120],
            }
            for t in triggers
        ],
        "related_event_ids": [t.event_id for t in triggers],
    }
    day = as_of[:10]
    eid = make_event_id(
        "move_attr",
        f"{move.symbol}|{day}|{direction}|{round(move.chg_24h, 1)}",
    )
    return MarketEvent(
        event_id=eid,
        timestamp=as_of,
        event_type=EVT_MOVE_LINK,
        symbols=[move.symbol],
        impact_score=impact,
        description=desc[:500],
        source="move_attribution",
        metadata=meta,
        embedding=embed_event(desc, event_type=EVT_MOVE_LINK),
    )


def fetch_move_snaps(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
) -> list[MoveSnap]:
    """Best-effort 24h moves: CMC quotes → fail-open empty."""
    out: list[MoveSnap] = []
    if not symbols:
        return out
    btc_chg = None
    try:
        from intelligence.memory.coin_facts_cmc_pro import (
            fetch_quotes_for_symbols,
            parse_quote_snap,
        )

        # include BTC for relative move
        want = list(dict.fromkeys(["BTC/USDT"] + list(symbols)))
        quotes = fetch_quotes_for_symbols(want, config_raw=config_raw) or {}
        if "BTC/USDT" in quotes:
            btc_snap = parse_quote_snap("BTC/USDT", quotes["BTC/USDT"])
            btc_chg = float(btc_snap.chg)
        for sym in symbols:
            q = quotes.get(sym) or quotes.get(sym.replace("/USDT", ""))
            if not q:
                continue
            snap = parse_quote_snap(sym, q, btc_chg_24h=btc_chg)
            vs = None
            if btc_chg is not None:
                vs = float(snap.chg) - float(btc_chg)
            out.append(
                MoveSnap(
                    symbol=snap.symbol,
                    chg_24h=float(snap.chg),
                    vol_chg_24h=float(snap.vol_chg or 0),
                    price=float(snap.price or 0),
                    source="cmc_pro_quotes",
                    vs_btc=vs,
                )
            )
    except Exception as e:
        log(f"move_attribution quotes: {e}", "DEBUG")
    return out


def sync_move_attribution(
    store: MemoryStore | None = None,
    *,
    config_raw: dict | None = None,
    symbols: list[str] | None = None,
    moves: list[MoveSnap] | None = None,
) -> dict[str, Any]:
    """Detect large moves in universe and write attribution events."""
    if not memory_enabled(config_raw):
        return {"enabled": False, "reason": "memory_disabled"}
    if not move_attribution_enabled(config_raw):
        return {"enabled": False, "reason": "move_attribution_disabled"}

    cfg = move_attribution_config(config_raw)
    store = store or MemoryStore()
    result = AttributionResult()

    if symbols is None:
        try:
            from intelligence.memory.coin_facts_ingest import coin_fact_universe

            symbols = coin_fact_universe(config_raw)
        except Exception as e:
            result.errors.append(f"universe:{e}")
            symbols = []
    symbols = list(symbols or [])[: int(cfg["max_symbols_per_cycle"])]

    snaps = moves if moves is not None else fetch_move_snaps(symbols, config_raw=config_raw)
    result.moves_seen = len(snaps)

    for snap in snaps:
        if not is_large_move(
            snap,
            abs_chg=float(cfg["abs_chg_24h_pct"]),
            rel_btc=float(cfg["rel_btc_pct"]),
            prefer_idiosyncratic=bool(cfg["prefer_idiosyncratic"]),
        ):
            continue
        result.moves_large += 1
        result.symbols.append(snap.symbol)
        try:
            triggers = find_triggers(
                store,
                snap,
                lookback_hours=float(cfg["lookback_hours"]),
                max_triggers=int(cfg["max_triggers"]),
                min_score=float(cfg["min_trigger_score"]),
            )
            result.links_found += len(triggers)
            ev = build_attribution_event(snap, triggers)
            if store.get_event(ev.event_id):
                continue  # already attributed today for this magnitude bucket
            if store.upsert_event(ev):
                result.attributions_written += 1
                if cfg.get("index_rag", True):
                    try:
                        from hermes.memory.rag_retriever import RagRetriever
                        from intelligence.memory.rag_config import rag_enabled

                        if rag_enabled(config_raw):
                            RagRetriever(config=config_raw).add_to_memory(
                                ev.description,
                                {
                                    "type": EVT_MOVE_LINK,
                                    "symbol": snap.symbol,
                                    "source_id": ev.event_id,
                                    "chg_24h": snap.chg_24h,
                                },
                            )
                    except Exception:
                        pass
                log(
                    f"move_attr {snap.symbol} {snap.chg_24h:+.1f}% "
                    f"triggers={len(triggers)} top="
                    f"{triggers[0].event_type if triggers else '-'}",
                    "INFO",
                )
        except Exception as e:
            result.errors.append(f"{snap.symbol}:{e}")

    out = result.to_dict()
    out["enabled"] = True
    return out
