from dataclasses import dataclass

from core.config import get_bot_config
from core.models import SandboxMetrics


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
    def primary_metric(self) -> str:
        return self.hermes.get("primary_metric", "sharpe")

    def _metric(self, metrics: SandboxMetrics | dict, key: str) -> float:
        if isinstance(metrics, dict):
            return float(metrics.get(key, 0))
        return float(getattr(metrics, key, 0))

    def meets_success_criteria(self, metrics: SandboxMetrics | dict) -> bool:
        s = self.success
        if self._metric(metrics, "sharpe") < s.get("min_sharpe", 0.8):
            return False
        if self._metric(metrics, "max_drawdown_pct") > s.get("max_drawdown_pct", 15):
            return False
        if self._metric(metrics, "win_rate") < s.get("min_win_rate", 50):
            return False
        if self._metric(metrics, "trades") < s.get("min_trades", 5):
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
            return Verdict(
                promoted=True,
                reason=f"Variant {primary} {v_val:.2f} > baseline {b_val:.2f}",
                baseline_better=False,
                meets_success_criteria=meets,
            )

        return Verdict(
            promoted=False,
            reason=f"Variant {primary} {v_val:.2f} <= baseline {b_val:.2f}",
            baseline_better=True,
            meets_success_criteria=self.meets_success_criteria(baseline),
        )