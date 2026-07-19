"""Unified DCA — multi-factor scoring for open losers (full or partial positions)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.actions import BUY_DCA
from core.config import get_bot_config
from core.models import MarketContext
from strategies.positions import position_notional_usdt


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
    """True when no partial exit-ladder sell has occurred yet."""
    step = int(position.get("exit_ladder_step", 0) or 0)
    sold = float(position.get("sold_percent", 0) or 0)
    return step == 0 and sold < 0.01


def in_recovery_phase(position: dict) -> bool:
    """Alias: position has partial sells but may still DCA under unified rules."""
    return not _in_accumulation_phase(position)


def _tail_gate_config(cfg: dict) -> dict:
    """Tail / cascade limits (formerly dca.recovery); top-level keys override nested."""
    rec = dict(cfg.get("recovery") or {})
    return {
        "max_sold_percent": float(cfg.get("max_sold_percent", rec.get("max_sold_percent", 0.85))),
        "min_remainder_usdt": float(cfg.get("min_remainder_usdt", rec.get("min_remainder_usdt", 150.0))),
        "cascade_min_drop_pct": float(cfg.get("cascade_min_drop_pct", rec.get("cascade_min_drop_pct", 4.0))),
        "cascade_score_discount": int(cfg.get("cascade_score_discount", rec.get("cascade_score_discount", 1))),
    }


def _total_dca_rounds(position: dict) -> int:
    return int(position.get("dca_rounds", 0) or 0) + int(position.get("dca_recovery_rounds", 0) or 0)


def _hours_since_last_dca(position: dict) -> float | None:
    elapsed: list[float] = []
    for ts_key in ("last_dca_at", "last_dca_recovery_at"):
        hours = _hours_since(position.get(ts_key))
        if hours is not None:
            elapsed.append(hours)
    return min(elapsed) if elapsed else None


def _cascade_ref_price(position: dict, entry: float) -> float:
    for key in ("last_recovery_ref_price", "last_buy_price", "average_entry"):
        val = float(position.get(key, 0) or 0)
        if val > 0:
            return val
    return entry


def _cascade_active(position: dict, current_price: float, entry: float, cfg: dict) -> bool:
    ref = _cascade_ref_price(position, entry)
    if ref <= 0 or current_price <= 0:
        return False
    drop_pct = (1.0 - (current_price / ref)) * 100.0
    tail = _tail_gate_config(cfg)
    return drop_pct >= float(tail.get("cascade_min_drop_pct", 4.0))


def _apply_cascade_scoring(decision: DCADecision, cfg: dict) -> DCADecision:
    scoring = dict(cfg.get("scoring") or {})
    tail = _tail_gate_config(cfg)
    discount = int(tail.get("cascade_score_discount", 1))
    min_score = int(scoring.get("min_score", 6)) - discount
    min_core = int(scoring.get("min_core_criteria_met", 3)) - discount
    core_keys = ("atr_distance", "rsi", "funding", "btc_underperf")
    core_met = sum(1 for k in core_keys if decision.breakdown.get(k, 0) > 0)
    passed = decision.score >= min_score and core_met >= min_core
    if not passed:
        decision.should_dca = False
        decision.blocked_reason = (
            f"cascade score {decision.score}/{decision.max_score} "
            f"(core {core_met}/{min_core}, need {min_score}): {decision.breakdown}"
        )
        return decision
    decision.should_dca = True
    decision.blocked_reason = None
    return decision


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
    recovery_rounds = int(position.get("dca_recovery_rounds", 0) or 0)
    total_dca_rounds = dca_rounds + recovery_rounds
    widen = float(dca_cfg.get("stop_loss_widen_pct_per_round", 0))
    full_stop = stop_loss_pct + total_dca_rounds * widen

    grace_hours = float(dca_cfg.get("grace_hours_after_dca", 0))
    if grace_hours <= 0:
        grace_hours = float(dca_cfg.get("interval_hours", 12))

    grace_elapsed: float | None = None
    for ts_key in ("last_dca_recovery_at", "last_dca_at"):
        elapsed = _hours_since(position.get(ts_key))
        if elapsed is not None and (grace_elapsed is None or elapsed < grace_elapsed):
            grace_elapsed = elapsed
    in_grace = (
        total_dca_rounds > 0
        and grace_elapsed is not None
        and grace_elapsed < grace_hours
    )

    if dca_cfg.get("pause_partial_stop_during_dca", True) and total_dca_rounds > 0:
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


def _position_notional_for_sizing(position: dict | None, market: MarketContext | None = None) -> float:
    if not position:
        return 0.0
    amount = float(position.get("amount", 0) or 0)
    if amount <= 0:
        return 0.0
    if market and float(market.current_price or 0) > 0:
        return amount * float(market.current_price)
    return position_notional_usdt(position)


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
    position: dict | None = None,
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

    from strategies.dca_sizing import compute_dca_usdt

    rounds = _total_dca_rounds(position or {})
    usdt_amount = compute_dca_usdt(
        base_usdt=fixed_usdt,
        score=total_score,
        max_score=max_score,
        min_score=min_score,
        loss_pct=loss_pct,
        round_index=rounds,
        max_rounds=int(cfg.get("max_rounds", 4)),
        dca_cfg=cfg,
        position_notional_usdt=_position_notional_for_sizing(position, market),
    ) if passed else fixed_usdt

    return DCADecision(
        should_dca=passed,
        score=total_score,
        max_score=max_score,
        breakdown=breakdown,
        blocked_reason=reason,
        usdt_amount=usdt_amount,
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

    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)
    if loss_pct >= 0:
        return False, "gain_positive", 0.0

    tail = _tail_gate_config(cfg)
    sold = float(position.get("sold_percent", 0) or 0)
    if sold >= float(tail["max_sold_percent"]):
        return False, f"sold>={tail['max_sold_percent']:.0%}", 0.0

    remainder = _position_notional_for_sizing(position, market)
    if remainder < float(tail["min_remainder_usdt"]):
        return False, f"remainder<{tail['min_remainder_usdt']:.0f}", 0.0

    loss_min = float(cfg.get("loss_pct_min", -25))
    loss_max = float(cfg.get("loss_pct_max", -3))
    if loss_pct < loss_min or loss_pct > loss_max:
        return False, f"loss_pct {loss_pct:.1f}% outside [{loss_min}, {loss_max}]", 0.0
    if _near_stop_loss(loss_pct, strategy_params or {}, cfg):
        return False, "near_stop_loss", 0.0

    max_rounds = _effective_max_dca_rounds(position, cfg)
    if _total_dca_rounds(position) >= max_rounds:
        return False, "max_rounds", 0.0

    interval_hours = float(cfg.get("interval_hours", 12))
    elapsed = _hours_since_last_dca(position)
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
        decision = _evaluate_scoring(market, loss_pct, cfg, strategy_params, position)
        if not decision.should_dca and _cascade_active(
            position, market.current_price, market.average_entry, cfg,
        ):
            decision = _apply_cascade_scoring(decision, cfg)
        if not decision.should_dca:
            return decision
        decision.blocked_reason = None
        return decision

    fixed_usdt = float(cfg.get("fixed_usdt", 20))
    mode = str(cfg.get("mode", "shadow"))
    from strategies.dca_sizing import compute_dca_usdt

    rounds = _total_dca_rounds(position)
    usdt_amount = compute_dca_usdt(
        base_usdt=fixed_usdt,
        score=0,
        max_score=int(scoring_cfg.get("max_score", 10)),
        min_score=int(scoring_cfg.get("min_score", 6)),
        loss_pct=loss_pct,
        round_index=rounds,
        max_rounds=int(cfg.get("max_rounds", 4)),
        dca_cfg=cfg,
        position_notional_usdt=_position_notional_for_sizing(position, market),
    )
    return DCADecision(
        should_dca=True,
        score=0,
        usdt_amount=usdt_amount,
        shadow_only=mode == "shadow",
    )


def evaluate_dca_addon(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
) -> DCACandidate | None:
    """Return a BUY_DCA candidate when unified loss-band and scoring rules pass.

    Optional policy layer (plans/dca-policy-v1.md / #79 D2–D3).
    """
    decision = should_dca(market, position, strategy_params)
    if not decision.should_dca:
        return None

    cfg = dca_config(strategy_params)
    rounds = _total_dca_rounds(position)
    max_rounds = _effective_max_dca_rounds(position, cfg)
    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)
    sold = float(position.get("sold_percent", 0) or 0)
    sold_tag = f" sold {sold:.0%}" if sold >= 0.01 else ""

    if decision.score > 0:
        core = {k: v for k, v in decision.breakdown.items() if k != "bb_support" and v > 0}
        rationale = (
            f"DCA score {decision.score}/{decision.max_score} "
            f"loss {loss_pct:.1f}%{sold_tag} round {rounds + 1}/{max_rounds} "
            f"[{', '.join(f'{k}={v}' for k, v in core.items())}]"
        )
    else:
        rationale = f"DCA dip {loss_pct:.1f}%{sold_tag} (round {rounds + 1}/{max_rounds})"

    usdt_amount = float(decision.usdt_amount or 0)
    breakdown = dict(decision.breakdown)

    # Policy layer — fail-open to base candidate
    try:
        from strategies.dca_context import build_dca_context
        from strategies.dca_policy import (
            apply_policy_to_usdt,
            dca_policy_config,
            evaluate_dca_policy,
        )

        pcfg = dca_policy_config(cfg)
        if pcfg.get("enabled"):
            sym = str(
                (position or {}).get("symbol")
                or getattr(market, "symbol", "")
                or ""
            )
            ctx = build_dca_context(
                symbol=sym,
                position=position,
                market=market,
                strategy_params=strategy_params,
                score=int(decision.score or 0),
                max_score=int(decision.max_score or 10),
                loss_pct=loss_pct,
            )
            result = evaluate_dca_policy(ctx, pcfg)
            shadow = bool(pcfg.get("shadow", True))
            codes = ",".join(result.reason_codes) if result.reason_codes else "-"
            rationale = (
                f"{rationale} policy[v{result.policy_version} "
                f"{'shadow ' if shadow else ''}"
                f"mult={result.size_mult} skip={result.skip} {codes}]"
            )
            breakdown["policy_mult"] = result.size_mult
            breakdown["policy_skip"] = 1 if result.skip else 0
            base_before = usdt_amount
            if result.skip and not shadow:
                from strategies.dca_policy import emit_dca_policy_audit

                emit_dca_policy_audit(
                    symbol=sym,
                    result=result,
                    ctx=ctx,
                    shadow=shadow,
                    base_usdt=base_before,
                    final_usdt=0.0,
                    applied="live_skip",
                    policy_cfg=pcfg,
                )
                return None
            usdt_amount = apply_policy_to_usdt(
                usdt_amount,
                result,
                spendable_dca=ctx.spendable_dca,
                shadow=shadow,
            )
            from strategies.dca_policy import emit_dca_policy_audit

            emit_dca_policy_audit(
                symbol=sym,
                result=result,
                ctx=ctx,
                shadow=shadow,
                base_usdt=base_before,
                final_usdt=usdt_amount,
                applied="shadow" if shadow else "live",
                policy_cfg=pcfg,
            )
    except Exception:
        pass

    return DCACandidate(
        action=BUY_DCA,
        source="dca",
        rationale=rationale,
        usdt_amount=usdt_amount,
        shadow_only=decision.shadow_only,
        score=decision.score,
        breakdown=breakdown,
    )

