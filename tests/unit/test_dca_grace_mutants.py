"""The grace mutants from the #733 re-review die against the in-tree tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_dca_grace_mutants_are_killed():
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_dca_grace_mutants.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for mutant in (
        "a1_remove_fallback",
        "m4i_future_timestamp_inside",
        "m5_grace_end_le",
        "m7_full_stop_ge",
        "f1_engine_repause_outside_grace",
    ):
        assert f"{mutant}: KILLED" in proc.stdout
