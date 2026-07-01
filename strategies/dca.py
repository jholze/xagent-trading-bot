"""DCA accumulation — multi-factor scoring before first exit-ladder sell."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.actions import BUY_DCA
from core.config import get_bot_config
from core.models import MarketContext


@dataclass
class DCADecision:
    should_dca: bool
    score: int = 0
    max_score: int = 10
    breakdown: dict[str, int] = field(default_factory=dict)
    blocked_reason: str | None = None
    usdt_amount: float = 0.0
    shadow_only: bool = False


@dataclass
class DCACandidate:
    action: str
    source: str
    rationale: str
    usdt_amount: float
    shadow_only: bool = False
    score: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)


def dca_config(strategy_params: dict | None) -> dict:
    params = strategy_params or {}
    return dict(params.get("dca") or {})


def dca_enabled(strategy_params: dict | None) -> bool:
    cfg = dca_config(strategy_params)
    return bool(cfg.get("enabled", False))


def _unrealized_loss_pct(entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    return (price / entry - 1.0) * 100.0


def _hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    return (datetime.now() - last_ts).total_seconds() / 3600.0


def _in_accumulation_phase(position: dict) -> bool:
    step = int(position.get("exit_ladder_step", 0) or 0)
    sold = float(position.get("sold_percent", 0) or 0)
    return step == 0 and sold < 0.01


def effective_stop_loss_thresholds(
    position: dict,
    strategy_params: dict | None,
    base_stop_loss_pct: float,
) -> tuple[float, float | None, bool]:
    """Return (full_stop_pct, partial_stop_pct_or_none, in_grace_period)."""
    params = strategy_params or {}
    dca_cfg = dca_config(params)
    stop_loss_pct = float(params.get("stop_loss_pct") or base_stop_loss_pct)

    if params.get("partial_stop_pct") is not None:
        partial_stop = float(params["partial_stop_pct"])
    else:
        partial_ratio = float(params.get("partial_stop_ratio", 0.67))
        partial_stop = stop_loss_pct * partial_ratio

    dca_rounds = int(position.get("dca_rounds", 0) or 0)
    widen = float(dca_cfg.get("stop_loss_widen_pct_per_round", 0))
    full_stop = stop_loss_pct + dca_rounds * widen

    grace_hours = float(dca_cfg.get("grace_hours_after_dca", 0))
    if grace_hours <= 0:
        grace_hours = float(dca_cfg.get("interval_hours", 12))

    elapsed = _hours_since(position.get("last_dca_at"))
    in_grace = (
        dca_rounds > 0
        and position.get("last_dca_at")
        and elapsed is not None
        and elapsed < grace_hours
    )

    if dca_cfg.get("pause_partial_stop_during_dca", True) and dca_rounds > 0:
        partial_effective: float | None = None
    else:
        partial_effective = partial_stop

    return full_stop, partial_effective, in_grace


def _effective_max_dca_rounds(position: dict, cfg: dict) -> int:
    """Freeze max DCA rounds on first use so tier flips cannot grant extra rounds."""
    cfg_max = int(cfg.get("max_rounds", 3))
    frozen = int(position.get("dca_max_rounds", 0) or 0)
    if frozen <= 0:
        position["dca_max_rounds"] = cfg_max
        return cfg_max
    return frozen


def _near_stop_loss(
    loss_pct: float,
    strategy_params: dict,
    cfg: dict,
) -> bool:
    """Block DCA when unrealized loss is within sl_proximity_pct of the stop trigger."""
    proximity = float(cfg.get("sl_proximity_pct", 15))
    if proximity <= 0:
        return False
    stop_pct = float(
        strategy_params.get("stop_loss_pct")
        or get_bot_config().stop_loss_pct
    )
    margin = stop_pct + loss_pct
    if margin <= 0:
        return False
    buffer = stop_pct * (proximity / 100.0)
    return margin < buffer


def _volatility_tier(strategy_params: dict | None) -> str:
    tier = str((strategy_params or {}).get("volatility_tier") or "stable").lower()
    return tier if tier in ("stable", "volatile") else "stable"


def _scoring_profile(cfg: dict, strategy_params: dict | None) -> dict:
    scoring = dict(cfg.get("scoring") or {})
    tier = _volatility_tier(strategy_params)
    tier_cfg = dict(scoring.get(tier) or scoring.get("stable") or {})
    return tier_cfg


def _score_atr_distance(
    loss_pct: float,
    atr_pct: float,
    tier_cfg: dict,
) -> int:
    if atr_pct <= 0 or loss_pct >= 0:
        return 0
    drop_pct = abs(loss_pct)
    atr_multiples = drop_pct / atr_pct
    high = float(tier_cfg.get("atr_mult_high", 2.5))
    low = float(tier_cfg.get("atr_mult_low", 1.8))
    if atr_multiples >= high:
        return 3
    if atr_multiples >= low:
        return 2
    if atr_multiples >= low * 0.75:
        return 1
    return 0


def _score_rsi(rsi: float, tier_cfg: dict) -> int:
    hard = float(tier_cfg.get("rsi_hard", 30))
    soft = float(tier_cfg.get("rsi_soft", 35))
    if rsi < hard:
        return 2
    if rsi < soft:
        return 1
    return 0


def _score_funding(funding_rate_pct: float | None, tier_cfg: dict) -> int:
    if funding_rate_pct is None:
        return 0
    threshold = float(tier_cfg.get("funding_max_pct", -0.06))
    if funding_rate_pct <= threshold:
        return 2
    if funding_rate_pct <= threshold * 0.5:
        return 1
    return 0


def _score_btc_underperf(ratio: float | None, tier_cfg: dict) -> int:
    if ratio is None or ratio < 1.0:
        return 0
    high = float(tier_cfg.get("btc_underperf_high", 2.0))
    low = float(tier_cfg.get("btc_underperf_low", 1.5))
    if ratio >= high:
        return 2
    if ratio >= low:
        return 1
    return 0


def _score_bb_support(price: float, lower_bb: float, tier_cfg: dict) -> int:
    if lower_bb <= 0 or price <= 0:
        return 0
    if not bool(tier_cfg.get("bb_support_enabled", True)):
        return 0
    ratio = float(tier_cfg.get("bb_support_ratio", 1.02))
    if price <= lower_bb * ratio:
        return 1
    return 0


def _evaluate_scoring(
    market: MarketContext,
    loss_pct: float,
    cfg: dict,
    strategy_params: dict | None,
) -> DCADecision:
    scoring = dict(cfg.get("scoring") or {})
    tier_cfg = _scoring_profile(cfg, strategy_params)
    breakdown: dict[str, int] = {
        "atr_distance": _score_atr_distance(loss_pct, market.atr_pct, tier_cfg),
        "rsi": _score_rsi(market.rsi, tier_cfg),
        "funding": _score_funding(market.funding_rate_pct, tier_cfg),
        "btc_underperf": _score_btc_underperf(market.btc_underperf_ratio, tier_cfg),
        "bb_support": _score_bb_support(
            market.current_price, market.lower_bb, tier_cfg
        ),
    }
    core_keys = ("atr_distance", "rsi", "funding", "btc_underperf")
    core_score = sum(breakdown[k] for k in core_keys)
    total_score = core_score + breakdown["bb_support"]
    max_score = int(scoring.get("max_score", 10))
    min_score = int(scoring.get("min_score", 6))
    min_core = int(scoring.get("min_core_criteria_met", 3))
    core_met = sum(1 for k in core_keys if breakdown[k] > 0)

    fixed_usdt = float(cfg.get("fixed_usdt", 20))
    mode = str(cfg.get("mode", "shadow"))
    passed = total_score >= min_score and core_met >= min_core
    reason = None
    if not passed:
        reason = (
            f"score {total_score}/{max_score} "
            f"(core {core_met}/{min_core}): {breakdown}"
        )

    return DCADecision(
        should_dca=passed,
        score=total_score,
        max_score=max_score,
        breakdown=breakdown,
        blocked_reason=reason,
        usdt_amount=fixed_usdt,
        shadow_only=mode == "shadow",
    )


def _check_hard_gates(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    cfg: dict,
) -> tuple[bool, str | None, float]:
    if not cfg.get("enabled", False):
        return False, "dca_disabled", 0.0
    if not market.has_position or market.average_entry <= 0:
        return False, "no_position", 0.0
    if not _in_accumulation_phase(position):
        return False, "not_accumulation_phase", 0.0

    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)
    loss_min = float(cfg.get("loss_pct_min", -20))
    loss_max = float(cfg.get("loss_pct_max", -3))
    if loss_pct > loss_max or loss_pct < loss_min:
        return False, f"loss_pct {loss_pct:.1f}% outside [{loss_min}, {loss_max}]", 0.0
    if _near_stop_loss(loss_pct, strategy_params or {}, cfg):
        return False, "near_stop_loss", 0.0

    max_rounds = _effective_max_dca_rounds(position, cfg)
    rounds = int(position.get("dca_rounds", 0) or 0)
    if rounds >= max_rounds:
        return False, "max_rounds", 0.0

    interval_hours = float(cfg.get("interval_hours", 12))
    elapsed = _hours_since(position.get("last_dca_at"))
    if elapsed is not None and elapsed < interval_hours:
        return False, "interval", 0.0

    return True, None, loss_pct


def should_dca(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
) -> DCADecision:
    """Multi-factor DCA gate: hard accumulation rules, then optional scoring."""
    cfg = dca_config(strategy_params)
    ok, blocked_reason, loss_pct = _check_hard_gates(
        market, position, strategy_params, cfg
    )
    if not ok:
        return DCADecision(should_dca=False, blocked_reason=blocked_reason)

    scoring_cfg = dict(cfg.get("scoring") or {})
    if scoring_cfg.get("enabled", False):
        decision = _evaluate_scoring(market, loss_pct, cfg, strategy_params)
        if not decision.should_dca:
            return decision
        rounds = int(position.get("dca_rounds", 0) or 0)
        max_rounds = int(cfg.get("max_rounds", 3))
        decision.blocked_reason = None
        return decision

    fixed_usdt = float(cfg.get("fixed_usdt", 20))
    mode = str(cfg.get("mode", "shadow"))
    return DCADecision(
        should_dca=True,
        score=0,
        usdt_amount=fixed_usdt,
        shadow_only=mode == "shadow",
    )


def evaluate_dca_addon(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
) -> DCACandidate | None:
    """Return a BUY_DCA candidate when accumulation and scoring rules pass."""
    decision = should_dca(market, position, strategy_params)
    if not decision.should_dca:
        return None

    cfg = dca_config(strategy_params)
    rounds = int(position.get("dca_rounds", 0) or 0)
    max_rounds = _effective_max_dca_rounds(position, cfg)
    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)

    if decision.score > 0:
        core = {k: v for k, v in decision.breakdown.items() if k != "bb_support" and v > 0}
        rationale = (
            f"DCA score {decision.score}/{decision.max_score} "
            f"loss {loss_pct:.1f}% round {rounds + 1}/{max_rounds} "
            f"[{', '.join(f'{k}={v}' for k, v in core.items())}]"
        )
    else:
        rationale = f"DCA dip {loss_pct:.1f}% (round {rounds + 1}/{max_rounds})"

    return DCACandidate(
        action=BUY_DCA,
        source="dca",
        rationale=rationale,
        usdt_amount=decision.usdt_amount,
        shadow_only=decision.shadow_only,
        score=decision.score,
        breakdown=dict(decision.breakdown),
    )