#!/usr/bin/env python3
"""Simulate one price cycle on open demo positions; assert zero position writes."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.ledger_sync import _build_positions_snapshot_from_orders
from strategies.positions import bootstrap_positions, update_market_snapshot


def main() -> int:
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("MONGODB_DB", "xagent_test")

    bootstrap_positions(scope="demo")
    snapshot = _build_positions_snapshot_from_orders("demo")
    open_keys = [
        k for k, v in snapshot.items() if float(v.get("amount", 0) or 0) > 1e-12
    ]

    save_calls = 0

    def _count_save(*_args, **_kwargs):
        nonlocal save_calls
        save_calls += 1
        return True

    lines = [
        f"open_positions={len(open_keys)}",
        "action=update_market_snapshot per open lot",
    ]

    with patch("strategies.positions.save_positions_document", side_effect=_count_save):
        for key in open_keys:
            symbol = key.rpartition("_")[0].replace("_", "/")
            tf = key.rpartition("_")[2] or "4h"
            update_market_snapshot(symbol, tf, 1.0)

    lines.append(f"position_writes={save_calls}")
    lines.append(f"zero_writes={save_calls == 0}")

    out = os.environ.get("CYCLE_WRITES_OUT")
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if save_calls == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())