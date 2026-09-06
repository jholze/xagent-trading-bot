from dataclasses import dataclass, replace

from core.config import get_bot_config
from core.models import SandboxMetrics
from hermes.live_evidence import LiveMetrics
from hermes.significance import (
    block_bootstrap_win_probability,
    fold_sharpe_deltas,
    format_win_probability,
    tightened_threshold,
    total_in_sample_trades,
)
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
    inconclusive: bool = False
    win_probability: float | None = None
    total_trades: int | None = None
    threshold_used: float | None = None

    @property
    def label(self) -> str:
        if self.promoted:
            return "promoted"
        if self.inconclusive:
            return "inconclusive"
        return "rejected"


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

    def _zero_trade_inconclusive(
        self,
        baseline: SandboxMetrics | dict,
        variant: SandboxMetrics | dict,
    ) -> Verdict | None:
        """trades=0 on both sides is not evidence — never rejected (#308)."""
        if int(self._metric(baseline, "trades")) != 0:
            return None
        if int(self._metric(variant, "trades")) != 0:
            return None
        return Verdict(
            promoted=False,
            reason="Inconclusive: baseline and variant both have 0 trades",
            baseline_better=False,
            meets_success_criteria=False,
            inconclusive=True,
        )

    def evaluate(self, baseline: SandboxMetrics | dict, variant: SandboxMetrics | dict) -> Verdict:
        zero = self._zero_trade_inconclusive(baseline, variant)
        if zero is not None:
            return zero

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

    def _holdout_check(
        self,
        baseline: WalkForwardResult,
        variant: WalkForwardResult,
    ) -> Verdict | None:
        """Reject when hold-out Sharpe/DD fails. Reason always starts with ``holdout``."""
        holdout_b = list(getattr(baseline, "holdout_metrics", None) or [])
        holdout_v = list(getattr(variant, "holdout_metrics", None) or [])
        if not holdout_b and not holdout_v:
            return None
        if not holdout_b or not holdout_v:
            return Verdict(
                promoted=False,
                reason="holdout: missing baseline or variant hold-out folds",
                baseline_better=True,
                meets_success_criteria=False,
            )
        scored_b = [f for f in holdout_b if not f.get("excluded")]
        scored_v = [f for f in holdout_v if not f.get("excluded")]
        if not scored_b or not scored_v:
            return Verdict(
                promoted=False,
                reason="holdout: no scored hold-out folds",
                baseline_better=True,
                meets_success_criteria=False,
            )
        b_sh = sum(float(f.get("sharpe") or 0) for f in scored_b) / len(scored_b)
        v_sh = sum(float(f.get("sharpe") or 0) for f in scored_v) / len(scored_v)
        b_dd = max(float(f.get("max_drawdown_pct") or 0) for f in scored_b)
        v_dd = max(float(f.get("max_drawdown_pct") or 0) for f in scored_v)
        dd_tol = float(self.validation.get("holdout_dd_tolerance_pct", 2.0))
        if v_sh - b_sh < 0:
            return Verdict(
                promoted=False,
                reason=f"holdout Sharpe delta {v_sh - b_sh:.2f} < 0",
                baseline_better=True,
                meets_success_criteria=False,
            )
        if v_dd > b_dd + dd_tol:
            return Verdict(
                promoted=False,
                reason=(
                    f"holdout max_drawdown {v_dd:.1f} > baseline {b_dd:.1f} "
                    f"+ {dd_tol:g}"
                ),
                baseline_better=True,
                meets_success_criteria=False,
            )
        return None

    def _significance_check(
        self,
        baseline: WalkForwardResult,
        variant: WalkForwardResult,
        *,
        n_variables_today: int = 1,
    ) -> tuple[float, int, float, Verdict | None]:
        """Win-probability + min-trades gate on in-sample fold Sharpe deltas."""
        min_p = float(self.validation.get("min_win_probability", 0.95))
        min_trades = int(self.validation.get("min_total_trades", 30))
        threshold = tightened_threshold(min_p, n_variables_today)
        b_trades = total_in_sample_trades(baseline.fold_metrics)
        v_trades = total_in_sample_trades(variant.fold_metrics)
        total_trades = min(b_trades, v_trades)
        deltas = fold_sharpe_deltas(baseline.fold_metrics, variant.fold_metrics)
        win_p = block_bootstrap_win_probability(deltas)
        if b_trades < min_trades or v_trades < min_trades:
            return win_p, total_trades, threshold, Verdict(
                promoted=False,
                reason=(
                    f"min_total_trades {total_trades} < {min_trades} "
                    f"(baseline={b_trades} variant={v_trades})"
                ),
                baseline_better=True,
                meets_success_criteria=False,
                win_probability=win_p,
                total_trades=total_trades,
                threshold_used=threshold,
            )
        if win_p < threshold:
            return win_p, total_trades, threshold, Verdict(
                promoted=False,
                reason=(
                    f"win_probability {win_p:.2f} < threshold {threshold:.2f} "
                    f"({format_win_probability(win_p, total_trades)})"
                ),
                baseline_better=True,
                meets_success_criteria=False,
                win_probability=win_p,
                total_trades=total_trades,
                threshold_used=threshold,
            )
        return win_p, total_trades, threshold, None

    def evaluate_walk_forward(
        self,
        baseline: WalkForwardResult,
        variant: WalkForwardResult,
        *,
        n_variables_today: int = 1,
    ) -> Verdict:
        vcfg = self.validation
        min_ratio = float(vcfg.get("min_folds_won_ratio", 0.6))
        primary = self.primary_metric
        min_trades_per_fold = int(vcfg.get("min_trades_per_fold", 1))

        zero = self._zero_trade_inconclusive(baseline.aggregate, variant.aggregate)
        if zero is not None:
            return zero

        if variant.folds_total == 0 or baseline.folds_total == 0:
            return Verdict(
                promoted=False,
                reason="No valid walk-forward folds",
                baseline_better=True,
                meets_success_criteria=False,
            )

        excluded = int(getattr(variant, "folds_excluded", 0) or 0)
        scored = variant.folds_total - excluded
        if scored <= 0:
            return Verdict(
                promoted=False,
                reason=(
                    f"Inconclusive: no folds with enough trades "
                    f"(min_trades_per_fold={min_trades_per_fold}, "
                    f"{excluded} excluded)"
                ),
                baseline_better=False,
                meets_success_criteria=False,
                inconclusive=True,
            )

        win_ratio = variant.folds_won / scored
        b_agg = baseline.aggregate
        v_agg = variant.aggregate
        b_val = self._metric(b_agg, primary)
        v_val = self._metric(v_agg, primary)

        for b_fold, v_fold in zip(baseline.fold_metrics, variant.fold_metrics):
            if b_fold.get("fold_id") != v_fold.get("fold_id"):
                continue
            if v_fold.get("excluded") or b_fold.get("excluded"):
                continue
            b_tr = int(b_fold.get("trades") or 0)
            v_tr = int(v_fold.get("trades") or 0)
            if b_tr < min_trades_per_fold or v_tr < min_trades_per_fold:
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
            excl_note = f", {excluded} excluded" if excluded else ""
            return Verdict(
                promoted=False,
                reason=(
                    f"Won {variant.folds_won}/{scored} folds "
                    f"({win_ratio:.0%} < {min_ratio:.0%}){excl_note}"
                ),
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

        holdout_fail = self._holdout_check(baseline, variant)
        if holdout_fail is not None:
            return holdout_fail

        win_p = None
        total_trades = None
        threshold = None
        has_holdout = bool(getattr(variant, "holdout_metrics", None) or getattr(variant, "folds_holdout", 0))
        if has_holdout:
            win_p, total_trades, threshold, sig_fail = self._significance_check(
                baseline, variant, n_variables_today=n_variables_today,
            )
            if sig_fail is not None:
                return sig_fail

        excl_note = f", {excluded} excluded" if excluded else ""
        holdout_note = ", hold-out ok" if has_holdout else ""
        sig_note = ""
        if win_p is not None and total_trades is not None:
            sig_note = f", {format_win_probability(win_p, total_trades)}"
        return Verdict(
            promoted=True,
            reason=(
                f"Won {variant.folds_won}/{scored} folds{excl_note}, {improve_reason}, "
                f"opp={self._metric(v_agg, 'opportunity_score'):.2f}"
                f"{holdout_note}{sig_note}"
            ),
            baseline_better=False,
            meets_success_criteria=True,
            win_probability=win_p,
            total_trades=total_trades,
            threshold_used=threshold,
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
                return replace(
                    verdict,
                    reason=verdict.reason + live_suffix + " (insufficient live sample)",
                    live_veto=False,
                )
            return verdict

        if verdict.promoted and live_metrics.live_sell_pnl < -max_loss:
            return replace(
                verdict,
                promoted=False,
                reason=(
                    f"Live veto: sell_pnl={live_metrics.live_sell_pnl:.2f} "
                    f"< -{max_loss:.0f} USDT{live_suffix}"
                ),
                baseline_better=True,
                meets_success_criteria=False,
                live_veto=True,
                inconclusive=False,
            )

        return replace(
            verdict,
            reason=verdict.reason + live_suffix,
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
        if cf_result.pnl_delta < min_delta:
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
        if wf_verdict.inconclusive:
            return wf_verdict

        verdict = self.apply_live_evidence(wf_verdict, live_metrics)
        if verdict.promoted:
            return verdict

        dual = self._try_dual_promote(verdict, live_metrics, cf_result, variable, variant_metrics)
        if dual is not None:
            if dual.promoted:
                return dual
            if dual.inconclusive:
                return dual
            if dual.reason.startswith("Dual blocked"):
                return replace(
                    verdict,
                    promoted=False,
                    reason=verdict.reason + " | " + dual.reason,
                    baseline_better=dual.baseline_better,
                    meets_success_criteria=dual.meets_success_criteria,
                )
        return verdict