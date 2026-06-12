from dataclasses import dataclass

from core.config import get_bot_config
from core.models import SandboxMetrics
from hermes.validation import WalkForwardResult


@dataclass
class Verdict:
    promoted: bool
    reason: str
    baseline_better: bool
    meets_success_criteria: bool


class GoalEngine:
    """Evaluate backtest results against Hermes success/failure criteria."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()
        self.hermes = self.config.hermes_config

    @property
    def success(self) -> dict:
        return self.hermes.get("success_criteria", {})

    @property
    def failure(self) -> dict:
        return self.hermes.get("failure_criteria", {})

    @property
    def validation(self) -> dict:
        return self.hermes.get("validation", {})

    @property
    def primary_metric(self) -> str:
        return self.hermes.get("primary_metric", "sharpe")

    def _metric(self, metrics: SandboxMetrics | dict, key: str) -> float:
        if isinstance(metrics, dict):
            return float(metrics.get(key, 0))
        return float(getattr(metrics, key, 0))

    def meets_success_criteria(self, metrics: SandboxMetrics | dict, aggregate_trades: bool = False) -> bool:
        s = self.success
        v = self.validation
        min_trades = int(v.get("min_trades_aggregate", s.get("min_trades", 5))) if aggregate_trades else int(
            s.get("min_trades", 5)
        )
        if self._metric(metrics, "sharpe") < s.get("min_sharpe", 0.8):
            return False
        if self._metric(metrics, "max_drawdown_pct") > s.get("max_drawdown_pct", 15):
            return False
        if self._metric(metrics, "win_rate") < s.get("min_win_rate", 50):
            return False
        if self._metric(metrics, "trades") < min_trades:
            return False
        return True

    def evaluate(self, baseline: SandboxMetrics | dict, variant: SandboxMetrics | dict) -> Verdict:
        primary = self.primary_metric
        b_val = self._metric(baseline, primary)
        v_val = self._metric(variant, primary)

        sharpe_delta = v_val - b_val
        dd_delta = self._metric(variant, "max_drawdown_pct") - self._metric(baseline, "max_drawdown_pct")

        fail_sharpe = sharpe_delta < self.failure.get("sharpe_delta_max", -0.2)
        fail_dd = dd_delta > self.failure.get("drawdown_delta_max", 5)

        if fail_sharpe or fail_dd:
            reason = []
            if fail_sharpe:
                reason.append(f"{primary} delta {sharpe_delta:.2f} below limit")
            if fail_dd:
                reason.append(f"drawdown worsened by {dd_delta:.1f}%")
            return Verdict(
                promoted=False,
                reason="; ".join(reason),
                baseline_better=True,
                meets_success_criteria=False,
            )

        if v_val > b_val:
            meets = self.meets_success_criteria(variant)
            if meets:
                return Verdict(
                    promoted=True,
                    reason=f"Variant {primary} {v_val:.2f} > baseline {b_val:.2f} and meets success criteria",
                    baseline_better=False,
                    meets_success_criteria=True,
                )
            return Verdict(
                promoted=False,
                reason=f"Variant {primary} {v_val:.2f} > baseline {b_val:.2f} but below success criteria",
                baseline_better=False,
                meets_success_criteria=False,
            )

        return Verdict(
            promoted=False,
            reason=f"Variant {primary} {v_val:.2f} <= baseline {b_val:.2f}",
            baseline_better=True,
            meets_success_criteria=self.meets_success_criteria(baseline),
        )

    def evaluate_walk_forward(self, baseline: WalkForwardResult, variant: WalkForwardResult) -> Verdict:
        vcfg = self.validation
        min_ratio = float(vcfg.get("min_folds_won_ratio", 0.6))
        primary = self.primary_metric

        if variant.folds_total == 0 or baseline.folds_total == 0:
            return Verdict(
                promoted=False,
                reason="No valid walk-forward folds",
                baseline_better=True,
                meets_success_criteria=False,
            )

        win_ratio = variant.folds_won / variant.folds_total if variant.folds_total else 0
        b_agg = baseline.aggregate
        v_agg = variant.aggregate
        b_val = self._metric(b_agg, primary)
        v_val = self._metric(v_agg, primary)

        for b_fold, v_fold in zip(baseline.fold_metrics, variant.fold_metrics):
            if b_fold.get("fold_id") != v_fold.get("fold_id"):
                continue
            dd_delta = float(v_fold.get("max_drawdown_pct", 0)) - float(b_fold.get("max_drawdown_pct", 0))
            if dd_delta > self.failure.get("drawdown_delta_max", 5):
                return Verdict(
                    promoted=False,
                    reason=f"Fold {b_fold.get('fold_id')}: drawdown worsened by {dd_delta:.1f}%",
                    baseline_better=True,
                    meets_success_criteria=False,
                )

        if win_ratio < min_ratio:
            return Verdict(
                promoted=False,
                reason=f"Won {variant.folds_won}/{variant.folds_total} folds ({win_ratio:.0%} < {min_ratio:.0%})",
                baseline_better=True,
                meets_success_criteria=False,
            )

        if v_val <= b_val:
            return Verdict(
                promoted=False,
                reason=f"Aggregate {primary} {v_val:.2f} <= baseline {b_val:.2f}",
                baseline_better=True,
                meets_success_criteria=False,
            )

        if not self.meets_success_criteria(v_agg, aggregate_trades=True):
            return Verdict(
                promoted=False,
                reason=f"Aggregate metrics below success criteria ({primary}={v_val:.2f})",
                baseline_better=False,
                meets_success_criteria=False,
            )

        return Verdict(
            promoted=True,
            reason=(
                f"Won {variant.folds_won}/{variant.folds_total} folds, "
                f"aggregate {primary} {v_val:.2f} > {b_val:.2f}"
            ),
            baseline_better=False,
            meets_success_criteria=True,
        )