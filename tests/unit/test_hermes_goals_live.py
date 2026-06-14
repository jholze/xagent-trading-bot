from core.models import SandboxMetrics
from hermes.goals import GoalEngine, Verdict
from hermes.live_evidence import LiveMetrics
from hermes.validation import WalkForwardResult


def _promoted_verdict() -> Verdict:
    return Verdict(
        promoted=True,
        reason="Won 3/4 folds",
        baseline_better=False,
        meets_success_criteria=True,
    )


def _hermes_with_live(enabled=True):
    from core.config import BotConfig

    raw = BotConfig().raw
    raw["hermes"]["live_evidence"] = {
        "enabled": enabled,
        "lookback_days": 7,
        "min_live_trades": 3,
        "min_live_sell_trades": 2,
        "live_max_loss_usdt": 10,
    }
    return GoalEngine(BotConfig(raw))


def test_live_veto_blocks_promotion_on_negative_ledger():
    goals = _hermes_with_live()
    metrics = LiveMetrics(
        symbol="ARIA/USDT",
        live_trades=4,
        live_sell_trades=3,
        live_sell_pnl=-16.0,
    )
    result = goals.apply_live_evidence(_promoted_verdict(), metrics)
    assert result.promoted is False
    assert result.live_veto is True
    assert "Live veto" in result.reason


def test_positive_ledger_allows_promotion():
    goals = _hermes_with_live()
    metrics = LiveMetrics(
        symbol="H/USDT",
        live_trades=3,
        live_sell_trades=2,
        live_sell_pnl=42.0,
    )
    result = goals.apply_live_evidence(_promoted_verdict(), metrics)
    assert result.promoted is True
    assert result.live_veto is False


def test_insufficient_trades_ignores_veto():
    goals = _hermes_with_live()
    metrics = LiveMetrics(symbol="XPL/USDT", live_trades=1, live_sell_trades=0)
    result = goals.apply_live_evidence(_promoted_verdict(), metrics)
    assert result.promoted is True
    assert result.live_veto is False


def test_live_evidence_disabled_passthrough():
    goals = _hermes_with_live(enabled=False)
    metrics = LiveMetrics(symbol="ARIA/USDT", live_trades=5, live_sell_trades=3, live_sell_pnl=-20)
    result = goals.apply_live_evidence(_promoted_verdict(), metrics)
    assert result.promoted is True


def test_walk_forward_regression_still_works():
    goals = GoalEngine()
    base = WalkForwardResult(
        symbol="T", timeframe="4h", params={},
        fold_metrics=[{"fold_id": i, "sharpe": 0.3, "max_drawdown_pct": 5, "trades": 2} for i in range(10)],
        aggregate=SandboxMetrics(
            sharpe=0.3, max_drawdown_pct=5, win_rate=55, trades=5,
            opportunity_score=0.1, trade_quality=0.5,
        ),
        folds_total=10, folds_won=0,
    )
    var = WalkForwardResult(
        symbol="T", timeframe="4h", params={},
        fold_metrics=[{"fold_id": i, "sharpe": 0.35, "max_drawdown_pct": 5, "trades": 3} for i in range(10)],
        aggregate=SandboxMetrics(
            sharpe=0.35, max_drawdown_pct=8, win_rate=55, trades=6,
            opportunity_score=0.25, trade_quality=0.8,
        ),
        folds_total=10, folds_won=7,
    )
    verdict = goals.evaluate_walk_forward(base, var)
    assert verdict.promoted is True