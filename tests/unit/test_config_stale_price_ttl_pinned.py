"""#415 — the live-quote freshness knobs are pinned in repo-root ``config.json``.

``stale_price_max_age_sec`` and ``gate_ticker_snapshot_ttl_sec`` bound how old a
quote may be before it is refused / how long a Gate ticker snapshot is reused.
Both used to exist only as code defaults in ``core/config.py``; an operator
lowering the visible ``price_cache_ttl_sec`` would not have bounded quote age.

These tests assert the keys are *present* under ``architecture`` — they do not
pin the numeric values. The repo ``config.json`` is only read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _architecture_block() -> dict:
    raw = json.loads((_ROOT / "config.json").read_text(encoding="utf-8"))
    block = raw.get("architecture")
    assert isinstance(block, dict), "config.json must have an 'architecture' object"
    return block


@pytest.mark.parametrize(
    "key",
    ["stale_price_max_age_sec", "gate_ticker_snapshot_ttl_sec"],
)
def test_live_quote_freshness_key_is_pinned_in_config(key: str) -> None:
    block = _architecture_block()
    assert key in block, f"architecture.{key} missing from config.json (#415)"
    value = block[key]
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert value > 0
