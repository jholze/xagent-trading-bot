from core.models import SandboxMetrics
from hermes.goals import GoalEngine
from hermes.validation import WalkForwardResult


def test_opportunity_score_can_promote_without_sharpe_lead():
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