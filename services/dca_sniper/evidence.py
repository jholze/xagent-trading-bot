"""Evidence pack for sniper deep pass: news/facts, path stats, wallet placeholder.

Design (future bot):
  - News/facts: **read from Memory** (async ingest elsewhere), never live-scrape
    in the decision hot path.
  - Path stats: soft historical bias for recovery quality (fail-open).
  - Wallet/on-chain: adapter interface only; pure evaluator ready when a feed exists.
    No fake confidence without data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NewsItem:
    event_type: str
    impact: float
    description: str
    age_hours: float | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidencePack:
    symbol: str
    lookback_hours: float = 72.0
    news: list[NewsItem] = field(default_factory=list)
    freshest_age_hours: float | None = None
    facts_fresh: bool = False  # at least one event within lookback
    hard_news: bool = False  # hack/exploit/unlock-class in pack
    path_stats: dict[str, Any] = field(default_factory=dict)
    wallet: dict[str, Any] = field(default_factory=dict)
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "lookback_hours": self.lookback_hours,
            "news": [n.to_dict() for n in self.news[:12]],
            "news_count": len(self.news),
            "freshest_age_hours": self.freshest_age_hours,
            "facts_fresh": self.facts_fresh,
            "hard_news": self.hard_news,
            "path_stats": self.path_stats,
            "wallet": self.wallet,
            "sources_used": list(self.sources_used),
            "errors": list(self.errors)[:5],
        }


def _parse_event_ts(ev: Any) -> datetime | None:
    for attr in ("created_at", "timestamp", "ts", "at"):
        raw = getattr(ev, attr, None)
        if raw is None and isinstance(ev, dict):
            raw = ev.get(attr)
        if not raw:
            continue
        try:
            s = str(raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _event_fields(ev: Any) -> tuple[str, float, str, str]:
    if isinstance(ev, dict):
        et = str(ev.get("event_type") or ev.get("type") or "noise")
        try:
            imp = float(ev.get("impact_score") if ev.get("impact_score") is not None else ev.get("impact") or 0)
        except (TypeError, ValueError):
            imp = 0.0
        desc = str(ev.get("description") or ev.get("text") or "")[:200]
        src = str(ev.get("source") or "")
        return et, imp, desc, src
    et = str(getattr(ev, "event_type", None) or getattr(ev, "type", None) or "noise")
    try:
        imp = float(getattr(ev, "impact_score", None) or getattr(ev, "impact", 0) or 0)
    except (TypeError, ValueError):
        imp = 0.0
    desc = str(getattr(ev, "description", None) or getattr(ev, "text", None) or "")[:200]
    src = str(getattr(ev, "source", "") or "")
    return et, imp, desc, src


def events_to_news_items(
    events: list[Any],
    *,
    now: datetime | None = None,
    max_items: int = 12,
) -> tuple[list[NewsItem], float | None]:
    """Convert memory events → NewsItem list + freshest age hours."""
    now = now or datetime.now(timezone.utc)
    items: list[NewsItem] = []
    ages: list[float] = []
    for ev in events or []:
        et, imp, desc, src = _event_fields(ev)
        dt = _parse_event_ts(ev)
        age = None
        if dt is not None:
            age = max(0.0, (now - dt).total_seconds() / 3600.0)
            ages.append(age)
        items.append(
            NewsItem(
                event_type=et.lower(),
                impact=max(-1.0, min(1.0, imp)),
                description=desc,
                age_hours=round(age, 2) if age is not None else None,
                source=src,
            )
        )
    # worst impact first, then freshest
    items.sort(key=lambda n: (n.impact, -(n.age_hours if n.age_hours is not None else 1e9)))
    freshest = min(ages) if ages else None
    return items[:max_items], freshest


_HARD_NEWS_TYPES = frozenset(
    {"hack", "exploit", "sec_alert", "delisting", "unlock", "supply_unlock", "supply_overhang"}
)


def news_is_hard(items: list[NewsItem]) -> bool:
    for n in items:
        if n.event_type in _HARD_NEWS_TYPES:
            return True
        if n.impact <= -0.7:
            return True
    return False


def load_symbol_events(
    symbol: str,
    *,
    lookback_hours: float = 72.0,
    limit: int = 40,
    store: Any = None,
) -> list[Any]:
    """Fail-open load of memory events for symbol."""
    try:
        if store is None:
            # Avoid hanging unit tests / hosts without Mongo
            import os

            uri = (
                os.environ.get("MONGO_URL")
                or os.environ.get("MONGODB_URI")
                or os.environ.get("MONGO_PUBLIC_URL")
                or ""
            ).strip()
            if not uri and not os.environ.get("DCA_SNIPER_ALLOW_DEFAULT_MONGO"):
                return []
            from intelligence.memory.store import MemoryStore

            store = MemoryStore()
        since = (
            datetime.now(timezone.utc).timestamp() - float(lookback_hours) * 3600
        )
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return list(store.list_events(symbol=symbol, since_iso=since_iso, limit=limit) or [])
    except Exception:
        return []


def load_path_stats_brief(
    symbol: str,
    *,
    config_raw: dict | None = None,
) -> dict[str, Any]:
    """Soft historical path brief (fail-open)."""
    out: dict[str, Any] = {"available": False}
    try:
        from intelligence.memory.path_stats import (
            list_path_summaries_for_symbol,
            path_stats_enabled,
        )
        from intelligence.memory.path_stats_bias import pick_band_summary, soft_bias_config

        if not path_stats_enabled(config_raw):
            out["reason"] = "disabled"
            return out
        summaries = list_path_summaries_for_symbol(symbol) or []
        if not summaries:
            out["reason"] = "no_data"
            return out
        sb = soft_bias_config(config_raw)
        band = pick_band_summary(
            summaries,
            prefer_band=float(sb.get("prefer_band_pct") or 10) / 100.0,
            require_quality=str(sb.get("require_quality") or "ok"),
        )
        if band is None:
            out["reason"] = "no_band"
            return out
        out["available"] = True
        out["band"] = getattr(band, "band_pct", None) or getattr(band, "band", None)
        out["n"] = getattr(band, "n", None) or getattr(band, "samples", None)
        out["trail_hit_rate"] = getattr(band, "trail_hit_rate", None)
        out["median_giveback"] = getattr(band, "median_giveback", None)
        out["quality"] = getattr(band, "quality", None)
        # soft hint for recovery: high giveback after bounce → caution
        try:
            gb = float(out["median_giveback"] or 0)
            if gb >= 0.12:
                out["hint"] = "high_giveback_caution"
            elif gb <= 0.05:
                out["hint"] = "held_extensions"
            else:
                out["hint"] = "neutral"
        except (TypeError, ValueError):
            out["hint"] = "neutral"
        return out
    except Exception as e:
        out["reason"] = f"error:{type(e).__name__}"
        return out


def wallet_evidence(
    symbol: str,
    *,
    provider: Any = None,
) -> dict[str, Any]:
    """Wallet/on-chain evidence adapter.

    Future bot: inject a provider with ``fetch_flows(symbol) -> dict``.
    Until then: explicit unavailable — never invent whales.
    """
    if provider is None:
        return {
            "available": False,
            "status": "unavailable",
            "reason": "no_wallet_provider",
            "net_flow": None,
            "exchange_inflow": None,
            "large_tx_count": None,
        }
    try:
        data = provider.fetch_flows(symbol) or {}
        if not isinstance(data, dict):
            return {"available": False, "status": "bad_payload", "reason": "invalid"}
        return {
            "available": True,
            "status": "ok",
            "reason": None,
            "net_flow": data.get("net_flow"),
            "exchange_inflow": data.get("exchange_inflow"),
            "large_tx_count": data.get("large_tx_count"),
            "raw": {k: data[k] for k in list(data)[:8]},
        }
    except Exception as e:
        return {
            "available": False,
            "status": "error",
            "reason": f"{type(e).__name__}:{e}"[:80],
            "net_flow": None,
            "exchange_inflow": None,
            "large_tx_count": None,
        }


def evaluate_wallet_soft(
    wallet: dict[str, Any],
    *,
    heavy_inflow_blocks_heavy: bool = True,
) -> tuple[float, list[str]]:
    """Pure soft policy on wallet pack. Returns (size_mult, reason_codes).

    Only acts when available=True and numeric signals present.
    """
    if not wallet or not wallet.get("available"):
        return 1.0, []
    codes: list[str] = []
    mult = 1.0
    try:
        inflow = wallet.get("exchange_inflow")
        if inflow is not None and float(inflow) > 0:
            # coins moving to exchange → distribution risk
            mult *= 0.75
            codes.append("wallet_exchange_inflow")
            if heavy_inflow_blocks_heavy and float(inflow) > 1.0:
                # signal only — hard block left to caller
                codes.append("wallet_inflow_elevated")
        net = wallet.get("net_flow")
        if net is not None and float(net) < 0:
            mult *= 0.85
            codes.append("wallet_net_outflow")
    except (TypeError, ValueError):
        return 1.0, []
    return max(0.5, min(1.0, mult)), codes


def gather_evidence(
    symbol: str,
    *,
    config_raw: dict | None = None,
    lookback_hours: float | None = None,
    store: Any = None,
    wallet_provider: Any = None,
    events: list[Any] | None = None,
) -> EvidencePack:
    """Assemble full evidence pack for a symbol (fail-open I/O)."""
    lb = 72.0
    max_ev = 40
    try:
        if lookback_hours is not None:
            lb = float(lookback_hours)
        else:
            try:
                # Optional config; must not fail whole pack if intelligence deps missing
                from intelligence.memory.coin_facts import coin_facts_config

                cfg = coin_facts_config(config_raw)
                lb = float(cfg.get("lookback_hours") or 72)
                max_ev = int(cfg.get("max_events_per_symbol") or 40)
            except Exception:
                lb = 72.0
    except (TypeError, ValueError):
        lb = 72.0
    pack = EvidencePack(symbol=str(symbol or ""), lookback_hours=lb)
    try:
        evs = events if events is not None else load_symbol_events(
            symbol, lookback_hours=lb, limit=max_ev, store=store
        )
        if evs:
            pack.sources_used.append("memory_events")
        news, freshest = events_to_news_items(evs)
        pack.news = news
        pack.freshest_age_hours = freshest
        pack.facts_fresh = bool(news) and (
            freshest is None or freshest <= lb
        )
        pack.hard_news = news_is_hard(news)
    except Exception as e:
        pack.errors.append(f"events:{type(e).__name__}")

    try:
        ps = load_path_stats_brief(symbol, config_raw=config_raw)
        pack.path_stats = ps
        if ps.get("available"):
            pack.sources_used.append("path_stats")
    except Exception as e:
        pack.errors.append(f"path_stats:{type(e).__name__}")

    try:
        pack.wallet = wallet_evidence(symbol, provider=wallet_provider)
        if pack.wallet.get("available"):
            pack.sources_used.append("wallet")
    except Exception as e:
        pack.errors.append(f"wallet:{type(e).__name__}")
        pack.wallet = wallet_evidence(symbol, provider=None)

    return pack


def apply_evidence_to_candidate(
    cand: dict[str, Any],
    pack: EvidencePack,
) -> dict[str, Any]:
    """Merge evidence into candidate for checklist/quality."""
    out = dict(cand)
    out["evidence"] = pack.to_dict()
    out["news_count"] = len(pack.news)
    out["facts_fresh"] = pack.facts_fresh
    out["hard_news"] = pack.hard_news
    out["freshest_news_age_h"] = pack.freshest_age_hours
    if pack.news:
        # top brief for operators
        out["news_brief"] = [
            f"{n.event_type}:{n.impact:+.2f}:{n.description[:60]}" for n in pack.news[:5]
        ]
        # if hard news types present, unlock/hard flags
        if pack.hard_news:
            types = {n.event_type for n in pack.news}
            if types & {"hack", "exploit", "sec_alert", "delisting"}:
                out["hard_negative"] = True
                out["unlock_risk"] = True
            if types & {"unlock", "supply_unlock", "supply_overhang"}:
                out["unlock_risk"] = True
                out["fact_unlock"] = True
        # social noise from news
        if any(n.event_type in ("social_spike", "noise") for n in pack.news):
            out["social_noise"] = True
        if not out.get("fact_summary") and pack.news:
            out["fact_summary"] = pack.news[0].description[:160]
        if not out.get("fact_event_count"):
            out["fact_event_count"] = len(pack.news)
    if pack.path_stats.get("available"):
        out["path_stats_hint"] = pack.path_stats.get("hint")
        out["path_stats"] = pack.path_stats
    out["wallet"] = pack.wallet
    return out


def apply_evidence_size_adjust(
    usdt: float,
    size_reason: str,
    pack: EvidencePack,
    *,
    cfg: dict[str, Any] | None = None,
) -> tuple[float, str, list[str]]:
    """Pure size adjustments from evidence (after policy)."""
    cfg = cfg or {}
    extra: list[str] = []
    usdt = float(usdt or 0)
    reason = str(size_reason or "")
    if usdt <= 0:
        return usdt, reason, extra

    # hard news already often policy-skipped; double-safe
    if pack.hard_news and bool(cfg.get("deep_hard_news_blocks_heavy", True)):
        if "HEAVY" in reason.upper():
            small = float(cfg.get("small_dca_usdt") or 500)
            min_u = float(cfg.get("min_meaningful_usdt") or 200)
            if bool(cfg.get("deep_allow_small_if_thin", True)) and small >= min_u:
                return round(min(usdt, small), 2), "DCA_SMALL_hard_news", extra
            return 0.0, "hard_news_block", ["hard_news"]

    # path stats high giveback → trim size
    hint = (pack.path_stats or {}).get("hint")
    if hint == "high_giveback_caution" and usdt > 0:
        usdt = round(usdt * 0.85, 2)
        if "path_giveback" not in reason:
            reason = f"{reason}+path_caution" if reason else "path_caution"

    w_mult, w_codes = evaluate_wallet_soft(pack.wallet or {})
    if w_codes and w_mult < 1.0:
        usdt = round(usdt * w_mult, 2)
        extra.extend(w_codes)
        if usdt < float(cfg.get("min_meaningful_usdt") or 200):
            return 0.0, "wallet_size_too_small", extra

    return usdt, reason, extra
