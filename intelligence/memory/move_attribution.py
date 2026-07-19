"""Large price moves → candidate trigger attribution (memory only).

Screen on **1h candles**. When a move is large, drill into **15m** for
impulse timing/size, then link nearby memory events (news, social, unlocks,
macro) as candidate triggers.

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
    """Primary magnitude lives in ``chg_pct`` (usually 1h); ``chg_24h`` alias for tests/compat."""

    symbol: str
    chg_24h: float = 0.0  # primary magnitude (often 1h move %; name kept for API compat)
    vol_chg_24h: float = 0.0
    price: float = 0.0
    source: str = ""
    vs_btc: float | None = None
    # Multi-timeframe detail
    screen_tf: str = "1h"
    chg_1h: float | None = None
    chg_1h_bars: int = 1  # how many 1h bars used for chg
    fine_tf: str = ""
    fine_impulse_pct: float | None = None  # strongest 15m bar in window
    fine_impulse_vol_x: float | None = None
    fine_bars_scanned: int = 0
    fine_window_minutes: int = 0
    # When the move is considered to have started (UTC). Used to prefer *preceding* news.
    move_at: str = ""  # ISO; empty → "now" at scoring time

    @property
    def chg_pct(self) -> float:
        """Primary move % used for thresholds and direction."""
        if self.chg_1h is not None:
            return float(self.chg_1h)
        return float(self.chg_24h or 0)


@dataclass
class TriggerHit:
    event_id: str
    event_type: str
    score: float
    description: str
    source: str = ""
    hours_delta: float | None = None  # signed: negative = before move, positive = after
    relation: str = ""  # before | after | unknown


@dataclass
class AttributionResult:
    moves_seen: int = 0
    moves_large: int = 0
    fine_drills: int = 0
    attributions_written: int = 0
    links_found: int = 0
    symbols: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "moves_seen": self.moves_seen,
            "moves_large": self.moves_large,
            "fine_drills": self.fine_drills,
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
        # 1h screen
        "screen_tf": str(raw.get("screen_tf") or "1h"),
        "screen_bars": int(raw.get("screen_bars", 1) or 1),  # 1 = last closed 1h bar
        "abs_chg_1h_pct": float(raw.get("abs_chg_1h_pct", raw.get("abs_chg_24h_pct", 4.0))),
        "rel_btc_1h_pct": float(raw.get("rel_btc_1h_pct", raw.get("rel_btc_pct", 3.0))),
        "ohlcv_limit_1h": int(raw.get("ohlcv_limit_1h", 36) or 36),
        # 15m drill-down when 1h is large
        "fine_tf": str(raw.get("fine_tf") or "15m"),
        "fine_bars": int(raw.get("fine_bars", 8) or 8),  # ~2h of 15m
        "fine_impulse_min_pct": float(raw.get("fine_impulse_min_pct", 1.5)),
        "ohlcv_limit_15m": int(raw.get("ohlcv_limit_15m", 48) or 48),
        # CMC fallback if OHLCV empty
        "cmc_fallback": bool(raw.get("cmc_fallback", True)),
        "abs_chg_24h_pct": float(raw.get("abs_chg_24h_pct", 12.0)),
        "rel_btc_pct": float(raw.get("rel_btc_pct", 8.0)),
        "lookback_hours": float(raw.get("lookback_hours", 72.0)),
        "max_symbols_per_cycle": int(raw.get("max_symbols_per_cycle", 30)),
        "max_triggers": int(raw.get("max_triggers", 5)),
        "index_rag": bool(raw.get("index_rag", True)),
        "min_trigger_score": float(raw.get("min_trigger_score", 0.12)),
        "prefer_idiosyncratic": bool(raw.get("prefer_idiosyncratic", True)),
        # Prefer news/events that precede the move (leading catalysts)
        "prefer_pre_move": bool(raw.get("prefer_pre_move", True)),
        "pre_window_hours": float(raw.get("pre_window_hours", 48.0)),
        "pre_move_boost": float(raw.get("pre_move_boost", 0.45)),
        "post_move_penalty": float(raw.get("post_move_penalty", 0.55)),
        "max_post_hours": float(raw.get("max_post_hours", 6.0)),  # ignore late after-noise
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
    abs_chg: float = 4.0,
    rel_btc: float = 3.0,
    prefer_idiosyncratic: bool = True,
) -> bool:
    """True if primary move (1h or 24h fallback) is large, or large vs BTC."""
    a = abs(float(snap.chg_pct or 0))
    if a >= abs_chg:
        return True
    if snap.vs_btc is not None and abs(float(snap.vs_btc)) >= rel_btc:
        return True
    _ = prefer_idiosyncratic
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


def _df_closes(df) -> list[float]:
    if df is None or getattr(df, "empty", True):
        return []
    try:
        col = "close" if "close" in df.columns else df.columns[-2]
        return [float(x) for x in df[col].tolist() if x is not None]
    except Exception:
        return []


def _df_vols(df) -> list[float]:
    if df is None or getattr(df, "empty", True):
        return []
    try:
        if "volume" not in df.columns:
            return []
        return [float(x) for x in df["volume"].tolist() if x is not None]
    except Exception:
        return []


def pct_change_from_closes(closes: list[float], bars: int = 1) -> float | None:
    """% change from close[-1-bars] → close[-1] (last bar as end)."""
    if not closes or len(closes) < bars + 1:
        return None
    a = float(closes[-(bars + 1)])
    b = float(closes[-1])
    if a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def strongest_bar_impulse(
    closes: list[float],
    volumes: list[float] | None = None,
    *,
    window: int = 8,
) -> tuple[float | None, float | None]:
    """Return (max |bar % change| with sign of that bar, vol / avg_vol)."""
    if not closes or len(closes) < 2:
        return None, None
    n = min(int(window), len(closes) - 1)
    best_pct = 0.0
    best_idx = None
    for i in range(len(closes) - n, len(closes)):
        if i <= 0:
            continue
        prev, cur = float(closes[i - 1]), float(closes[i])
        if prev <= 0:
            continue
        pct = (cur / prev - 1.0) * 100.0
        if abs(pct) >= abs(best_pct):
            best_pct = pct
            best_idx = i
    if best_idx is None:
        return None, None
    vol_x = None
    if volumes and len(volumes) == len(closes) and best_idx is not None:
        try:
            avg = sum(volumes[max(0, best_idx - n) : best_idx]) / max(
                1, min(n, best_idx)
            )
            if avg > 0:
                vol_x = float(volumes[best_idx]) / avg
        except Exception:
            vol_x = None
    return best_pct, vol_x


def _move_anchor_time(move: MoveSnap, now: datetime | None = None) -> datetime:
    """When the move is considered to start (for before/after scoring)."""
    now = now or datetime.now(timezone.utc)
    if move.move_at:
        t = _parse_ts(move.move_at)
        if t is not None:
            return t
    # 1h bar: treat move as ending "now"; start ~1 bar earlier
    bars = max(1, int(move.chg_1h_bars or 1))
    if move.screen_tf == "1h":
        return now - timedelta(hours=bars)
    if move.screen_tf == "15m":
        return now - timedelta(minutes=15 * bars)
    return now - timedelta(hours=1)


def score_trigger_for_move(
    *,
    move: MoveSnap,
    event: MarketEvent,
    now: datetime | None = None,
    lookback_hours: float = 72.0,
    prefer_pre_move: bool = True,
    pre_window_hours: float = 48.0,
    pre_move_boost: float = 0.45,
    post_move_penalty: float = 0.55,
    max_post_hours: float = 6.0,
) -> float:
    """Heuristic relevance — **strongly prefer news/events before the move**.

    Leading catalysts (unlock announced → then dump) score higher than
    after-the-fact headlines that only react to the price move.
    """
    now = now or datetime.now(timezone.utc)
    e_ts = _parse_ts(event.timestamp)
    if e_ts is None:
        return 0.0

    anchor = _move_anchor_time(move, now)
    # signed hours: negative = event before move, positive = after move
    hours_signed = (e_ts - anchor).total_seconds() / 3600.0
    hours_abs = abs(hours_signed)

    # Window: look further back before the move; only a short window after
    if hours_signed < 0:
        if hours_abs > max(lookback_hours, pre_window_hours):
            return 0.0
    else:
        if prefer_pre_move and hours_signed > max_post_hours:
            return 0.0  # ignore lagging commentary far after the move
        if hours_abs > lookback_hours:
            return 0.0

    mag = float(move.chg_pct or 0)
    score = 0.0
    et = str(event.event_type or "").lower()
    desc = str(event.description or "").lower()
    bases = {_base(s) for s in (event.symbols or [])}
    move_base = _base(move.symbol)

    if move.symbol in (event.symbols or []) or move_base in bases:
        score += 0.45
    elif bases and move_base:
        if bases <= {"BTC", "ETH"} or "BTC/USDT" in (event.symbols or []):
            score += 0.08
        else:
            return 0.0
    else:
        if move_base and move_base.lower() in desc:
            score += 0.35
        else:
            return 0.0

    # Event types that often *lead* moves get extra weight when pre-move
    leading_types = {
        "token_unlock",
        "unlock",
        "listing",
        "structure_risk",
        "macro_scheduled",
        "macro_window",
        "macro_news",
        "macro_pressure",
        "pm_mispricing",
        "cmc_trending",
        "lc_social_spike",
    }
    if et in _TRIGGER_TYPES:
        score += 0.2
    if et in ("token_unlock", "unlock", "structure_risk", "listing"):
        score += 0.2
    if et.startswith("lc_") or et.startswith("cmc_"):
        score += 0.12
    if et in ("macro_pressure", "macro_window", "macro_scheduled", "macro_news", "pm_pressure"):
        score += 0.15 if hours_signed < 0 else 0.05

    impact = float(event.impact_score or 0)
    if mag <= -abs_threshold_soft() and impact < -0.15:
        score += 0.12
    if mag >= abs_threshold_soft() and impact > 0.15:
        score += 0.12

    # --- Before vs after the move (core preference) ---
    if prefer_pre_move:
        if hours_signed <= 0:
            # Before move: boost; closer to anchor is better (not 3 months early)
            pre_h = min(pre_window_hours, max(lookback_hours, 1.0))
            # sweet spot: 0–24h before move
            if hours_abs <= 24:
                score += pre_move_boost
            elif hours_abs <= pre_h:
                score += pre_move_boost * (1.0 - (hours_abs - 24) / max(pre_h - 24, 1.0)) * 0.7
            else:
                score += pre_move_boost * 0.25
            if et in leading_types:
                score += 0.12
        else:
            # After move: heavy penalty (reactive noise)
            score *= max(0.05, 1.0 - post_move_penalty)
            score -= 0.15

    # Recency toward move anchor (not wall-clock only)
    recency = max(0.0, 1.0 - hours_abs / max(lookback_hours, 1.0))
    score *= 0.5 + 0.5 * recency

    if move.fine_impulse_pct is not None and hours_signed <= 0 and hours_abs <= 6:
        score += 0.1  # news shortly before 15m impulse

    try:
        q = f"{move.symbol} {mag:+.1f}% move preceding trigger {et}"
        from intelligence.memory.embeddings import cosine

        score += 0.12 * max(
            0.0,
            cosine(embed_text(q), event.embedding or embed_event(desc, event_type=et)),
        )
    except Exception:
        pass

    return float(max(0.0, score))


def abs_threshold_soft() -> float:
    return 3.0


def find_triggers(
    store: MemoryStore,
    move: MoveSnap,
    *,
    lookback_hours: float = 72.0,
    max_triggers: int = 5,
    min_score: float = 0.12,
    now: datetime | None = None,
    prefer_pre_move: bool = True,
    pre_window_hours: float = 48.0,
    pre_move_boost: float = 0.45,
    post_move_penalty: float = 0.55,
    max_post_hours: float = 6.0,
) -> list[TriggerHit]:
    """Rank catalysts. Prefer news/events that occurred *before* the move."""
    now = now or datetime.now(timezone.utc)
    anchor = _move_anchor_time(move, now)
    lb = float(lookback_hours)
    pre_w = float(pre_window_hours)
    search_back = max(lb, pre_w)
    if move.screen_tf == "1h" and abs(move.chg_pct) < 15:
        search_back = min(search_back, 48.0)
    since = (anchor - timedelta(hours=search_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates: list[MarketEvent] = []

    try:
        candidates.extend(
            store.list_events(symbol=move.symbol, since_iso=since, limit=100) or []
        )
    except Exception:
        pass
    try:
        for e in store.list_events(since_iso=since, limit=150) or []:
            if e.event_id in {c.event_id for c in candidates}:
                continue
            et = str(e.event_type or "")
            if (
                et in _TRIGGER_TYPES
                or et.startswith("macro")
                or et.startswith("pm_")
                or et in ("news", "listing", "macro_news", "token_unlock")
            ):
                candidates.append(e)
    except Exception:
        pass

    try:
        from intelligence.memory.retriever import similar_events

        direction = "pump" if move.chg_pct >= 0 else "dump"
        fine = ""
        if move.fine_impulse_pct is not None:
            fine = f" 15m_impulse={move.fine_impulse_pct:+.1f}%"
        q = (
            f"{move.symbol} {direction} before move {move.chg_pct:+.1f}% "
            f"{move.screen_tf}{fine} unlock listing news social macro catalyst"
        )
        for e in similar_events(q, symbol=move.symbol, k=10, store=store) or []:
            if e.event_id not in {c.event_id for c in candidates}:
                candidates.append(e)
    except Exception:
        pass

    scored: list[TriggerHit] = []
    for e in candidates:
        if str(e.event_type or "") in (EVT_PRICE_MOVE, EVT_MOVE_LINK):
            continue
        sc = score_trigger_for_move(
            move=move,
            event=e,
            now=now,
            lookback_hours=search_back,
            prefer_pre_move=prefer_pre_move,
            pre_window_hours=pre_w,
            pre_move_boost=pre_move_boost,
            post_move_penalty=post_move_penalty,
            max_post_hours=max_post_hours,
        )
        if sc < min_score:
            continue
        e_ts = _parse_ts(e.timestamp)
        hours_signed = None
        relation = "unknown"
        if e_ts:
            hours_signed = (e_ts - anchor).total_seconds() / 3600.0
            relation = "before" if hours_signed <= 0 else "after"
        scored.append(
            TriggerHit(
                event_id=e.event_id,
                event_type=str(e.event_type or ""),
                score=round(sc, 4),
                description=str(e.description or "")[:200],
                source=str(e.source or ""),
                hours_delta=round(hours_signed, 2) if hours_signed is not None else None,
                relation=relation,
            )
        )
    scored.sort(key=lambda t: (t.score, 1 if t.relation == "before" else 0), reverse=True)
    return scored[: max(1, int(max_triggers))]


def build_attribution_event(
    move: MoveSnap,
    triggers: list[TriggerHit],
    *,
    as_of: str | None = None,
) -> MarketEvent:
    as_of = as_of or utc_now_iso()
    mag = float(move.chg_pct or 0)
    direction = "pump" if mag >= 0 else "dump"
    top = triggers[0] if triggers else None
    if top:
        rel = top.relation or "?"
        h = f"{top.hours_delta:+.1f}h" if top.hours_delta is not None else ""
        link_txt = (
            f"top_trigger={top.event_type} [{rel}{h}] score={top.score:.2f} "
            f"({top.description[:70]})"
        )
    else:
        link_txt = "no strong *preceding* trigger found in lookback window"
    vs = ""
    if move.vs_btc is not None:
        vs = f" vsBTC={move.vs_btc:+.1f}pp"
    fine = ""
    if move.fine_impulse_pct is not None:
        vol_s = (
            f" volx={move.fine_impulse_vol_x:.1f}"
            if move.fine_impulse_vol_x is not None
            else ""
        )
        fine = (
            f" 15m_impulse={move.fine_impulse_pct:+.1f}%{vol_s}"
            f" (scan {move.fine_bars_scanned}x15m)"
        )
    desc = (
        f"Large {direction} {move.symbol} {mag:+.1f}% {move.screen_tf}"
        f"{vs}{fine} (src={move.source or '?'}). Attribution: {link_txt}"
    )
    impact = max(-1.0, min(1.0, mag / 25.0))
    meta = {
        "kind": "move_attribution",
        "screen_tf": move.screen_tf,
        "chg_1h": move.chg_1h,
        "chg_1h_bars": move.chg_1h_bars,
        "chg_pct": round(mag, 3),
        "chg_24h": round(float(move.chg_24h or 0), 3),  # CMC fallback field if any
        "fine_tf": move.fine_tf or None,
        "fine_impulse_pct": move.fine_impulse_pct,
        "fine_impulse_vol_x": move.fine_impulse_vol_x,
        "fine_bars_scanned": move.fine_bars_scanned,
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
                "relation": t.relation,
                "description": t.description[:120],
            }
            for t in triggers
        ],
        "preceding_triggers": [
            t.event_id for t in triggers if t.relation == "before"
        ],
        "related_event_ids": [t.event_id for t in triggers],
        "prefer_pre_move": True,
    }
    hour = as_of[:13]
    eid = make_event_id(
        "move_attr",
        f"{move.symbol}|{hour}|{move.screen_tf}|{direction}|{round(mag, 1)}",
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


def _market_service(config_raw: dict | None = None):
    try:
        from core.config import BotConfig, get_bot_config
        from services.market_service import MarketService

        cfg = get_bot_config() if config_raw is None else BotConfig(config_raw)
        return MarketService(cfg)
    except Exception:
        try:
            from services.market_service import MarketService

            return MarketService()
        except Exception:
            return None


def drill_15m(
    symbol: str,
    *,
    market=None,
    fine_bars: int = 8,
    ohlcv_limit: int = 48,
    impulse_min_pct: float = 1.5,
) -> dict[str, Any]:
    """Load 15m OHLCV and measure strongest impulse bar in recent window."""
    out: dict[str, Any] = {
        "fine_tf": "15m",
        "fine_impulse_pct": None,
        "fine_impulse_vol_x": None,
        "fine_bars_scanned": 0,
        "fine_window_minutes": int(fine_bars) * 15,
        "price": None,
    }
    if market is None:
        return out
    try:
        df = market.fetch_ohlcv(symbol, "15m", int(ohlcv_limit))
        closes = _df_closes(df)
        vols = _df_vols(df)
        if not closes:
            return out
        out["price"] = float(closes[-1])
        out["fine_bars_scanned"] = min(int(fine_bars), max(0, len(closes) - 1))
        imp, vol_x = strongest_bar_impulse(
            closes, vols or None, window=int(fine_bars)
        )
        if imp is not None and abs(imp) >= float(impulse_min_pct):
            out["fine_impulse_pct"] = round(float(imp), 3)
            if vol_x is not None:
                out["fine_impulse_vol_x"] = round(float(vol_x), 3)
        elif imp is not None:
            # still record weak impulse for context
            out["fine_impulse_pct"] = round(float(imp), 3)
            if vol_x is not None:
                out["fine_impulse_vol_x"] = round(float(vol_x), 3)
    except Exception as e:
        log(f"move_attr 15m drill {symbol}: {e}", "DEBUG")
    return out


def fetch_move_snaps_1h(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
    cfg: dict | None = None,
) -> list[MoveSnap]:
    """Screen universe on 1h candles; do not drill 15m here (only on large)."""
    cfg = cfg or move_attribution_config(config_raw)
    out: list[MoveSnap] = []
    market = _market_service(config_raw)
    if market is None:
        return out

    screen_bars = max(1, int(cfg.get("screen_bars") or 1))
    limit = max(int(cfg.get("ohlcv_limit_1h") or 36), screen_bars + 5)
    btc_chg = None
    try:
        btc_df = market.fetch_ohlcv("BTC/USDT", "1h", limit)
        btc_closes = _df_closes(btc_df)
        btc_chg = pct_change_from_closes(btc_closes, screen_bars)
    except Exception:
        btc_chg = None

    for sym in symbols:
        try:
            df = market.fetch_ohlcv(sym, "1h", limit)
            closes = _df_closes(df)
            chg = pct_change_from_closes(closes, screen_bars)
            if chg is None:
                continue
            vs = None
            if btc_chg is not None:
                vs = float(chg) - float(btc_chg)
            price = float(closes[-1]) if closes else 0.0
            out.append(
                MoveSnap(
                    symbol=sym if "/" in sym else f"{sym}/USDT",
                    chg_24h=float(chg),  # primary mag for is_large_move / tests
                    chg_1h=float(chg),
                    chg_1h_bars=screen_bars,
                    price=price,
                    source="ohlcv_1h",
                    vs_btc=vs,
                    screen_tf="1h",
                )
            )
        except Exception as e:
            log(f"move_attr 1h {sym}: {e}", "DEBUG")
    return out


def fetch_move_snaps_cmc_fallback(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
) -> list[MoveSnap]:
    """24h CMC quotes when 1h OHLCV unavailable."""
    out: list[MoveSnap] = []
    if not symbols:
        return out
    btc_chg = None
    try:
        from intelligence.memory.coin_facts_cmc_pro import (
            fetch_quotes_for_symbols,
            parse_quote_snap,
        )

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
                    screen_tf="24h",
                )
            )
    except Exception as e:
        log(f"move_attribution quotes: {e}", "DEBUG")
    return out


def fetch_move_snaps(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
) -> list[MoveSnap]:
    """Prefer 1h OHLCV screen; optional CMC 24h fallback for missing symbols."""
    cfg = move_attribution_config(config_raw)
    snaps = fetch_move_snaps_1h(symbols, config_raw=config_raw, cfg=cfg)
    have = {s.symbol for s in snaps}
    missing = [s for s in symbols if (s if "/" in s else f"{s}/USDT") not in have]
    if missing and cfg.get("cmc_fallback", True):
        for s in fetch_move_snaps_cmc_fallback(missing, config_raw=config_raw):
            if s.symbol not in have:
                snaps.append(s)
                have.add(s.symbol)
    return snaps


def apply_15m_drill(
    snap: MoveSnap,
    *,
    config_raw: dict | None = None,
    cfg: dict | None = None,
    market=None,
) -> MoveSnap:
    """Mutate/return snap with 15m impulse fields filled."""
    cfg = cfg or move_attribution_config(config_raw)
    market = market or _market_service(config_raw)
    detail = drill_15m(
        snap.symbol,
        market=market,
        fine_bars=int(cfg.get("fine_bars") or 8),
        ohlcv_limit=int(cfg.get("ohlcv_limit_15m") or 48),
        impulse_min_pct=float(cfg.get("fine_impulse_min_pct") or 1.5),
    )
    snap.fine_tf = str(detail.get("fine_tf") or "15m")
    snap.fine_impulse_pct = detail.get("fine_impulse_pct")
    snap.fine_impulse_vol_x = detail.get("fine_impulse_vol_x")
    snap.fine_bars_scanned = int(detail.get("fine_bars_scanned") or 0)
    snap.fine_window_minutes = int(detail.get("fine_window_minutes") or 0)
    if detail.get("price") and not snap.price:
        snap.price = float(detail["price"])
    return snap


def sync_move_attribution(
    store: MemoryStore | None = None,
    *,
    config_raw: dict | None = None,
    symbols: list[str] | None = None,
    moves: list[MoveSnap] | None = None,
) -> dict[str, Any]:
    """Detect large 1h moves, drill 15m, write attribution events."""
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

    # Thresholds: 1h path vs 24h CMC fallback
    market = _market_service(config_raw)

    for snap in snaps:
        if snap.screen_tf == "1h" or snap.chg_1h is not None:
            abs_thr = float(cfg["abs_chg_1h_pct"])
            rel_thr = float(cfg["rel_btc_1h_pct"])
        else:
            abs_thr = float(cfg["abs_chg_24h_pct"])
            rel_thr = float(cfg["rel_btc_pct"])

        if not is_large_move(
            snap,
            abs_chg=abs_thr,
            rel_btc=rel_thr,
            prefer_idiosyncratic=bool(cfg["prefer_idiosyncratic"]),
        ):
            continue

        # Drill 15m only on large 1h (or large CMC if no 1h)
        if moves is None or not snap.fine_tf:
            try:
                apply_15m_drill(snap, config_raw=config_raw, cfg=cfg, market=market)
                if snap.fine_impulse_pct is not None:
                    result.fine_drills += 1
            except Exception as e:
                result.errors.append(f"drill:{snap.symbol}:{e}")

        result.moves_large += 1
        result.symbols.append(snap.symbol)
        try:
            triggers = find_triggers(
                store,
                snap,
                lookback_hours=float(cfg["lookback_hours"]),
                max_triggers=int(cfg["max_triggers"]),
                min_score=float(cfg["min_trigger_score"]),
                prefer_pre_move=bool(cfg.get("prefer_pre_move", True)),
                pre_window_hours=float(cfg.get("pre_window_hours", 48)),
                pre_move_boost=float(cfg.get("pre_move_boost", 0.45)),
                post_move_penalty=float(cfg.get("post_move_penalty", 0.55)),
                max_post_hours=float(cfg.get("max_post_hours", 6)),
            )
            result.links_found += len(triggers)
            ev = build_attribution_event(snap, triggers)
            if store.get_event(ev.event_id):
                continue
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
                                    "chg_1h": snap.chg_1h,
                                    "fine_impulse_pct": snap.fine_impulse_pct,
                                    "screen_tf": snap.screen_tf,
                                },
                            )
                    except Exception:
                        pass
                log(
                    f"move_attr {snap.symbol} {snap.chg_pct:+.1f}% {snap.screen_tf} "
                    f"15m_imp={snap.fine_impulse_pct} "
                    f"triggers={len(triggers)} top="
                    f"{triggers[0].event_type if triggers else '-'}",
                    "INFO",
                )
        except Exception as e:
            result.errors.append(f"{snap.symbol}:{e}")

    out = result.to_dict()
    out["enabled"] = True
    return out
