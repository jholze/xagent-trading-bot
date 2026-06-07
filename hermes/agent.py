import time
from dataclasses import dataclass

from core.config import get_bot_config
from hermes.backtester import Backtester
from hermes.experiment import ExperimentRunner
from hermes.goals import GoalEngine
from hermes.memory import store
from hermes.self_improver import SelfImprover
from logger import log


@dataclass
class CycleResult:
    experiment_id: str
    variable: str
    verdict: str
    promoted: bool
    baseline_sharpe: float
    variant_sharpe: float
    summary: str


class HermesAgent:
    """Self-improving trading agent: one variable, hypothesis, backtest, learn."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()
        self.hermes = self.config.hermes_config
        self.backtester = Backtester(self.config)
        self.goals = GoalEngine(self.config)
        self.experiments = ExperimentRunner(self.config)
        self.improver = SelfImprover(self.config)

    def run_cycle(self) -> CycleResult:
        baseline = store.init_baseline_from_config(self.config)
        symbol = baseline.get("symbol", self.hermes.get("symbols", ["ARIA/USDT"])[0])
        timeframe = baseline.get("timeframe", self.hermes.get("timeframes", ["4h"])[0])
        params = baseline.get("params", {})

        grok_proposal = self.improver.propose_experiment(baseline)
        proposal = self.experiments.propose(params, grok_proposal)

        log(
            f"Hermes experiment: {proposal.variable} {proposal.old_value}→{proposal.new_value} "
            f"({proposal.source})",
            "INFO",
        )

        bt_base = self.backtester.run(symbol, timeframe, params)
        bt_var = self.backtester.run(symbol, timeframe, proposal.params)

        base_m = bt_base.metrics.__dict__
        var_m = bt_var.metrics.__dict__
        verdict = self.goals.evaluate(bt_base.metrics, bt_var.metrics)

        record = self.experiments.record(
            proposal=proposal,
            baseline_metrics=base_m,
            variant_metrics=var_m,
            verdict_promoted=verdict.promoted,
            verdict_reason=verdict.reason,
            symbol=symbol,
            timeframe=timeframe,
        )

        if verdict.promoted:
            baseline["params"] = proposal.params
            baseline["metrics"] = var_m
            store.save_baseline(baseline)
            log(f"Hermes baseline updated: {proposal.variable}={proposal.new_value}", "INFO")

        self.improver.extract_skill(proposal, base_m, var_m, verdict.promoted, symbol, timeframe)
        summary = self.improver.analyze_and_suggest(record)

        return CycleResult(
            experiment_id=record.get("id", ""),
            variable=proposal.variable,
            verdict=record.get("verdict", "rejected"),
            promoted=verdict.promoted,
            baseline_sharpe=base_m.get("sharpe", 0),
            variant_sharpe=var_m.get("sharpe", 0),
            summary=summary,
        )

    def run_loop(self, interval_sec: int | None = None):
        interval = interval_sec or int(self.hermes.get("cycle_interval_sec", 3600))
        log(f"Hermes agent loop started (interval={interval}s)", "INFO")
        while True:
            try:
                result = self.run_cycle()
                log(result.summary, "INFO")
            except Exception as e:
                log(f"Hermes cycle error: {e}", "ERROR")
            time.sleep(interval)

    def status(self) -> str:
        baseline = store.load_baseline()
        recent = store.recent_experiments(5)
        skills = store.load_skills().get("skills", [])[-3:]
        lines = [
            "=== Hermes Agent Status ===",
            f"Baseline: {baseline.get('symbol')} {baseline.get('timeframe')}",
            f"Params: {baseline.get('params', {})}",
            f"Metrics: {baseline.get('metrics', {})}",
            f"Updated: {baseline.get('updated_at', 'never')}",
            "",
            f"Recent experiments ({len(recent)}):",
        ]
        for exp in recent:
            lines.append(
                f"  • {exp.get('id')}: {exp.get('variable')} "
                f"{exp.get('old_value')}→{exp.get('new_value')} → {exp.get('verdict')}"
            )
        if skills:
            lines.append("")
            lines.append("Recent skills:")
            for s in skills:
                lines.append(f"  • [{s.get('confidence', 0)}] {s.get('pattern', '')[:80]}")
        return "\n".join(lines)