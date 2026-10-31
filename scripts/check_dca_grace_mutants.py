#!/usr/bin/env python3
"""Kill the grace mutants against the in-tree tests. Restores sources after.

Read-only toward git: each mutant is written, pytest runs, and the original
bytes are put back in a finally block. Exit 0 only when every mutant is killed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTANTS = (
    {
        "id": "a1_remove_fallback",
        "file": "strategies/dca.py",
        "old": (
            "    if bad:\n"
            "        _warn_bad_round_fields(pos, bad)\n"
            "        return 0\n"
        ),
        "new": (
            "    if bad:\n"
            "        _warn_bad_round_fields(pos, bad)\n"
            "        raise ValueError('bad rounds')\n"
        ),
        "tests": [
            "tests/unit/test_issue_733_grace_review.py::test_ac_a1_malformed_rounds_still_fire_full_stop",
        ],
    },
    {
        "id": "m4i_future_timestamp_inside",
        "file": "strategies/dca.py",
        "old": "    if age is None or age < 0:\n        return False\n",
        "new": "    if age is None:\n        return False\n",
        "tests": [
            "tests/unit/test_issue_733_grace_review.py::test_ac_a2_future_timestamp_is_outside_grace",
        ],
    },
    {
        "id": "m5_grace_end_le",
        "file": "strategies/dca.py",
        "old": "    return age < grace_window_hours(strategy_params)\n",
        "new": "    return age <= grace_window_hours(strategy_params)\n",
        "tests": [
            "tests/unit/test_issue_733_grace_review.py::test_ac_a3_age_equal_to_window_is_outside_grace",
            "tests/unit/test_issue_733_grace_review.py::test_f1_decision_engine_partial_pause_path",
        ],
    },
    {
        "id": "m7_full_stop_ge",
        "file": "strategies/dca.py",
        "old": (
            "    if loss_pct > full_stop:\n"
            '        return "SELL_STOP_FULL", "stop_loss"\n'
        ),
        "new": (
            "    if loss_pct >= full_stop:\n"
            '        return "SELL_STOP_FULL", "stop_loss"\n'
        ),
        "tests": [
            "tests/unit/test_issue_719_resolver_gap.py::test_ac_719_equal_loss_is_not_full_stop",
        ],
    },
    {
        "id": "f1_engine_repause_outside_grace",
        "file": "strategies/decision_engine.py",
        "old": (
            "                    skip_partial = should_pause_partial_stop(\n"
            "                        position,\n"
            "                        getattr(market, \"strategy_params\", None) if market else None,\n"
            "                    )\n"
        ),
        "new": (
            "                    skip_partial = should_pause_partial_stop(\n"
            "                        position,\n"
            "                        getattr(market, \"strategy_params\", None) if market else None,\n"
            "                    ) or int((position or {}).get(\"dca_rounds\") or 0) > 0\n"
        ),
        "tests": [
            "tests/unit/test_issue_733_grace_review.py::test_f1_decision_engine_partial_pause_path",
        ],
    },
)


def _run(tests: list[str]) -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=line",
        "-p",
        "no:randomly",
        *tests,
    ]
    env = os.environ.copy()
    env["PYTEST_DB_SUFFIX"] = "grace_mutants"
    env["UNIT_TEST_PROGRESS"] = "0"
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    only = set(argv or [])
    selected = [item for item in MUTANTS if not only or item["id"] in only]
    if only and len(selected) != len(only):
        known = ", ".join(item["id"] for item in MUTANTS)
        print(f"unknown mutant. known: {known}", file=sys.stderr)
        return 2
    results: list[tuple[str, str]] = []
    originals: dict[str, str] = {}
    try:
        for item in selected:
            path = ROOT / item["file"]
            originals.setdefault(item["file"], path.read_text(encoding="utf-8"))
            source = originals[item["file"]]
            if source.count(item["old"]) != 1:
                results.append((item["id"], "NOT_APPLIED"))
                continue
            path.write_text(source.replace(item["old"], item["new"], 1), encoding="utf-8")
            code = _run(item["tests"])
            path.write_text(source, encoding="utf-8")
            results.append((item["id"], "KILLED" if code != 0 else "SURVIVED"))
            print(f"{item['id']}: {results[-1][1]}")
    finally:
        for name, text in originals.items():
            (ROOT / name).write_text(text, encoding="utf-8")
    survived = [name for name, status in results if status != "KILLED"]
    if survived:
        print("survived: " + ", ".join(survived), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
