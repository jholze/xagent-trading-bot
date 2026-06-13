import json
from pathlib import Path

from core.config import get_bot_config
from hermes.experiment import ExperimentRunner
from hermes.memory import store
from intelligence.grok_json import GrokError, ask_grok_json
from logger import log

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


class SelfImprover:
    """Analyze cycle results and propose next experiments via Grok or heuristics."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()
        self.runner = ExperimentRunner(self.config)

    def propose_experiment(self, baseline: dict) -> dict:
        params = baseline.get("params", {})
        symbol = baseline.get("symbol", "ARIA/USDT")
        timeframe = baseline.get("timeframe", "4h")
        recent = store.recent_experiments(5)
        skills = store.relevant_skills(symbol, timeframe, limit=8)
        prompt_template = _load_prompt("propose_experiment.txt")

        if prompt_template:
            prompt = prompt_template.format(
                baseline_params=json.dumps(params, indent=2),
                recent_experiments=json.dumps(recent, indent=2),
                recent_skills=json.dumps(skills, indent=2),
                tunable_params=json.dumps(self.runner.tunable_params),
                symbol=symbol,
                timeframe=timeframe,
            )
            try:
                data = ask_grok_json(
                    prompt,
                    required_keys=["variable", "new_value"],
                )
                data["source"] = "grok"
                return data
            except GrokError as e:
                log(f"Grok experiment proposal unavailable, using heuristic: {e}", "WARNING")

        proposal = self.runner.propose(params, symbol=symbol, timeframe=timeframe)
        return {
            "variable": proposal.variable,
            "old_value": proposal.old_value,
            "new_value": proposal.new_value,
            "hypothesis": proposal.hypothesis,
            "source": proposal.source,
        }

    def extract_skill(
        self,
        proposal,
        baseline_metrics: dict,
        variant_metrics: dict,
        promoted: bool,
        symbol: str,
        timeframe: str,
    ) -> dict | None:
        regime = proposal.params.get("buy_regime", "dip")
        opp_b = baseline_metrics.get("opportunity_score", 0)
        opp_v = variant_metrics.get("opportunity_score", 0)
        if promoted:
            pattern = (
                f"{proposal.variable} {proposal.old_value}→{proposal.new_value} on {symbol} {timeframe} "
                f"(regime={regime}): Sharpe {baseline_metrics.get('sharpe', 0)}→"
                f"{variant_metrics.get('sharpe', 0)}, opp {opp_b}→{opp_v}"
            )
            delta = max(
                abs(variant_metrics.get("sharpe", 0) - baseline_metrics.get("sharpe", 0)),
                abs(opp_v - opp_b),
            )
            confidence = min(0.9, 0.5 + delta * 0.2)
        else:
            pattern = (
                f"{proposal.variable} {proposal.old_value}→{proposal.new_value} failed on {symbol} "
                f"{timeframe} (regime={regime}): Sharpe {variant_metrics.get('sharpe', 0)} vs "
                f"{baseline_metrics.get('sharpe', 0)}, opp {opp_v} vs {opp_b}"
            )
            confidence = 0.4

        skill = {
            "pattern": pattern,
            "confidence": round(confidence, 2),
            "applies_to": {"symbol": symbol, "timeframe": timeframe},
            "variable": proposal.variable,
            "regime": regime,
            "promoted": promoted,
            "grok_enhanced": False,
        }

        prompt = _load_prompt("analyze_cycle.txt")
        if prompt:
            grok_prompt = prompt.format(
                proposal_variable=proposal.variable,
                old_value=proposal.old_value,
                new_value=proposal.new_value,
                baseline_metrics=json.dumps(baseline_metrics),
                variant_metrics=json.dumps(variant_metrics),
                promoted=promoted,
            )
            try:
                data = ask_grok_json(grok_prompt, required_keys=["pattern"])
                if data.get("pattern"):
                    skill["pattern"] = data["pattern"]
                if "confidence" in data:
                    skill["confidence"] = float(data["confidence"])
                skill["grok_enhanced"] = True
            except GrokError as e:
                log(f"Grok skill extraction failed, using template: {e}", "WARNING")

        return store.upsert_skill(skill, proposal.old_value, proposal.new_value)

    def analyze_and_suggest(self, experiment_record: dict) -> str:
        v = experiment_record.get("verdict", "unknown")
        var = experiment_record.get("variable", "?")
        reason = experiment_record.get("verdict_reason", "")
        bm = experiment_record.get("baseline_metrics", {})
        vm = experiment_record.get("variant_metrics", {})
        folds = experiment_record.get("folds_won")
        folds_total = experiment_record.get("folds_total")
        fold_part = ""
        if folds is not None and folds_total:
            fold_part = f" Folds {folds}/{folds_total}."
        return (
            f"Experiment {experiment_record.get('id')}: [{experiment_record.get('symbol')}] {var} "
            f"{experiment_record.get('old_value')}→{experiment_record.get('new_value')} → {v}. "
            f"Sharpe {bm.get('sharpe', 0)}→{vm.get('sharpe', 0)}, "
            f"opp {bm.get('opportunity_score', 0)}→{vm.get('opportunity_score', 0)}.{fold_part} {reason}"
        )