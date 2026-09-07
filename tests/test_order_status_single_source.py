"""Grep: no leftover ``== "filled"`` / ``== "executing"`` / ``in ("filled"``
outside ``OrderStatus.from_legacy`` (core/models.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRS = (
    "services",
    "storage",
    "execution",
    "risk",
    "strategies",
)
PATTERN = r'== "filled"|== "executing"|in \("filled"'


def test_order_status_single_source():
    result = subprocess.run(
        ["grep", "-rn", PATTERN, *DIRS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    leftovers = [
        line
        for line in (result.stdout or "").splitlines()
        if "OrderStatus.from_legacy" not in line
    ]
    assert leftovers == [], "leftover status string compares:\n" + "\n".join(leftovers)
