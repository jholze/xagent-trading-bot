"""Dynamic social chorus — raise CMC / Santiment / LunarCrush weight only when they agree.

Default regime mix stays 62% tech / 38% social. When ≥2 of {cmc, santiment, lunar}
vote the same way, social gets a louder seat. Mixed or thin → no boost.
Fusion RISK_OFF/CRASH and climax harvest never boost buys (no FOMO into a dump).

LunarCrush may be absent (sidecar off) — CMC + Santiment is enough for a 2-source chorus.
Kill: regime_detector.dynamic_social.enabled=false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "min_sources": 2,
    "base_tech_weight": 0.62,
    "base_sentiment_weight": 0.38,
    "bull_sentiment_weight": 0.55,
    "bear_sentiment_weight": 0.48,
    "cmc_chorus_trust_mult": 1.25,
    "quotes_bull_min_conf": 80.0,
    "signal_bull_min_conf": 55.0,
    "block_on_fusion_risk_off": True,
    "block_on_climax_harvest": True,
}

VOTE_BULL = "bull"
VOTE_BEAR = "bear"
AGREE_THIN = "thin"
AGREE_MIXED = "mixed"


@dataclass(frozen=True)
class SocialChorus:
    agree: str
    n_present: int
    n_bull: int
    n_bear: int
    sources: tuple[str, ...]
    tech_weight: float
    sentiment_weight: float
    cmc_trust_mult: float
    boost_buys: bool
    reasons: tuple[str, ...]


def dynamic_social_config(config_raw: dict | None = None) -> dict:
    root = dict((config_raw or {}).get("regime_detector") or {})
    block = dict(root.get("dynamic_social") or {})
    return {**_DEFAULTS, **block}


def _f(val: Any, default: float | None = None) -> float | None:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _vote_cmc(ctx: dict, cfg: dict) -> str | None:
    act = str(ctx.get("cmc_action") or "").upper()
    conf = _f(ctx.get("cmc_confidence"), 0.0) or 0.0
    quotes = bool(ctx.get("cmc_quotes_fallback"))
    if act == "BUY":
        floor = float(cfg["quotes_bull_min_conf"] if quotes else cfg["signal_bull_min_conf"])
        return VOTE_BULL if conf >= floor else None
    if act == "SELL":
        return VOTE_BEAR
    sent = _f(ctx.get("cmc_sentiment"))
    if sent is None:
        return None
    if sent >= 60:
        return VOTE_BULL
    if sent <= 40:
        return VOTE_BEAR
    return None


def _vote_lunar(ctx: dict) -> str | None:
    act = str(ctx.get("lc_action") or "").upper()
    if act == "BUY":
        return VOTE_BULL
    if act == "SELL":
        return VOTE_BEAR
    sent = _f(ctx.get("lunarcrush_sentiment"))
    if sent is None:
        return None
    if sent >= 60:
        return VOTE_BULL
    if sent <= 40:
        return VOTE_BEAR
    return None


def _vote_santiment(ctx: dict) -> str | None:
    regime = str(ctx.get("fusion_regime") or ctx.get("santiment_regime") or "").upper()
    if regime == "RISK_ON":
        return VOTE_BULL
    if regime in {"RISK_OFF", "CRASH"}:
        return VOTE_BEAR
    sent = _f(ctx.get("santiment_sentiment"))
    if sent is None:
        return None
    # injected fusion RISK_ON is 0.55 on -1..1
    if sent > 1.0:
        sent = (sent - 50.0) / 50.0
    if sent >= 0.35:
        return VOTE_BULL
    if sent <= -0.35:
        return VOTE_BEAR
    return None


def evaluate_social_chorus(
    social_context: dict | None,
    *,
    cfg: dict | None = None,
    climax_mode: str | None = None,
) -> SocialChorus:
    cfg = {**_DEFAULTS, **(cfg or {})}
    base_t = float(cfg["base_tech_weight"])
    base_s = float(cfg["base_sentiment_weight"])
    idle = SocialChorus(
        agree=AGREE_THIN,
        n_present=0,
        n_bull=0,
        n_bear=0,
        sources=(),
        tech_weight=base_t,
        sentiment_weight=base_s,
        cmc_trust_mult=1.0,
        boost_buys=False,
        reasons=("disabled",) if not cfg.get("enabled") else ("thin",),
    )
    if not bool(cfg.get("enabled")):
        return idle

    ctx = social_context or {}
    fusion = str(ctx.get("fusion_regime") or "").upper()
    if bool(cfg.get("block_on_fusion_risk_off")) and fusion in {"RISK_OFF", "CRASH"}:
        return SocialChorus(
            agree=AGREE_THIN,
            n_present=0,
            n_bull=0,
            n_bear=0,
            sources=(),
            tech_weight=base_t,
            sentiment_weight=base_s,
            cmc_trust_mult=1.0,
            boost_buys=False,
            reasons=("fusion_risk_off",),
        )
    mode = str(climax_mode or ctx.get("climax_mode") or "").lower()
    harvest_blocked = bool(cfg.get("block_on_climax_harvest")) and mode in {
        "harvest",
        "tighten",
    }

    votes: list[tuple[str, str]] = []
    cmc = _vote_cmc(ctx, cfg)
    if cmc:
        votes.append(("cmc", cmc))
    san = _vote_santiment(ctx)
    if san:
        votes.append(("santiment", san))
    lc = _vote_lunar(ctx)
    if lc:
        votes.append(("lunar", lc))

    n_bull = sum(1 for _, v in votes if v == VOTE_BULL)
    n_bear = sum(1 for _, v in votes if v == VOTE_BEAR)
    sources = tuple(name for name, _ in votes)
    need = int(cfg.get("min_sources") or 2)

    if len(votes) < need:
        return SocialChorus(
            agree=AGREE_THIN,
            n_present=len(votes),
            n_bull=n_bull,
            n_bear=n_bear,
            sources=sources,
            tech_weight=base_t,
            sentiment_weight=base_s,
            cmc_trust_mult=1.0,
            boost_buys=False,
            reasons=("thin",),
        )

    if n_bull >= need and n_bull > n_bear:
        agree = VOTE_BULL
        sw = float(cfg["bull_sentiment_weight"])
        boost = not harvest_blocked
        reasons = ("chorus_bull",) + (("harvest_no_buy_boost",) if harvest_blocked else ())
        trust_mult = float(cfg["cmc_chorus_trust_mult"]) if boost else 1.0
    elif n_bear >= need and n_bear > n_bull:
        agree = VOTE_BEAR
        sw = float(cfg["bear_sentiment_weight"])
        boost = False
        reasons = ("chorus_bear",)
        trust_mult = 1.0
    else:
        return SocialChorus(
            agree=AGREE_MIXED,
            n_present=len(votes),
            n_bull=n_bull,
            n_bear=n_bear,
            sources=sources,
            tech_weight=base_t,
            sentiment_weight=base_s,
            cmc_trust_mult=1.0,
            boost_buys=False,
            reasons=("mixed",),
        )

    sw = min(0.70, max(0.20, sw))
    tw = round(1.0 - sw, 4)
    return SocialChorus(
        agree=agree,
        n_present=len(votes),
        n_bull=n_bull,
        n_bear=n_bear,
        sources=sources,
        tech_weight=tw,
        sentiment_weight=sw,
        cmc_trust_mult=trust_mult,
        boost_buys=boost,
        reasons=reasons,
    )
