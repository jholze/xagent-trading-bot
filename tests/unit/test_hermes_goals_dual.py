from core.models import SandboxMetrics
from hermes.counterfactual import CounterfactualResult
from hermes.goals import GoalEngine, Verdict
from hermes.live_evidence import LiveMetrics
from hermes.validation import WalkForwardResult


def _hermes_dual():
    from core.config import BotConfig

    raw = BotConfig().raw
    raw["hermes"]["live_evidence"] = {
        "enabled": True,
        "mode": "dual",
        "counterfactual_enabled": True,
        "lookback_days": 7,
        "min_live_trades": 3,
        "min_live_sell_trades": 2,
        "live_max_loss_usdt": 10,
        "min_live_pnl_delta_usdt": 5,
        "min_counterfactual_sells": 1,
        "require_cf_seeded": True,
        "dual_exit_params_only": True,
        "live_blocklist": ["stop_loss_pct"],
    }
    return GoalEngine(BotConfig(raw))


def _rejected_wf() -> Verdict:
    return Verdict(
        promoted=False,
        reason="Won 0/4 folds",
        baseline_better=True,
        meets_success_criteria=False,
    )


def _live_h() -> LiveMetrics:
    return LiveMetrics(
        symbol="H/USDT",
        live_trades=3,
        live_sell_trades=2,
        live_sell_pnl=45.0,
        lookback_days=7,
    )


def _cf_positive() -> CounterfactualResult:
    return CounterfactualResult(
        baseline_pnl=10.0,
        variant_pnl=18.0,
        pnl_delta=8.0,
        baseline_sells=1,
        variant_sells=2,
        seeded=True,
        seed_source="manual",
        window_start=__import__("datetime").datetime(2026, 6, 7, tzinfo=__import__("datetime").timezone.utc),
        window_end=__import__("datetime").datetime(2026, 6, 14, tzinfo=__import__("datetime").timezone.utc),
    )


def test_dual_promotes_take_profit_on_positive_cf():
    goals = _hermes_dual()
    result = goals.evaluate_with_live_and_counterfactual(
        _rejected_wf(),
        _live_h(),
        _cf_positive(),
        "take_profit_pct",
        {"trades": 2, "sharpe": 0.1, "opportunity_score": 0.1, "trade_quality": 0.5,
         "win_rate": 50, "max_drawdown_pct": 5},
    )
    assert result.promoted is True
    assert "Dual promote" in result.reason


def test_dual_blocks_buy_param():
    goals = _hermes_dual()
    result = goals.evaluate_with_live_and_counterfactual(
        _rejected_wf(),
        _live_h(),
        _cf_positive(),
        "rsi_buy_low",
        {"trades": 0},
    )
    assert result.promoted is False


def test_dual_blocks_stop_loss():
    goals = _hermes_dual()
    result = goals.evaluate_with_live_and_counterfactual(
        _rejected_wf(),
        _live_h(),
        _cf_positive(),
        "stop_loss_pct",
        {"trades": 2},
    )
    assert result.promoted is False


def test_dual_blocks_unseeded():
    goals = _hermes_dual()
    cf = _cf_positive()
    cf.seeded = False
    result = goals.evaluate_with_live_and_counterfactual(
        _rejected_wf(),
        _live_h(),
        cf,
        "take_profit_pct",
        {"trades": 2},
    )
    assert result.promoted is False
    assert "not seeded" in result.reason


def test_dual_blocks_small_cf_delta():
    goals = _hermes_dual()
    cf = _cf_positive()
    cf.pnl_delta = 1.0
    cf.variant_pnl = 11.0
    result = goals.evaluate_with_live_and_counterfactual(
        _rejected_wf(),
        _live_h(),
        cf,
        "take_profit_pct",
        {"trades": 0, "sharpe": 0, "opportunity_score": 0, "trade_quality": 0,
         "win_rate": 0, "max_drawdown_pct": 0},
    )
    assert result.promoted is False
    assert "Dual blocked" in result.reason