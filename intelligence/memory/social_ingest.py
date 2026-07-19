"""CMC + LunarCrush → Trading Memory (Epic #42).

Reads bot artifacts (JSON logs + optional shared Mongo feed). Writes only memory_*.
Never touches orders/positions. Social alone never forces BUY / soft_block.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from intelligence.memory.embeddings import embed_event
from intelligence.memory.event_ingest import make_event_id
from intelligence.memory.models import CoinProfile, MarketEvent, TradeMemory, utc_now_iso
from intelligence.memory.store import MemoryStore, resolve_memory_scope
from logger import log

# Event types (SM-0 freeze)
EVT_CMC_SOCIAL = "cmc_social"
EVT_CMC_TRENDING = "cmc_trending"
EVT_CMC_QUOTE_EXTREME = "cmc_quote_extreme"
EVT_LC_SPIKE = "lc_social_spike"
EVT_LC_FADE = "lc_social_fade"
EVT_LC_SENTIMENT = "lc_sentiment_extreme"

COL_SOCIAL_FEED = "memory_social_feed"  # shared bot→Hermes bridge (memory_* only)


def normalize_symbol(coin: str | None) -> str:
    raw = str(coin or "").strip().upper().replace("-", "/").replace("_", "/")
    if not raw:
        return ""
    if raw.endswith("/USDT"):
        return raw
    if raw.endswith("USDT") and "/" not in raw:
        base = raw[:-4]
        return f"{base}/USDT" if base else ""
    if "/" not in raw:
        return f"{raw}/USDT"
    return raw


def clamp_impact(v: float) -> float:
    return max(-1.0, min(1.0, float(v)))


def float_or(value: Any, default: float) -> float:
    """Parse float; only fall back to default when value is None/missing/invalid.

    Important: 0 and 0.0 are valid (e.g. LC sentiment=0) — never use `x or default`.
    """
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def impact_from_action(action: str, confidence: float) -> float:
    """Map BUY/SELL/HOLD + conf% → impact in [-1, 1]."""
    a = (action or "HOLD").upper()
    conf = max(0.0, min(100.0, float(confidence or 0))) / 100.0
    if a == "BUY":
        return clamp_impact(0.15 + 0.35 * conf)
    if a == "SELL":
        return clamp_impact(-(0.15 + 0.40 * conf))
    return 0.0


def is_quotes_fallback(post: dict[str, Any]) -> bool:
    pid = str(post.get("post_id") or "")
    if pid.startswith("cmc_quote_") or pid.startswith("cmc_mkt_listings_"):
        return True
    if post.get("quotes_fallback") is True:
        return True
    rat = str(post.get("rationale") or "").lower()
    if "cmc market data" in rat and "neutral" in rat and float(post.get("confidence") or 0) <= 50:
        # pure quote-style noise often looks like this
        if pid.startswith("cmc_quote_"):
            return True
    return pid.startswith("cmc_quote_")


def social_config(config: dict | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from core.config import get_bot_config

            config = get_bot_config().raw
        except Exception:
            config = {}
    mem = (config or {}).get("memory") or {}
    return dict(mem.get("social") or {})


def social_enabled(config: dict | None = None) -> bool:
    if os.environ.get("MEMORY_SOCIAL", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    soc = social_config(config)
    cmc = soc.get("cmc") or {}
    lc = soc.get("lunarcrush") or {}
    if os.environ.get("MEMORY_SOCIAL_CMC", "").strip() in ("0", "false"):
        cmc = {**cmc, "enabled": False}
    if os.environ.get("MEMORY_SOCIAL_LC", "").strip() in ("0", "false"):
        lc = {**lc, "enabled": False}
    return bool(cmc.get("enabled", True) or lc.get("enabled", True))


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


def _within_lookback(ts_raw: Any, hours: float) -> bool:
    if hours <= 0:
        return True
    dt = _parse_ts(ts_raw)
    if dt is None:
        return True  # keep unknown ts
    return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)


def ingest_social_signal(
    *,
    source: str,
    event_type: str,
    symbol: str,
    impact: float,
    description: str,
    metadata: dict[str, Any] | None = None,
    stable_key: str = "",
    timestamp: str | None = None,
    store: MemoryStore | None = None,
) -> MarketEvent | None:
    """Shared primitive: normalize → dedupe → MarketEvent."""
    store = store or MemoryStore()
    sym = normalize_symbol(symbol)
    if not sym or not description:
        return None
    key = stable_key or f"{event_type}|{sym}|{description[:80]}"
    eid = make_event_id(source, key)
    existing = store.get_event(eid)
    if existing:
        return existing
    desc = (description or "")[:500]
    et = (event_type or "social").strip() or "social"
    ev = MarketEvent(
        event_id=eid,
        timestamp=timestamp or utc_now_iso(),
        event_type=et,
        symbols=[sym],
        impact_score=clamp_impact(impact),
        description=desc,
        source=source,
        metadata=dict(metadata or {}),
        embedding=embed_event(desc, event_type=et),
    )
    store.upsert_event(ev)
    return ev


def append_social_feed(entry: dict[str, Any]) -> bool:
    """Bot dual-write: persist social row for Hermes (memory_* only, fail-open)."""
    try:
        from storage.mongo_client import get_database

        doc = dict(entry)
        doc.setdefault("ingested_at", utc_now_iso())
        sid = str(doc.get("post_id") or doc.get("signal_id") or "")
        source = str(doc.get("source") or "social")
        _id = f"{source}:{sid}" if sid else f"{source}:{hash(str(doc)) & 0xFFFFFFFF:x}"
        doc["_id"] = _id
        get_database()[COL_SOCIAL_FEED].replace_one({"_id": _id}, doc, upsert=True)
        return True
    except Exception as e:
        log(f"memory social feed write skipped: {e}", "DEBUG")
        return False


def load_social_feed(
    *,
    source: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    try:
        from storage.mongo_client import get_database

        q: dict[str, Any] = {}
        if source:
            q["source"] = source
        cur = (
            get_database()[COL_SOCIAL_FEED]
            .find(q)
            .sort("timestamp", -1)
            .limit(int(limit))
        )
        return list(cur)
    except Exception as e:
        log(f"memory social feed read skipped: {e}", "DEBUG")
        return []


def _load_cmc_posts_combined() -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        from data_manager import load_cmc_posts

        for p in load_cmc_posts().get("posts") or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("post_id") or "")
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            posts.append(p)
    except Exception as e:
        log(f"load_cmc_posts failed: {e}", "DEBUG")
    for p in load_social_feed(source="cmc", limit=200):
        pid = str(p.get("post_id") or "")
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        posts.append(p)
    return posts


def _load_lc_signals_combined() -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        from data_manager import load_lc_signals

        for s in load_lc_signals().get("signals") or []:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("signal_id") or "")
            if sid and sid in seen:
                continue
            if sid:
                seen.add(sid)
            signals.append(s)
    except Exception as e:
        log(f"load_lc_signals failed: {e}", "DEBUG")
    for s in load_social_feed(source="lc", limit=200):
        sid = str(s.get("signal_id") or "")
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        signals.append(s)
    return signals


def _merge_profile_features(
    store: MemoryStore,
    symbol: str,
    *,
    cmc: dict[str, Any] | None = None,
    lc: dict[str, Any] | None = None,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
) -> bool:
    """Update features only — never invent soft_block from social alone."""
    scope = resolve_memory_scope(ledger_scope)
    prof = store.get_profile(symbol, ledger_scope=scope, tenant_id=tenant_id)
    if not prof:
        prof = CoinProfile(
            symbol=symbol,
            ledger_scope=scope,
            tenant_id=tenant_id,
            size_bias=1.0,
            entry_bias="neutral",
            rationale="social features init",
        )
    features = dict(prof.features or {})
    if cmc:
        features["cmc"] = {**(features.get("cmc") or {}), **cmc}
    if lc:
        features["lc"] = {**(features.get("lc") or {}), **lc}
    # short summary for risk audit
    parts = []
    if features.get("cmc"):
        c = features["cmc"]
        parts.append(
            f"cmc:{c.get('last_action', '?')}@{c.get('last_conf', 0)}"
        )
    if features.get("lc"):
        l = features["lc"]
        parts.append(
            f"lc:g{l.get('galaxy_score', '?')}/s{l.get('sentiment', '?')}"
        )
    if parts:
        features["social_summary"] = " ".join(parts)[:120]
    prof.features = features
    return store.upsert_profile(prof)


def sync_cmc_memory(
    store: MemoryStore | None = None,
    *,
    config: dict | None = None,
    posts: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """CMC posts/trending → MarketEvent + features.cmc."""
    store = store or MemoryStore()
    soc = social_config(config)
    cmc_cfg = dict(soc.get("cmc") or {})
    if not cmc_cfg.get("enabled", True):
        return {"cmc_events": 0, "cmc_features": 0, "cmc_skipped": 0}
    if os.environ.get("MEMORY_SOCIAL_CMC", "").strip() in ("0", "false"):
        return {"cmc_events": 0, "cmc_features": 0, "cmc_skipped": 0}

    min_conf = float(cmc_cfg.get("min_confidence", 60))
    include_qf = bool(cmc_cfg.get("include_quotes_fallback", False))
    lookback = float(cmc_cfg.get("lookback_hours", 72))
    max_ev = int(cmc_cfg.get("max_events_per_cycle", 40))
    quote_extreme = bool(cmc_cfg.get("quote_extreme", False))
    quote_min = float(cmc_cfg.get("quote_chg_pct_min", 12))

    rows = posts if posts is not None else _load_cmc_posts_combined()
    events_n = 0
    skipped = 0
    by_coin: dict[str, list[dict]] = defaultdict(list)

    for post in rows:
        if not _within_lookback(post.get("timestamp"), lookback):
            skipped += 1
            continue
        qf = is_quotes_fallback(post)
        if qf and not include_qf and not quote_extreme:
            skipped += 1
            continue
        coin = post.get("coin") or ""
        sym = normalize_symbol(coin)
        if not sym:
            skipped += 1
            continue
        conf = float(post.get("confidence") or 0)
        action = str(post.get("action") or "HOLD").upper()
        pid = str(post.get("post_id") or "")
        by_coin[sym].append(post)

        # trending style posts
        if pid.startswith("cmc_mkt_trend_") or "trend" in pid:
            if events_n >= max_ev:
                continue
            if conf < min_conf and action == "HOLD":
                skipped += 1
                continue
            impact = impact_from_action(action, conf) if action != "HOLD" else 0.2
            if ingest_social_signal(
                source="cmc",
                event_type=EVT_CMC_TRENDING,
                symbol=sym,
                impact=impact,
                description=(
                    f"CMC trending {sym} {action} conf={conf:.0f} "
                    f"{(post.get('rationale') or '')[:120]}"
                ),
                metadata={
                    "post_id": pid,
                    "action": action,
                    "confidence": conf,
                    "quotes_fallback": qf,
                },
                stable_key=f"trend|{pid or sym}|{str(post.get('timestamp') or '')[:13]}",
                timestamp=str(post.get("timestamp") or "") or None,
                store=store,
            ):
                events_n += 1
            continue

        # optional quote extremes
        if qf and quote_extreme:
            # parse % from rationale if present
            rat = str(post.get("rationale") or "")
            chg = 0.0
            import re

            m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", rat)
            if m:
                chg = float(m.group(1))
            if abs(chg) < quote_min:
                skipped += 1
                continue
            if events_n >= max_ev:
                continue
            impact = clamp_impact(chg / 50.0)  # 25% → 0.5
            if ingest_social_signal(
                source="cmc",
                event_type=EVT_CMC_QUOTE_EXTREME,
                symbol=sym,
                impact=impact,
                description=f"CMC quote extreme {sym} {chg:+.1f}%",
                metadata={"post_id": pid, "chg_pct": chg, "quotes_fallback": True},
                stable_key=f"quote|{pid}",
                timestamp=str(post.get("timestamp") or "") or None,
                store=store,
            ):
                events_n += 1
            continue

        if qf and not include_qf:
            skipped += 1
            continue

        if conf < min_conf and action == "HOLD":
            skipped += 1
            continue
        if action == "HOLD" and conf < min_conf:
            skipped += 1
            continue
        if events_n >= max_ev:
            continue

        impact = impact_from_action(action, conf)
        if impact == 0.0 and action == "HOLD":
            skipped += 1
            continue

        if ingest_social_signal(
            source="cmc",
            event_type=EVT_CMC_SOCIAL,
            symbol=sym,
            impact=impact,
            description=(
                f"CMC social {sym} {action} conf={conf:.0f} "
                f"{(post.get('rationale') or '')[:140]}"
            ),
            metadata={
                "post_id": pid,
                "action": action,
                "confidence": conf,
                "votes_bullish": post.get("votes_bullish"),
                "votes_bearish": post.get("votes_bearish"),
                "quotes_fallback": False,
            },
            stable_key=f"social|{pid or (sym + str(post.get('timestamp') or '')[:16])}",
            timestamp=str(post.get("timestamp") or "") or None,
            store=store,
        ):
            events_n += 1

    features_n = 0
    for sym, plist in by_coin.items():
        buys = [p for p in plist if str(p.get("action") or "").upper() == "BUY"]
        sells = [p for p in plist if str(p.get("action") or "").upper() == "SELL"]
        last = sorted(plist, key=lambda x: str(x.get("timestamp") or ""))[-1]
        bull_ratio = (len(buys) / len(plist)) if plist else 0.0
        feat = {
            "last_action": str(last.get("action") or "HOLD").upper(),
            "last_conf": float(last.get("confidence") or 0),
            "signal_count_7d": len(plist),
            "bullish_ratio_7d": round(bull_ratio, 3),
            "sell_signal_count_7d": len(sells),
            "as_of": utc_now_iso(),
        }
        if _merge_profile_features(store, sym, cmc=feat):
            features_n += 1

    # Trending overlay (optional)
    if cmc_cfg.get("trending", True):
        try:
            from data_manager import load_cmc_trending_overlay

            for coin in (load_cmc_trending_overlay().get("coins") or [])[:20]:
                if not isinstance(coin, dict):
                    continue
                sym = normalize_symbol(coin.get("symbol") or coin.get("coin"))
                if not sym or events_n >= max_ev:
                    continue
                rank = coin.get("rank") or coin.get("trending_rank") or 0
                if ingest_social_signal(
                    source="cmc",
                    event_type=EVT_CMC_TRENDING,
                    symbol=sym,
                    impact=0.25,
                    description=f"CMC trending overlay {sym} rank={rank}",
                    metadata={"trending_rank": rank, "overlay": True},
                    stable_key=f"overlay|{sym}|{utc_now_iso()[:10]}",
                    store=store,
                ):
                    events_n += 1
                tr_feat = {"trending_rank": rank, "as_of": utc_now_iso()}
                if _merge_profile_features(store, sym, cmc=tr_feat):
                    features_n += 1
        except Exception as e:
            log(f"cmc trending overlay memory: {e}", "DEBUG")

    log(f"memory social cmc: events={events_n} features={features_n} skipped={skipped}", "INFO")
    return {"cmc_events": events_n, "cmc_features": features_n, "cmc_skipped": skipped}


def sync_lc_memory(
    store: MemoryStore | None = None,
    *,
    config: dict | None = None,
    signals: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """LunarCrush signals → MarketEvent + features.lc."""
    store = store or MemoryStore()
    soc = social_config(config)
    lc_cfg = dict(soc.get("lunarcrush") or {})
    if not lc_cfg.get("enabled", True):
        return {"lc_events": 0, "lc_features": 0, "lc_skipped": 0}
    if os.environ.get("MEMORY_SOCIAL_LC", "").strip() in ("0", "false"):
        return {"lc_events": 0, "lc_features": 0, "lc_skipped": 0}

    min_conf = float(lc_cfg.get("min_confidence", 55))
    lookback = float(lc_cfg.get("lookback_hours", 72))
    max_ev = int(lc_cfg.get("max_events_per_cycle", 40))
    g_delta_min = float(lc_cfg.get("min_galaxy_delta", 8))
    sent_lo = float(lc_cfg.get("sentiment_extreme_low", 35))
    sent_hi = float(lc_cfg.get("sentiment_extreme_high", 75))

    rows = signals if signals is not None else _load_lc_signals_combined()
    events_n = 0
    skipped = 0
    by_coin: dict[str, list[dict]] = defaultdict(list)

    for sig in rows:
        if not _within_lookback(sig.get("timestamp"), lookback):
            skipped += 1
            continue
        sym = normalize_symbol(sig.get("coin"))
        if not sym:
            skipped += 1
            continue
        by_coin[sym].append(sig)
        conf = float_or(sig.get("confidence"), 0.0)
        action = str(sig.get("action") or "HOLD").upper()
        galaxy = float_or(sig.get("galaxy_score"), 0.0)
        sentiment = float_or(sig.get("sentiment"), 50.0)
        try:
            alt_rank = int(sig["alt_rank"]) if sig.get("alt_rank") is not None else 0
        except (TypeError, ValueError):
            alt_rank = 0
        sid = str(sig.get("signal_id") or "")
        rationale = str(sig.get("rationale") or "")

        # parse galaxy delta from rationale e.g. "Galaxy 56 (+21)"
        g_delta = 0.0
        import re

        m = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", rationale)
        if m:
            try:
                g_delta = float(m.group(1))
            except ValueError:
                g_delta = 0.0
        if g_delta == 0.0 and sig.get("galaxy_delta") is not None:
            g_delta = float_or(sig.get("galaxy_delta"), 0.0)

        if events_n >= max_ev:
            continue

        # spike
        if g_delta >= g_delta_min and (sentiment >= 55 or conf >= min_conf):
            if ingest_social_signal(
                source="lc",
                event_type=EVT_LC_SPIKE,
                symbol=sym,
                impact=clamp_impact(0.2 + min(0.3, g_delta / 40.0)),
                description=(
                    f"LC spike {sym} galaxy={galaxy:.0f} Δ={g_delta:+.0f} "
                    f"sent={sentiment:.0f} {action}"
                ),
                metadata={
                    "signal_id": sid,
                    "galaxy_score": galaxy,
                    "galaxy_delta": g_delta,
                    "sentiment": sentiment,
                    "alt_rank": alt_rank,
                    "action": action,
                    "confidence": conf,
                },
                stable_key=f"spike|{sid or sym}|{str(sig.get('timestamp') or '')[:13]}",
                timestamp=str(sig.get("timestamp") or "") or None,
                store=store,
            ):
                events_n += 1
        # fade
        elif g_delta <= -g_delta_min:
            if ingest_social_signal(
                source="lc",
                event_type=EVT_LC_FADE,
                symbol=sym,
                impact=clamp_impact(-(0.15 + min(0.25, abs(g_delta) / 40.0))),
                description=(
                    f"LC fade {sym} galaxy={galaxy:.0f} Δ={g_delta:+.0f} sent={sentiment:.0f}"
                ),
                metadata={
                    "signal_id": sid,
                    "galaxy_score": galaxy,
                    "galaxy_delta": g_delta,
                    "sentiment": sentiment,
                    "alt_rank": alt_rank,
                },
                stable_key=f"fade|{sid or sym}|{str(sig.get('timestamp') or '')[:13]}",
                timestamp=str(sig.get("timestamp") or "") or None,
                store=store,
            ):
                events_n += 1
        # sentiment extreme
        if sentiment <= sent_lo or sentiment >= sent_hi:
            if events_n >= max_ev:
                continue
            impact = -0.3 if sentiment <= sent_lo else 0.3
            if ingest_social_signal(
                source="lc",
                event_type=EVT_LC_SENTIMENT,
                symbol=sym,
                impact=impact,
                description=f"LC sentiment extreme {sym} sent={sentiment:.0f}",
                metadata={
                    "signal_id": sid,
                    "sentiment": sentiment,
                    "galaxy_score": galaxy,
                },
                stable_key=f"sent|{sid or sym}|{str(sig.get('timestamp') or '')[:10]}",
                timestamp=str(sig.get("timestamp") or "") or None,
                store=store,
            ):
                events_n += 1
        # action-based social if strong BUY/SELL
        elif action in ("BUY", "SELL") and conf >= min_conf:
            if events_n >= max_ev:
                continue
            if ingest_social_signal(
                source="lc",
                event_type=EVT_LC_SPIKE if action == "BUY" else EVT_LC_FADE,
                symbol=sym,
                impact=impact_from_action(action, conf),
                description=f"LC signal {sym} {action} conf={conf:.0f} {rationale[:100]}",
                metadata={
                    "signal_id": sid,
                    "action": action,
                    "confidence": conf,
                    "galaxy_score": galaxy,
                    "sentiment": sentiment,
                },
                stable_key=f"act|{sid or (sym + action)}",
                timestamp=str(sig.get("timestamp") or "") or None,
                store=store,
            ):
                events_n += 1
        else:
            skipped += 1

    features_n = 0
    for sym, slist in by_coin.items():
        last = sorted(slist, key=lambda x: str(x.get("timestamp") or ""))[-1]
        rat = str(last.get("rationale") or "")
        g_delta = float(last.get("galaxy_delta") or 0)
        import re

        m = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", rat)
        if m:
            try:
                g_delta = float(m.group(1))
            except ValueError:
                pass
        try:
            last_alt = int(last["alt_rank"]) if last.get("alt_rank") is not None else 0
        except (TypeError, ValueError):
            last_alt = 0
        feat = {
            "galaxy_score": float_or(last.get("galaxy_score"), 0.0),
            "galaxy_delta": g_delta,
            "alt_rank": last_alt,
            "sentiment": float_or(last.get("sentiment"), 50.0),
            "last_action": str(last.get("action") or "HOLD").upper(),
            "last_conf": float_or(last.get("confidence"), 0.0),
            "as_of": utc_now_iso(),
        }
        if _merge_profile_features(store, sym, lc=feat):
            features_n += 1

    log(f"memory social lc: events={events_n} features={features_n} skipped={skipped}", "INFO")
    return {"lc_events": events_n, "lc_features": features_n, "lc_skipped": skipped}


def sync_social_memory(
    store: MemoryStore | None = None,
    *,
    config: dict | None = None,
) -> dict[str, int]:
    """Full CMC + LC sync for Hermes cycle."""
    store = store or MemoryStore()
    if not social_enabled(config):
        return {
            "cmc_events": 0,
            "lc_events": 0,
            "cmc_features": 0,
            "lc_features": 0,
            "joined_trades": 0,
        }
    out: dict[str, int] = {}
    try:
        out.update(sync_cmc_memory(store, config=config))
    except Exception as e:
        log(f"sync_cmc_memory failed: {e}", "WARNING")
        out.setdefault("cmc_events", 0)
        out.setdefault("cmc_features", 0)
    try:
        out.update(sync_lc_memory(store, config=config))
    except Exception as e:
        log(f"sync_lc_memory failed: {e}", "WARNING")
        out.setdefault("lc_events", 0)
        out.setdefault("lc_features", 0)
    try:
        out["joined_trades"] = join_social_events_to_trades(store, config=config)
    except Exception as e:
        log(f"join social events failed: {e}", "WARNING")
        out["joined_trades"] = 0
    log(f"memory social: {out}", "INFO")
    return out


def _symbol_bases(symbols: list[str] | None) -> set[str]:
    out: set[str] = set()
    for s in symbols or []:
        n = normalize_symbol(s)
        if not n:
            continue
        out.add(n)
        out.add(n.split("/")[0])
    return out


def join_social_events_to_trades(
    store: MemoryStore | None = None,
    *,
    config: dict | None = None,
    tenant_id: str = "default",
) -> int:
    """Attach related_event_ids for social events within join window.

    P4: delayed join — default window 48h (config join_window_hours /
    join_window_hours_delayed). Matches on normalized base symbols; uses
    entry_time or exit_time. Also stamps event.metadata.joined_trade_ids
    for reverse lookup (fail-open).
    """
    store = store or MemoryStore()
    soc = social_config(config)
    window_h = float(
        soc.get("join_window_hours_delayed")
        or soc.get("join_window_hours")
        or 48
    )
    window = timedelta(hours=max(1.0, window_h))
    social_types = {
        EVT_CMC_SOCIAL,
        EVT_CMC_TRENDING,
        EVT_CMC_QUOTE_EXTREME,
        EVT_LC_SPIKE,
        EVT_LC_FADE,
        EVT_LC_SENTIMENT,
    }
    events = [
        e
        for e in store.list_events(limit=400)
        if e.event_type in social_types
    ]
    if not events:
        return 0

    # Index events by base symbol for O(trades * events_for_sym)
    by_base: dict[str, list] = defaultdict(list)
    for ev in events:
        bases = _symbol_bases(ev.symbols)
        if not bases:
            by_base["*"].append(ev)
            continue
        for b in bases:
            by_base[b].append(ev)

    n = 0
    for trade in store.list_trades(tenant_id=tenant_id, limit=500):
        t_ts = _parse_ts(trade.entry_time or trade.exit_time)
        if t_ts is None:
            continue
        trade_sym = normalize_symbol(trade.symbol)
        base = trade_sym.split("/")[0] if trade_sym else ""
        candidates = list(by_base.get(trade_sym, [])) + list(by_base.get(base, []))
        # de-dupe candidates by event_id
        seen_e: set[str] = set()
        uniq = []
        for ev in candidates:
            if ev.event_id in seen_e:
                continue
            seen_e.add(ev.event_id)
            uniq.append(ev)

        related = list(trade.related_event_ids or [])
        before = len(related)
        newly: list = []
        for ev in uniq:
            e_ts = _parse_ts(ev.timestamp)
            if e_ts is None:
                continue
            if abs((e_ts - t_ts).total_seconds()) <= window.total_seconds():
                if ev.event_id not in related:
                    related.append(ev.event_id)
                    newly.append(ev)
        if len(related) > before:
            trade.related_event_ids = related[:30]
            if store.upsert_trade(trade):
                n += 1
                # reverse stamp on events (best-effort)
                for ev in newly:
                    try:
                        meta = dict(ev.metadata or {})
                        joined = list(meta.get("joined_trade_ids") or [])
                        if trade.trade_id not in joined:
                            joined.append(trade.trade_id)
                        meta["joined_trade_ids"] = joined[:30]
                        meta["last_joined_at"] = utc_now_iso()
                        ev.metadata = meta
                        store.upsert_event(ev)
                    except Exception:
                        pass
    return n


def reflect_social(
    store: MemoryStore | None = None,
    *,
    config: dict | None = None,
    tenant_id: str = "default",
    min_samples: int = 3,
) -> dict[str, int]:
    """Rule-based lessons from social events × sell outcomes. Gated size_bias only."""
    store = store or MemoryStore()
    soc = social_config(config)
    if not soc.get("reflect_social", True):
        return {"social_lessons": 0, "social_profile_updates": 0}

    import hashlib

    from intelligence.memory.embeddings import embed_text
    from intelligence.memory.models import Lesson

    lessons = 0
    profile_updates = 0
    scope = resolve_memory_scope()
    trades = store.list_trades(tenant_id=tenant_id, limit=400)
    events = store.list_events(limit=200)
    social_ev = [
        e
        for e in events
        if e.event_type
        in (
            EVT_CMC_SOCIAL,
            EVT_CMC_TRENDING,
            EVT_LC_SPIKE,
            EVT_LC_FADE,
        )
    ]

    by_sym: dict[str, list[TradeMemory]] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    for symbol, tlist in by_sym.items():
        sells = [t for t in tlist if t.direction == "sell" and t.pnl_usdt is not None]
        if len(sells) < min_samples:
            continue
        total_pnl = sum(float(t.pnl_usdt or 0) for t in sells)
        # CMC bullish events near this symbol
        bullish = [
            e
            for e in social_ev
            if e.impact_score > 0.15
            and any(symbol.split("/")[0] in s for s in (e.symbols or []))
            and e.event_type in (EVT_CMC_SOCIAL, EVT_CMC_TRENDING, EVT_LC_SPIKE)
        ]
        fades = [
            e
            for e in social_ev
            if e.event_type == EVT_LC_FADE
            and any(symbol.split("/")[0] in s for s in (e.symbols or []))
        ]
        if bullish and total_pnl < 0 and len(bullish) >= 2:
            text = (
                f"{symbol}: social hype fade — {len(bullish)} bullish CMC/LC events "
                f"but sell PnL {total_pnl:.1f} USDT (n={len(sells)}). Prefer smaller size."
            )
            lid = hashlib.sha256(f"hype|{symbol}|{text[:60]}".encode()).hexdigest()[:16]
            if store.upsert_lesson(
                Lesson(
                    lesson_id=f"les_{lid}",
                    text=text,
                    confidence=0.55,
                    tags=["hype_fade", "social", symbol.split("/")[0].lower()],
                    symbols=[symbol],
                    sample_n=len(sells),
                    embedding=embed_text(text),
                    tenant_id=tenant_id,
                )
            ):
                lessons += 1
            prof = store.get_profile(symbol, ledger_scope=scope, tenant_id=tenant_id)
            if prof and len(sells) >= min_samples:
                changed = False
                if prof.size_bias > 0.7:
                    prof.size_bias = 0.7
                    changed = True
                stamp = "reflect hype_fade"
                if stamp not in (prof.rationale or ""):
                    prof.rationale = f"{(prof.rationale or '').strip()} | {stamp}"[:200]
                    changed = True
                # never soft_block from social alone without weak history already set
                if changed and store.upsert_profile(prof):
                    profile_updates += 1
        if fades and total_pnl < 0:
            text = (
                f"{symbol}: LC social_fade near losses "
                f"(n_fade={len(fades)}, sell_pnl={total_pnl:.1f})."
            )
            lid = hashlib.sha256(f"fade|{symbol}|{text[:60]}".encode()).hexdigest()[:16]
            if store.upsert_lesson(
                Lesson(
                    lesson_id=f"les_{lid}",
                    text=text,
                    confidence=0.5,
                    tags=["social_fade", "lc", symbol.split("/")[0].lower()],
                    symbols=[symbol],
                    sample_n=len(sells),
                    embedding=embed_text(text),
                    tenant_id=tenant_id,
                )
            ):
                lessons += 1

    log(
        f"memory reflect social: lessons={lessons} profile_updates={profile_updates}",
        "INFO",
    )
    return {"social_lessons": lessons, "social_profile_updates": profile_updates}
