"""Coin-fact layer (#103 D8): pure taxonomy, classifiers, context flags.

No network. No ledger writes. Fail-open consumers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# --- taxonomy (v1) ---
EVENT_TYPES = frozenset(
    {
        "listing",
        "delisting",
        "unlock",
        "supply_unlock",
        "supply_overhang",
        "partnership",
        "mainnet",
        "hack",
        "exploit",
        "sec_alert",
        "ai_narrative",
        "sector_rotation",
        "social_spike",
        "profit_taking_narrative",
        "volume_breakout",
        "flow_only_move",
        "structure_bias",
        "structure_risk",
        "relative_strength",
        "utility_adoption",
        "ignore_target",
        "noise",
    }
)

HARD_NEGATIVE_TYPES = frozenset({"hack", "exploit", "sec_alert", "delisting"})
UNLOCK_TYPES = frozenset({"unlock", "supply_unlock", "supply_overhang"})
CMC_SOURCES = frozenset({"cmc_ai_updates", "cmc_ai_price", "cmc_ai_prediction"})

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "policy_apply": True,
    "lookback_hours": 72,
    "max_events_per_symbol": 40,
    "sources": {
        "cmc_ai": {
            "enabled": True,
            "scrape_fallback": True,
            "ttl_hours_updates": 48,
            "ttl_hours_price": 12,
            "ttl_hours_prediction": 72,
            "max_coins_per_cycle": 40,
            "interval_sec": 3600,
            "prediction_use_targets_for_policy": False,
            "max_events_per_coin_cycle": 8,
        }
    },
    "universe": ["open_positions", "watchlist"],
}


@dataclass
class CoinFactDraft:
    """Parsed fact before persist."""

    event_type: str
    impact_score: float
    description: str
    source: str = "cmc_ai_updates"
    polarity_hint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        et = str(self.event_type or "noise").strip().lower()
        self.event_type = et if et in EVENT_TYPES else "noise"
        try:
            self.impact_score = max(-1.0, min(1.0, float(self.impact_score)))
        except (TypeError, ValueError):
            self.impact_score = 0.0
        self.description = str(self.description or "")[:500]
        self.source = str(self.source or "cmc_ai_updates")


@dataclass
class FactFlags:
    """Aggregated flags for one symbol (policy input)."""

    hard_negative: bool = False
    unlock: bool = False
    profit_taking: bool = False
    flow_only: bool = False
    structure_risk: bool = False
    volume_breakout: bool = False
    catalyst: bool = False
    utility: bool = False
    noise_only: bool = False
    event_count: int = 0
    min_impact: float = 0.0
    summary: str = ""


def coin_facts_config(config_raw: dict | None = None) -> dict[str, Any]:
    """Merge defaults ← config.memory.coin_facts."""
    raw = config_raw
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    mem = (raw or {}).get("memory") or {}
    user = dict(mem.get("coin_facts") or {})
    out = {**_DEFAULTS, **{k: v for k, v in user.items() if k != "sources"}}
    src_def = dict(_DEFAULTS.get("sources") or {})
    src_user = dict(user.get("sources") or {})
    cmc_def = dict(src_def.get("cmc_ai") or {})
    cmc_user = dict(src_user.get("cmc_ai") or {})
    out["sources"] = {**src_def, **src_user, "cmc_ai": {**cmc_def, **cmc_user}}
    return out


def coin_facts_enabled(config_raw: dict | None = None) -> bool:
    return bool(coin_facts_config(config_raw).get("enabled"))


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if s and "/" not in s:
        s = f"{s}/USDT"
    return s


# --- classifiers (keyword heuristics; no LLM) ---

_RE_HARD = re.compile(
    r"\b(hack|exploit|drained?|bridge\s+attack|sec\s*charg|fraud|rug\s*pull|delist(?:ing)?)\b",
    re.I,
)
_RE_UNLOCK = re.compile(
    r"\b(unlock|vesting|cliff|low\s+float|circulating\s+supply|supply\s+overhang|"
    r"token\s+unlock|overhang)\b",
    re.I,
)
_RE_PROFIT_TAKE = re.compile(
    r"\b(profit[-\s]?tak(?:e|ing)|cool(?:s|ing|ed|[- ]down)|cools?\b|take[-\s]?profit|"
    r"drops?\s+~?\d|falls?\s+~?\d|sell[-\s]?off)\b",
    re.I,
)
_RE_PARTNER = re.compile(
    r"\b(partnership|integrat(?:ion|es|ed)|mainnet|launch(?:es|ed)?|"
    r"collaboration|listed\s+on)\b",
    re.I,
)
_RE_SOCIAL = re.compile(
    r"\b(social\s+spike|trending\s+on\s+twitter|viral|telegram\s+pump|"
    r"bullish\s+signs?\s+only)\b",
    re.I,
)
_RE_SECTOR = re.compile(
    r"\b(ai\s+(?:token\s+)?rotation|sector\s+rotation|narrative\s+rotation)\b",
    re.I,
)
_RE_FLOW = re.compile(
    r"\b(no\s+clear\s+(?:secondary\s+)?driver|no\s+(?:news\s+)?catalyst|"
    r"flow[-\s]?driven|flow[-\s]?only|without\s+(?:clear\s+)?catalyst)\b",
    re.I,
)
_RE_VOL = re.compile(
    r"\b(volume\s+(?:surge|spike|breakout)|vol\s*\+?\s*\d+%|volume\s+up)\b",
    re.I,
)
_RE_STRUCT_RISK = re.compile(
    r"\b(break(?:s|ing)?\s+below\s+support|lose\s+support|support\s+fail|"
    r"structure\s+risk|volume\s+fade)\b",
    re.I,
)
_RE_STRUCT_BIAS = re.compile(
    r"\b(cautiously\s+bullish|key\s+support|structure\s+bias|holds?\s+support)\b",
    re.I,
)
_RE_RS = re.compile(r"\b(outperform(?:s|ing)?\s+(?:vs\s+)?btc|relative\s+strength)\b", re.I)
_RE_UTILITY = re.compile(
    r"\b(utility|adoption|integration|real\s+use|kalshi|quack|inference\s+demand)\b",
    re.I,
)
_RE_PRICE_TARGET = re.compile(
    r"(?:price\s+(?:will\s+)?(?:hit|reach|be)|target(?:\s+price)?|"
    r"will\s+hit\s+\$|prediction:\s*.*\$\s*\d)",
    re.I,
)


def classify_latest_updates_bullet(text: str) -> CoinFactDraft | None:
    t = (text or "").strip()
    if not t or len(t) < 8:
        return None
    if _RE_HARD.search(t):
        return CoinFactDraft(
            event_type="hack" if re.search(r"hack|exploit|drain", t, re.I) else "sec_alert",
            impact_score=-0.9,
            description=t[:400],
            source="cmc_ai_updates",
            polarity_hint="-",
        )
    if _RE_UNLOCK.search(t):
        return CoinFactDraft(
            event_type="unlock" if "unlock" in t.lower() else "supply_overhang",
            impact_score=-0.55,
            description=t[:400],
            source="cmc_ai_updates",
            polarity_hint="-",
        )
    if _RE_PROFIT_TAKE.search(t):
        return CoinFactDraft(
            event_type="profit_taking_narrative",
            impact_score=-0.45,
            description=t[:400],
            source="cmc_ai_updates",
            polarity_hint="-",
        )
    if _RE_PARTNER.search(t):
        return CoinFactDraft(
            event_type="partnership" if "partner" in t.lower() else "mainnet",
            impact_score=0.35,
            description=t[:400],
            source="cmc_ai_updates",
            polarity_hint="+",
        )
    if _RE_SECTOR.search(t):
        return CoinFactDraft(
            event_type="sector_rotation",
            impact_score=0.15,
            description=t[:400],
            source="cmc_ai_updates",
            polarity_hint="mixed",
        )
    if _RE_SOCIAL.search(t):
        return CoinFactDraft(
            event_type="social_spike",
            impact_score=0.05,
            description=t[:400],
            source="cmc_ai_updates",
            polarity_hint="mixed",
        )
    return None


def classify_price_analysis_snippet(text: str) -> CoinFactDraft | None:
    t = (text or "").strip()
    if not t or len(t) < 8:
        return None
    if _RE_FLOW.search(t):
        return CoinFactDraft(
            event_type="flow_only_move",
            impact_score=-0.15,
            description=t[:400],
            source="cmc_ai_price",
            polarity_hint="caution",
        )
    if _RE_STRUCT_RISK.search(t):
        return CoinFactDraft(
            event_type="structure_risk",
            impact_score=-0.5,
            description=t[:400],
            source="cmc_ai_price",
            polarity_hint="-",
        )
    if _RE_VOL.search(t):
        return CoinFactDraft(
            event_type="volume_breakout",
            impact_score=0.25,
            description=t[:400],
            source="cmc_ai_price",
            polarity_hint="+",
        )
    if _RE_RS.search(t):
        return CoinFactDraft(
            event_type="relative_strength",
            impact_score=0.2,
            description=t[:400],
            source="cmc_ai_price",
            polarity_hint="+",
        )
    if _RE_STRUCT_BIAS.search(t):
        return CoinFactDraft(
            event_type="structure_bias",
            impact_score=0.15,
            description=t[:400],
            source="cmc_ai_price",
            polarity_hint="+",
        )
    return None


def classify_prediction_driver(
    text: str,
    *,
    section: str = "",
) -> CoinFactDraft | None:
    """Structural drivers only — never numeric price targets as trade signals."""
    t = (text or "").strip()
    if not t or len(t) < 8:
        return None
    sec = str(section or "").lower()
    if _RE_PRICE_TARGET.search(t) and not _RE_UNLOCK.search(t) and not _RE_UTILITY.search(t):
        return CoinFactDraft(
            event_type="ignore_target",
            impact_score=0.0,
            description=t[:400],
            source="cmc_ai_prediction",
            polarity_hint="ignore",
            metadata={"section": sec, "policy": "ignore"},
        )
    if _RE_UNLOCK.search(t) or "bearish" in sec:
        if _RE_UNLOCK.search(t) or "vest" in t.lower() or "float" in t.lower():
            return CoinFactDraft(
                event_type="supply_overhang" if "float" in t.lower() or "vest" in t.lower() else "unlock",
                impact_score=-0.55 if "bearish" in sec or _RE_UNLOCK.search(t) else -0.4,
                description=t[:400],
                source="cmc_ai_prediction",
                polarity_hint="-",
                metadata={"section": sec},
            )
    if _RE_UTILITY.search(t) or "bullish" in sec:
        if _RE_UTILITY.search(t) or "bullish" in sec:
            return CoinFactDraft(
                event_type="utility_adoption",
                impact_score=0.3 if "bullish" in sec else 0.2,
                description=t[:400],
                source="cmc_ai_prediction",
                polarity_hint="+",
                metadata={"section": sec},
            )
    if _RE_SECTOR.search(t) or "mixed" in sec:
        return CoinFactDraft(
            event_type="sector_rotation",
            impact_score=0.1,
            description=t[:400],
            source="cmc_ai_prediction",
            polarity_hint="mixed",
            metadata={"section": sec},
        )
    return None


def flags_from_events(events: list[Any]) -> FactFlags:
    """Reduce MarketEvent-like objects / drafts into FactFlags."""
    flags = FactFlags()
    if not events:
        return flags

    actionable = 0
    noise_n = 0
    impacts: list[float] = []
    bits: list[str] = []

    for ev in events:
        et = str(getattr(ev, "event_type", None) or (ev.get("event_type") if isinstance(ev, dict) else "") or "").lower()
        try:
            imp = float(
                getattr(ev, "impact_score", None)
                if not isinstance(ev, dict)
                else ev.get("impact_score", 0)
                or 0
            )
        except (TypeError, ValueError):
            imp = 0.0
        desc = str(
            getattr(ev, "description", None)
            if not isinstance(ev, dict)
            else ev.get("description", "")
            or ""
        )[:80]

        if et in ("ignore_target",):
            continue
        if et in ("social_spike", "noise"):
            noise_n += 1
            continue

        actionable += 1
        impacts.append(imp)
        if et in HARD_NEGATIVE_TYPES or imp <= -0.8:
            flags.hard_negative = True
        if et in UNLOCK_TYPES:
            flags.unlock = True
        if et == "profit_taking_narrative":
            flags.profit_taking = True
        if et == "flow_only_move":
            flags.flow_only = True
        if et == "structure_risk":
            flags.structure_risk = True
        if et == "volume_breakout":
            flags.volume_breakout = True
        if et in ("partnership", "mainnet", "listing"):
            flags.catalyst = True
        if et == "utility_adoption":
            flags.utility = True
        if desc:
            bits.append(f"{et}:{desc[:40]}")

    flags.event_count = actionable + noise_n
    if actionable == 0 and noise_n > 0:
        flags.noise_only = True
    if impacts:
        flags.min_impact = min(impacts)
    flags.summary = "; ".join(bits[:4])
    return flags


def apply_fact_flags_to_context(ctx: Any, flags: FactFlags) -> None:
    """Mutate DcaContext-like object with fact_* fields."""
    ctx.fact_hard_negative = bool(flags.hard_negative)
    ctx.fact_unlock = bool(flags.unlock)
    ctx.fact_profit_taking = bool(flags.profit_taking)
    ctx.fact_flow_only = bool(flags.flow_only)
    ctx.fact_structure_risk = bool(flags.structure_risk)
    ctx.fact_volume_breakout = bool(flags.volume_breakout)
    ctx.fact_catalyst = bool(flags.catalyst)
    ctx.fact_utility = bool(flags.utility)
    ctx.fact_noise_only = bool(flags.noise_only)
    ctx.fact_event_count = int(flags.event_count)
    ctx.fact_min_impact = float(flags.min_impact)
    ctx.fact_summary = str(flags.summary or "")


def summarize_facts_for_symbol(
    symbol: str,
    *,
    store: Any = None,
    config_raw: dict | None = None,
    events: list[Any] | None = None,
) -> FactFlags:
    """Load recent events for symbol and return flags. Fail-open empty."""
    cfg = coin_facts_config(config_raw)
    if not cfg.get("enabled") and events is None:
        return FactFlags()
    if not cfg.get("policy_apply", True) and events is None:
        # still allow explicit events injection for tests
        pass

    sym = normalize_symbol(symbol)
    if events is not None:
        return flags_from_events(events)

    try:
        lookback = float(cfg.get("lookback_hours") or 72)
    except (TypeError, ValueError):
        lookback = 72.0
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    limit = int(cfg.get("max_events_per_symbol") or 40)

    try:
        if store is None:
            from intelligence.memory.store import MemoryStore

            store = MemoryStore()
        raw_events = store.list_events(symbol=sym, since_iso=since, limit=limit) or []
        # Prefer CMC AI + known taxonomy types for policy
        filtered = []
        for e in raw_events:
            src = str(getattr(e, "source", "") or "")
            et = str(getattr(e, "event_type", "") or "")
            if src in CMC_SOURCES or et in EVENT_TYPES:
                filtered.append(e)
        return flags_from_events(filtered or raw_events)
    except Exception:
        return FactFlags()


def apply_facts_to_context(
    ctx: Any,
    *,
    config_raw: dict | None = None,
    store: Any = None,
    events: list[Any] | None = None,
) -> FactFlags:
    """Load facts for ctx.symbol and set fact_* fields. Fail-open."""
    cfg = coin_facts_config(config_raw)
    if not cfg.get("enabled") and events is None:
        return FactFlags()
    if not cfg.get("policy_apply", True) and events is None:
        return FactFlags()
    sym = str(getattr(ctx, "symbol", "") or "")
    flags = summarize_facts_for_symbol(
        sym, store=store, config_raw=config_raw, events=events
    )
    apply_fact_flags_to_context(ctx, flags)
    return flags
