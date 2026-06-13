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

    def _metric(self, metrics: SandboxMetrics | dict, key: str, default: float = 0) -> float:
        if isinstance(metrics, dict):
            return float(metrics.get(key, default))
        return float(getattr(metrics, key, default))

    def meets_success_criteria(self, metrics: SandboxMetrics | dict, aggregate_trades: bool = False) -> bool:
        s = self.success
        v = self.validation
        min_trades = int(v.get("min_trades_aggregate", s.get("min_trades", 5))) if aggregate_trades else int(
            s.get("min_trades", 5)
        )
        trades = int(self._metric(metrics, "trades"))
        opp = self._metric(metrics, "opportunity_score")
        tq = self._metric(metrics, "trade_quality")
        min_opp = float(s.get("min_opportunity_score", 0))
        min_sharpe = float(s.get("min_sharpe", 0.8))

        if trades < min_trades:
            return False
        if self._metric(metrics, "max_drawdown_pct") > s.get("max_drawdown_pct", 15):
            return False
        if self._metric(metrics, "win_rate") < s.get("min_win_rate", 50):
            return False

        sharpe_ok = self._metric(metrics, "sharpe") >= min_sharpe
        opp_ok = min_opp > 0 and opp >= min_opp and tq > 0
        if not sharpe_ok and not opp_ok:
            return False
        return True

    def _variant_improved(
        self,
        baseline: SandboxMetrics | dict,
        variant: SandboxMetrics | dict,
    ) -> tuple[bool, str]:
        primary = self.primary_metric
        b_primary = self._metric(baseline, primary)
        v_primary = self._metric(variant, primary)
        opp_delta = self._metric(variant, "opportunity_score") - self._metric(baseline, "opportunity_score")
        min_opp_delta = float(self.validation.get("min_opportunity_delta", 0.05))

        if v_primary > b_primary:
            return True, f"{primary} {v_primary:.2f} > {b_primary:.2f}"
        if opp_delta >= min_opp_delta and self._metric(variant, "trade_quality") > 0:
            return True, f"opportunity_score +{opp_delta:.2f}"
        return False, f"{primary} {v_primary:.2f} <= {b_primary:.2f}"

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

        improved, improve_reason = self._variant_improved(baseline, variant)
        if improved:
            meets = self.meets_success_criteria(variant)
            if meets:
                return Verdict(
                    promoted=True,
                    reason=f"Variant improved ({improve_reason}) and meets success criteria",
                    baseline_better=False,
                    meets_success_criteria=True,
                )
            return Verdict(
                promoted=False,
                reason=f"Variant improved ({improve_reason}) but below success criteria",
                baseline_better=False,
                meets_success_criteria=False,
            )

        return Verdict(
            promoted=False,
            reason=f"Variant not improved ({improve_reason})",
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

        improved, improve_reason = self._variant_improved(b_agg, v_agg)
        if not improved:
            return Verdict(
                promoted=False,
                reason=f"Aggregate not improved ({improve_reason})",
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
                f"Won {variant.folds_won}/{variant.folds_total} folds, {improve_reason}, "
                f"opp={self._metric(v_agg, 'opportunity_score'):.2f}"
            ),
            baseline_better=False,
            meets_success_criteria=True,
        )