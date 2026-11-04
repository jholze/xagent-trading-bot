"""Central trial register for the deflated Sharpe ratio (DSR).

Single source of truth for N, the number of candidate variants the harness
has tested. Every harness module (scenarios, report, longwf) reads N from
here; nobody keeps a hand-written N_TRIALS any more.

Rules:
* Add an entry to ``SCENARIO_TRIALS`` in the same PR that adds a scenario to
  ``scenarios.SCENARIOS``. ``tests/unit/test_backtest_trial_registry.py``
  fails if a scenario is missing here (or registered but gone).
* ``trials`` = candidate variants that were evaluated against the baseline
  (the baseline itself is not a trial). Re-runs of the same candidates on a
  different book (``e_oct``, ``f_oct``) add 0.
* Never lower a count once a report was published with it: N only grows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Trial:
    trials: int
    added: str  # YYYY-MM-DD
    note: str = ""


# Scenario key in scenarios.SCENARIOS -> trials it contributes.
SCENARIO_TRIALS: dict[str, Trial] = {
    "a": Trial(3, "2026-11-01", "#733/#738 stop grace and escalation"),
    "b": Trial(3, "2026-11-01", "vol-target sizing"),
    "c": Trial(3, "2026-11-01", "SMA distance entry filter"),
    "d": Trial(3, "2026-11-01", "#740 symbol/source denylist"),
    "e": Trial(3, "2026-11-01", "early exit -8/-10/-12 % in 72 h"),
    "f": Trial(3, "2026-11-01", "flat stop -12/-15/-20 %, no adds"),
    "g": Trial(3, "2026-11-01", "no-chase 10/15/20 %"),
    "h": Trial(2, "2026-11-01", "early exit / flat stop + no-chase"),
    "e_oct": Trial(0, "2026-11-01", "same candidates as e on the October book"),
    "f_oct": Trial(0, "2026-11-01", "same candidates as f on the October book"),
    **{
        f"r_{rule}_{fam}": Trial(3, "2026-11-01", "regime-conditional loser-coin rule")
        for fam in ("oracle", "sma", "ret")
        for rule in ("e", "g", "h")
    },
    "k": Trial(5, "2026-11-02", "criteria eligibility E1/E2/G15"),
    "m": Trial(5, "2026-11-04", "pre-trade memory gate"),
}

# Variants of the long walk-forward study (backtest/harness/longwf.py).
LONGWF_TRIALS: dict[str, Trial] = {
    name: Trial(1, "2026-11-01", "longwf proxy variant")
    for name in ("a", "b", "c", "d", "e", "combo", "rg", "wf_selector")
}


def n_scenario_trials() -> int:
    """N for scenario runs (``python -m backtest.harness``)."""
    return sum(t.trials for t in SCENARIO_TRIALS.values())


def n_longwf_trials() -> int:
    return sum(t.trials for t in LONGWF_TRIALS.values())


def n_all_trials() -> int:
    """N over everything the harness ever tested (used by longwf)."""
    return n_scenario_trials() + n_longwf_trials()


def summary() -> str:
    return (
        f"DSR N = {n_all_trials()} ({n_scenario_trials()} scenario trials + "
        f"{n_longwf_trials()} long walk-forward trials, backtest/harness/trials.py)"
    )
