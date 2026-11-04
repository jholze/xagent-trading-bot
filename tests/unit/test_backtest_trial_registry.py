"""The DSR trial count N has one source: backtest/harness/trials.py."""

import re
from pathlib import Path

from backtest.harness import longwf, report, scenarios, trials

HARNESS = Path(trials.__file__).resolve().parent


def test_every_scenario_is_registered():
    missing = sorted(set(scenarios.SCENARIOS) - set(trials.SCENARIO_TRIALS))
    assert not missing, f"register these scenarios in backtest/harness/trials.py: {missing}"


def test_no_stale_registrations():
    stale = sorted(set(trials.SCENARIO_TRIALS) - set(scenarios.SCENARIOS))
    assert not stale, f"registered but not in SCENARIOS: {stale}"


def test_counts_are_sane():
    for key, t in {**trials.SCENARIO_TRIALS, **trials.LONGWF_TRIALS}.items():
        assert isinstance(t.trials, int) and t.trials >= 0, key
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", t.added), key


def test_all_modules_read_the_register():
    assert scenarios.N_TRIALS == trials.n_scenario_trials()
    assert report.N_TRIALS == trials.n_scenario_trials()
    assert longwf.NEW_TRIALS == trials.n_longwf_trials()
    assert longwf.N_ALL == trials.n_all_trials() == trials.n_scenario_trials() + trials.n_longwf_trials()


# Re-runs of an existing candidate set (key suffix _oct or _rerun) add no new trial; every other
# scenario must count at least one variant (a 0 would understate N).
RERUN_KEYS = {k for k, t in trials.SCENARIO_TRIALS.items() if k.endswith(("_oct", "_rerun"))}


def test_only_reruns_count_zero():
    for key, t in trials.SCENARIO_TRIALS.items():
        if key in RERUN_KEYS:
            assert t.trials == 0, key
        else:
            assert t.trials >= 1, f"{key} registers no trial"


def test_known_totals_at_introduction():
    # 12 (a-d) + 11 (e-h) + 27 (r_*) + 5 (k) + 5 (m, #767) = 60; long walk-forward adds 8.
    # Floor, not equality: N only grows when scenarios are added.
    assert trials.n_scenario_trials() >= 60
    assert trials.n_all_trials() >= 68
    assert trials.n_longwf_trials() == 8


def test_no_hand_written_trial_counts_left():
    pat = re.compile(r"^\s*(N_TRIALS|N_ALL|NEW_TRIALS)\s*=\s*\d", re.M)
    for path in HARNESS.glob("*.py"):
        assert not pat.search(path.read_text()), f"hand-written trial count in {path.name}"
