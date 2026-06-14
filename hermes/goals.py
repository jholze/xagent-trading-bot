from dataclasses import dataclass

from core.config import get_bot_config
from core.models import SandboxMetrics
from hermes.live_evidence import LiveMetrics
from hermes.validation import WalkForwardResult

DUAL_EXIT_PARAMS = frozenset({
    "take_profit_pct",
    "rsi_sell_30",
    "rsi_sell_20",
    "cmc_trust_score",
    "cmc_min_confidence",
})


@dataclass
class Verdict:
    promoted: bool
    reason: str
    baseline_better: bool
    meets_success_criteria: bool
    live_veto: bool = False


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

    @property
    def live_evidence(self) -> dict:
        return self.hermes.get("live_evidence", {})

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

    def apply_live_evidence(
        self,
        verdict: Verdict,
        live_metrics: LiveMetrics | None,
    ) -> Verdict:
        """Guardrail: veto WF promotion when dry-run ledger strongly disagrees."""
        le = self.live_evidence
        if not le.get("enabled", False):
            return verdict
        if live_metrics is None:
            return verdict

        min_trades = int(le.get("min_live_trades", 3))
        min_sells = int(le.get("min_live_sell_trades", 2))
        max_loss = float(le.get("live_max_loss_usdt", 10))
        live_suffix = (
            f" | live {live_metrics.lookback_days}d: "
            f"sell_pnl={live_metrics.live_sell_pnl:+.2f} "
            f"({live_metrics.live_sell_trades} sells)"
        )

        has_enough = (
            live_metrics.live_trades >= min_trades
            and live_metrics.live_sell_trades >= min_sells
        )

        if not has_enough:
            if live_metrics.live_trades > 0:
                return Verdict(
                    promoted=verdict.promoted,
                    reason=verdict.reason + live_suffix + " (insufficient live sample)",
                    baseline_better=verdict.baseline_better,
                    meets_success_criteria=verdict.meets_success_criteria,
                    live_veto=False,
                )
            return verdict

        if verdict.promoted and live_metrics.live_sell_pnl < -max_loss:
            return Verdict(
                promoted=False,
                reason=(
                    f"Live veto: sell_pnl={live_metrics.live_sell_pnl:.2f} "
                    f"< -{max_loss:.0f} USDT{live_suffix}"
                ),
                baseline_better=True,
                meets_success_criteria=False,
                live_veto=True,
            )

        return Verdict(
            promoted=verdict.promoted,
            reason=verdict.reason + live_suffix,
            baseline_better=verdict.baseline_better,
            meets_success_criteria=verdict.meets_success_criteria,
            live_veto=False,
        )

    def _live_suffix(self, live_metrics: LiveMetrics) -> str:
        return (
            f" | live {live_metrics.lookback_days}d: "
            f"sell_pnl={live_metrics.live_sell_pnl:+.2f} "
            f"({live_metrics.live_sell_trades} sells)"
        )

    def _try_dual_promote(
        self,
        wf_verdict: Verdict,
        live_metrics: LiveMetrics | None,
        cf_result,
        variable: str,
        variant_metrics: dict,
    ) -> Verdict | None:
        """Path B: promote via live + counterfactual when WF did not promote."""
        le = self.live_evidence
        if le.get("mode") != "dual":
            return None
        if wf_verdict.promoted:
            return None
        if live_metrics is None or cf_result is None:
            return None

        min_trades = int(le.get("min_live_trades", 3))
        min_sells = int(le.get("min_live_sell_trades", 2))
        min_cf_sells = int(le.get("min_counterfactual_sells", 1))
        min_delta = float(le.get("min_live_pnl_delta_usdt", 5))
        blocklist = set(le.get("live_blocklist") or [])

        if live_metrics.live_trades < min_trades or live_metrics.live_sell_trades < min_sells:
            return None
        if live_metrics.live_sell_pnl < 0:
            return None
        if variable in blocklist:
            return None
        if le.get("dual_exit_params_only", True) and variable not in DUAL_EXIT_PARAMS:
            return None
        if le.get("require_cf_seeded", True) and not cf_result.seeded:
            return Verdict(
                promoted=False,
                reason="Dual blocked: not seeded",
                baseline_better=True,
                meets_success_criteria=False,
            )
        if cf_result.variant_sells < min_cf_sells:
            return Verdict(
                promoted=False,
                reason=f"Dual blocked: variant_sells={cf_result.variant_sells}",
                baseline_better=True,
                meets_success_criteria=False,
            )
        if cf_result.pnl_delta <= 0:
            return Verdict(
                promoted=False,
                reason=f"Dual blocked: cf_delta={cf_result.pnl_delta:+.2f}",
                baseline_better=True,
                meets_success_criteria=False,
            )
        if cf_result.pnl_delta < min_delta and not self.meets_success_criteria(variant_metrics):
            return Verdict(
                promoted=False,
                reason=(
                    f"Dual blocked: cf_delta={cf_result.pnl_delta:+.2f} "
                    f"< {min_delta:.0f} USDT"
                ),
                baseline_better=True,
                meets_success_criteria=False,
            )

        seed = cf_result.seed_source or "?"
        return Verdict(
            promoted=True,
            reason=(
                f"Dual promote: cf_delta={cf_result.pnl_delta:+.2f} USDT"
                f"{self._live_suffix(live_metrics)}"
                f" | seeded {seed} | variant_sells={cf_result.variant_sells}"
            ),
            baseline_better=False,
            meets_success_criteria=self.meets_success_criteria(variant_metrics),
        )

    def evaluate_with_live_and_counterfactual(
        self,
        wf_verdict: Verdict,
        live_metrics: LiveMetrics | None,
        cf_result,
        variable: str,
        variant_metrics: dict,
    ) -> Verdict:
        """Apply guardrail/dual live evidence on top of walk-forward verdict."""
        le = self.live_evidence
        if not le.get("enabled", False):
            return wf_verdict

        verdict = self.apply_live_evidence(wf_verdict, live_metrics)
        if verdict.promoted:
            return verdict

        dual = self._try_dual_promote(verdict, live_metrics, cf_result, variable, variant_metrics)
        if dual is not None:
            if dual.promoted:
                return dual
            if dual.reason.startswith("Dual blocked"):
                return Verdict(
                    promoted=False,
                    reason=verdict.reason + " | " + dual.reason,
                    baseline_better=dual.baseline_better,
                    meets_success_criteria=dual.meets_success_criteria,
                )
        return verdict