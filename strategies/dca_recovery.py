"""Backward-compatible aliases — unified DCA lives in strategies.dca."""

from __future__ import annotations

from strategies.dca import (
    DCADecision,
    DCACandidate,
    dca_enabled,
    evaluate_dca_addon,
    in_recovery_phase,
    should_dca,
)


def recovery_config(strategy_params: dict | None) -> dict:
    """Legacy: return merged tail-gate slice for callers that still import this."""
    from strategies.dca import dca_config, _tail_gate_config

    cfg = dca_config(strategy_params)
    tail = _tail_gate_config(cfg)
    rec = dict(cfg.get("recovery") or {})
    return {**rec, **tail, "enabled": dca_enabled(strategy_params)}


def recovery_enabled(strategy_params: dict | None) -> bool:
    return dca_enabled(strategy_params)


def recovery_usdt_amount(
    cfg: dict,
    strategy_params: dict | None,
    *,
    score: int = 0,
    loss_pct: float = 0.0,
    round_index: int = 0,
    position: dict | None = None,
    market=None,
) -> float:
    from strategies.dca import _position_notional_for_sizing, dca_config
    from strategies.dca_sizing import compute_dca_usdt

    dca = dca_config(strategy_params)
    scoring = dict(dca.get("scoring") or {})
    return compute_dca_usdt(
        base_usdt=float(dca.get("fixed_usdt", 20)),
        score=score,
        max_score=int(scoring.get("max_score", 10)),
        min_score=int(scoring.get("min_score", 6)),
        loss_pct=loss_pct,
        round_index=round_index,
        max_rounds=int(dca.get("max_rounds", 4)),
        dca_cfg=dca,
        position_notional_usdt=_position_notional_for_sizing(position, market),
    )


def should_dca_recovery(market, position: dict, strategy_params: dict | None) -> DCADecision:
    return should_dca(market, position, strategy_params)


def evaluate_dca_recovery(market, position: dict, strategy_params: dict | None) -> DCACandidate | None:
    return evaluate_dca_addon(market, position, strategy_params)