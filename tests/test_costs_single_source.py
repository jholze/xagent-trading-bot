"""Grep: slippage_percent / fee_rate / assumed_fee_pct live only in core/costs.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRS = (
    "core",
    "strategies",
    "services",
    "risk",
    "hermes",
    "intelligence",
    "execution",
)
PATTERN = r"slippage_percent|fee_rate\b|assumed_fee_pct"


def test_cost_constants_single_source():
    result = subprocess.run(
        [
            "grep",
            "-RInE",
            PATTERN,
            "--include=*.py",
            *DIRS,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    leftovers = [
        line
        for line in (result.stdout or "").splitlines()
        if "core/costs.py" not in line
    ]
    assert leftovers == [], "leftover cost constants:\n" + "\n".join(leftovers)
