import time
from dataclasses import dataclass

from core.config import get_bot_config
from hermes.backtester import Backtester
from hermes.experiment import ExperimentRunner
from hermes.goals import GoalEngine
from hermes.memory import store
from hermes.self_improver import SelfImprover
from hermes.validation import run_walk_forward
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
        self.config.refresh()
        self.hermes = self.config.hermes_config
        baseline = store.init_baseline_from_config(self.config)
        symbol = baseline.get("symbol", self.hermes.get("symbols", ["ARIA/USDT"])[0])
        timeframe = baseline.get("timeframe", self.hermes.get("timeframes", ["4h"])[0])
        params = baseline.get("params", {})

        grok_proposal = self.improver.propose_experiment(baseline)
        proposal = self.experiments.propose(params, grok_proposal, symbol, timeframe)

        log(
            f"Hermes experiment: {proposal.variable} {proposal.old_value}→{proposal.new_value} "
            f"({proposal.source})",
            "INFO",
        )

        vcfg = self.hermes.get("validation", {})
        days = int(vcfg.get("backtest_days", self.hermes.get("backtest_days", 35)))
        ohlcv_df = self.backtester._fetch_ohlcv(symbol, timeframe, days)

        validation_mode = vcfg.get("mode", "walk_forward")
        if validation_mode == "walk_forward" and ohlcv_df is not None and not ohlcv_df.empty:
            wf_base = run_walk_forward(self.backtester, symbol, timeframe, params, ohlcv_df, self.hermes)
            wf_var = run_walk_forward(
                self.backtester, symbol, timeframe, proposal.params, ohlcv_df, self.hermes,
                baseline_folds=wf_base.fold_metrics,
            )
            base_m = wf_base.aggregate.__dict__
            var_m = wf_var.aggregate.__dict__
            verdict = self.goals.evaluate_walk_forward(wf_base, wf_var)
            record = self.experiments.record(
                proposal=proposal,
                baseline_metrics=base_m,
                variant_metrics=var_m,
                verdict_promoted=verdict.promoted,
                verdict_reason=verdict.reason,
                symbol=symbol,
                timeframe=timeframe,
                validation_mode="walk_forward",
                fold_metrics=wf_var.fold_metrics,
                baseline_fold_metrics=wf_base.fold_metrics,
                folds_won=wf_var.folds_won,
                folds_total=wf_var.folds_total,
            )
        else:
            bt_base = self.backtester.run(symbol, timeframe, params, ohlcv_df=ohlcv_df)
            bt_var = self.backtester.run(symbol, timeframe, proposal.params, ohlcv_df=ohlcv_df)
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
                validation_mode="full",
            )

        if verdict.promoted:
            baseline["params"] = proposal.params
            baseline["metrics"] = var_m
            store.save_baseline(baseline)
            log(f"Hermes baseline updated: {proposal.variable}={proposal.new_value}", "INFO")
            self._sync_to_config(baseline, record.get("id", ""))
            self._notify_promotion(record, proposal, var_m)

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

    def _sync_to_config(self, baseline: dict, experiment_id: str):
        if not self.hermes.get("sync_to_config", True):
            return
        try:
            from strategies.registry import sync_hermes_baseline_to_config
            ok, msg = sync_hermes_baseline_to_config(baseline, experiment_id)
            log(f"Hermes config sync: {msg}", "INFO" if ok else "WARNING")
        except Exception as e:
            log(f"Hermes config sync failed: {e}", "ERROR")

    def _notify_promotion(self, record: dict, proposal, metrics: dict):
        if not self.hermes.get("notify_on_promotion", True):
            return
        try:
            from telegram_notifier import send_telegram_message
            folds = ""
            if record.get("folds_total"):
                folds = f"\nFolds: {record.get('folds_won')}/{record.get('folds_total')}"
            send_telegram_message(
                f"🧠 <b>Hermes promoted</b>\n"
                f"{proposal.variable}: {proposal.old_value}→{proposal.new_value}\n"
                f"Sharpe: {metrics.get('sharpe', 0)} | WR: {metrics.get('win_rate', 0)}% | "
                f"DD: {metrics.get('max_drawdown_pct', 0)}%{folds}\n"
                f"Experiment: {record.get('id', '')}"
            )
        except Exception as e:
            log(f"Hermes promotion notify failed: {e}", "WARNING")

    def run_loop(self, interval_sec: int | None = None):
        interval = interval_sec or int(self.hermes.get("cycle_interval_sec", 1800))
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
            fold_info = ""
            if exp.get("folds_total"):
                fold_info = f" [{exp.get('folds_won')}/{exp.get('folds_total')} folds]"
            lines.append(
                f"  • {exp.get('id')}: {exp.get('variable')} "
                f"{exp.get('old_value')}→{exp.get('new_value')} → {exp.get('verdict')}{fold_info}"
            )
        if skills:
            lines.append("")
            lines.append("Recent skills:")
            for s in skills:
                ev = s.get("evidence_count", 1)
                lines.append(f"  • [{s.get('confidence', 0)} x{ev}] {s.get('pattern', '')[:80]}")
        return "\n".join(lines)