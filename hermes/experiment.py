import copy
import random
from dataclasses import dataclass

from core.config import get_bot_config
from hermes.memory import store


@dataclass
class ExperimentProposal:
    variable: str
    old_value: float | str
    new_value: float | str
    params: dict
    hypothesis: str
    source: str = "heuristic"


BUY_REGIMES = ("dip", "reversal", "both")

MUTATION_STEPS = {
    "rsi_buy_low": 2,
    "rsi_buy_high": 2,
    "rsi_sell_30": 2,
    "rsi_sell_20": 2,
    "volume_multiplier": 0.1,
    "stop_loss_pct": 1.0,
    "reversal_rsi_cross_low": 2,
    "reversal_rsi_cross_high": 2,
    "reversal_volume_multiplier": 0.1,
    "cmc_trust_score": 5.0,
    "cmc_min_confidence": 3.0,
}

BOUNDS = {
    "rsi_buy_low": (15, 40),
    "rsi_buy_high": (35, 60),
    "rsi_sell_30": (55, 80),
    "rsi_sell_20": (65, 95),
    "volume_multiplier": (1.0, 2.5),
    "stop_loss_pct": (5.0, 25.0),
    "reversal_rsi_cross_low": (25, 38),
    "reversal_rsi_cross_high": (35, 48),
    "reversal_volume_multiplier": (1.0, 2.0),
    "cmc_trust_score": (55.0, 85.0),
    "cmc_min_confidence": (45.0, 65.0),
}


class ExperimentRunner:
    """Scientific method: mutate exactly one strategy parameter per cycle."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()
        self.hermes = self.config.hermes_config

    @property
    def tunable_params(self) -> list[str]:
        return self.hermes.get("tunable_params", list(MUTATION_STEPS.keys()))

    def _recent_variables(self, limit: int = 10) -> set[str]:
        return {e.get("variable") for e in store.recent_experiments(limit) if e.get("variable")}

    def _pick_variable(self, baseline_params: dict, symbol: str = "", timeframe: str = "") -> str:
        recent = self._recent_variables()
        refuted = store.refuted_variables(symbol, timeframe) if symbol else set()
        skills = store.relevant_skills(symbol, timeframe, min_confidence=0.5) if symbol else []
        avoid = set()
        for skill in skills:
            if not skill.get("promoted") and skill.get("variable"):
                avoid.add(skill["variable"])

        candidates = [
            p for p in self.tunable_params
            if p not in recent and p not in refuted and p not in avoid
        ]
        if not candidates:
            candidates = [p for p in self.tunable_params if p not in avoid]
        if not candidates:
            candidates = list(self.tunable_params)
        return random.choice(candidates)

    def _mutate_regime(self, old_value: str) -> str:
        current = old_value if old_value in BUY_REGIMES else "dip"
        options = [r for r in BUY_REGIMES if r != current]
        return random.choice(options) if options else current

    def _mutate(self, variable: str, old_value: float) -> float:
        step = MUTATION_STEPS.get(variable, 1)
        low, high = BOUNDS.get(variable, (old_value - step, old_value + step))
        direction = random.choice([-1, 1])
        new_value = old_value + direction * step
        new_value = max(low, min(high, new_value))
        int_vars = (
            "rsi_buy_low", "rsi_buy_high", "rsi_sell_30", "rsi_sell_20",
            "reversal_rsi_cross_low", "reversal_rsi_cross_high",
        )
        if variable in int_vars:
            new_value = int(round(new_value))
        else:
            new_value = round(new_value, 2)
        if new_value == old_value:
            new_value = min(high, old_value + step) if direction > 0 else max(low, old_value - step)
        return new_value

    def propose(
        self,
        baseline_params: dict,
        grok_proposal: dict | None = None,
        symbol: str = "",
        timeframe: str = "",
    ) -> ExperimentProposal:
        if grok_proposal and grok_proposal.get("variable") in self.tunable_params:
            variable = grok_proposal["variable"]
            if variable == "buy_regime":
                old_value = str(baseline_params.get("buy_regime", "dip"))
                new_value = str(grok_proposal.get("new_value", old_value))
                if new_value not in BUY_REGIMES:
                    new_value = self._mutate_regime(old_value)
            else:
                old_value = float(baseline_params.get(variable, grok_proposal.get("old_value", 0)))
                new_value = float(grok_proposal.get("new_value", old_value))
                low, high = BOUNDS.get(variable, (new_value, new_value))
                new_value = max(low, min(high, new_value))
                int_vars = (
                    "rsi_buy_low", "rsi_buy_high", "rsi_sell_30", "rsi_sell_20",
                    "reversal_rsi_cross_low", "reversal_rsi_cross_high",
                )
                if variable in int_vars:
                    new_value = int(round(new_value))
                else:
                    new_value = round(new_value, 2)
            params = copy.deepcopy(baseline_params)
            params[variable] = new_value
            return ExperimentProposal(
                variable=variable,
                old_value=old_value,
                new_value=new_value,
                params=params,
                hypothesis=grok_proposal.get("hypothesis", f"Adjust {variable} from {old_value} to {new_value}"),
                source=grok_proposal.get("source", "grok"),
            )

        variable = self._pick_variable(baseline_params, symbol, timeframe)
        if variable == "buy_regime":
            old_value = str(baseline_params.get("buy_regime", "dip"))
            new_value = self._mutate_regime(old_value)
        else:
            old_value = float(baseline_params.get(variable, MUTATION_STEPS.get(variable, 1)))
            new_value = self._mutate(variable, old_value)
        params = copy.deepcopy(baseline_params)
        params[variable] = new_value
        return ExperimentProposal(
            variable=variable,
            old_value=old_value,
            new_value=new_value,
            params=params,
            hypothesis=f"If {variable} changes from {old_value} to {new_value}, Sharpe ratio improves.",
            source="heuristic",
        )

    def record(
        self,
        proposal: ExperimentProposal,
        baseline_metrics: dict,
        variant_metrics: dict,
        verdict_promoted: bool,
        verdict_reason: str,
        symbol: str,
        timeframe: str,
        validation_mode: str = "full",
        fold_metrics: list = None,
        baseline_fold_metrics: list = None,
        folds_won: int = 0,
        folds_total: int = 0,
    ) -> dict:
        return store.append_experiment({
            "variable": proposal.variable,
            "old_value": proposal.old_value,
            "new_value": proposal.new_value,
            "hypothesis": proposal.hypothesis,
            "source": proposal.source,
            "symbol": symbol,
            "timeframe": timeframe,
            "baseline_metrics": baseline_metrics,
            "variant_metrics": variant_metrics,
            "verdict": "promoted" if verdict_promoted else "rejected",
            "verdict_reason": verdict_reason,
            "validation_mode": validation_mode,
            "fold_metrics": fold_metrics or [],
            "baseline_fold_metrics": baseline_fold_metrics or [],
            "folds_won": folds_won,
            "folds_total": folds_total,
        })