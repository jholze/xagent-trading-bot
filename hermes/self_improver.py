import json
from pathlib import Path

from core.config import get_bot_config
from hermes.experiment import ExperimentRunner
from hermes.memory import store
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
        recent = store.recent_experiments(5)
        prompt_template = _load_prompt("propose_experiment.txt")
        if not prompt_template:
            proposal = self.runner.propose(params)
            return {
                "variable": proposal.variable,
                "old_value": proposal.old_value,
                "new_value": proposal.new_value,
                "hypothesis": proposal.hypothesis,
            }

        prompt = prompt_template.format(
            baseline_params=json.dumps(params, indent=2),
            recent_experiments=json.dumps(recent, indent=2),
            tunable_params=json.dumps(self.runner.tunable_params),
            symbol=baseline.get("symbol", "ARIA/USDT"),
            timeframe=baseline.get("timeframe", "4h"),
        )
        try:
            from grok_agent import ask_grok_json
            response = ask_grok_json(prompt)
            cleaned = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            if data.get("variable") and "new_value" in data:
                return data
        except Exception as e:
            log(f"Grok experiment proposal unavailable, using heuristic: {e}", "WARNING")

        proposal = self.runner.propose(params)
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
        if promoted:
            pattern = (
                f"Changing {proposal.variable} from {proposal.old_value} to {proposal.new_value} "
                f"improved Sharpe from {baseline_metrics.get('sharpe', 0)} to "
                f"{variant_metrics.get('sharpe', 0)} on {symbol} {timeframe}"
            )
            confidence = min(0.9, 0.5 + abs(variant_metrics.get("sharpe", 0) - baseline_metrics.get("sharpe", 0)) * 0.2)
        else:
            pattern = (
                f"Changing {proposal.variable} from {proposal.old_value} to {proposal.new_value} "
                f"did NOT improve Sharpe on {symbol} {timeframe} ({variant_metrics.get('sharpe', 0)} vs "
                f"{baseline_metrics.get('sharpe', 0)})"
            )
            confidence = 0.4

        skill = {
            "pattern": pattern,
            "confidence": round(confidence, 2),
            "applies_to": {"symbol": symbol, "timeframe": timeframe},
            "variable": proposal.variable,
            "promoted": promoted,
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
                from grok_agent import ask_grok_json
                response = ask_grok_json(grok_prompt)
                cleaned = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                data = json.loads(cleaned)
                if data.get("pattern"):
                    skill["pattern"] = data["pattern"]
                if "confidence" in data:
                    skill["confidence"] = float(data["confidence"])
            except Exception:
                pass

        return store.append_skill(skill)

    def analyze_and_suggest(self, experiment_record: dict) -> str:
        """Human-readable summary of last experiment."""
        v = experiment_record.get("verdict", "unknown")
        var = experiment_record.get("variable", "?")
        reason = experiment_record.get("verdict_reason", "")
        bm = experiment_record.get("baseline_metrics", {})
        vm = experiment_record.get("variant_metrics", {})
        return (
            f"Experiment {experiment_record.get('id')}: {var} "
            f"{experiment_record.get('old_value')}→{experiment_record.get('new_value')} → {v}. "
            f"Sharpe {bm.get('sharpe', 0)}→{vm.get('sharpe', 0)}. {reason}"
        )