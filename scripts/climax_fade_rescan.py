#!/usr/bin/env python3
"""Weekly climax-fade rescan (same formula as strategies.climax_fade).

Unit tests use the synthetic fixture (no network). A live run may fetch Gate 4h
OHLCV; print holdout-style PF vs baseline 1.21 / n=43.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from strategies.climax_fade import (  # noqa: E402
    climax_fade_config,
    replay_climax_fade,
)

BASELINE_PF = 1.21
BASELINE_N = 43
EXCLUDE = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT")


def rescan_synthetic(fixture_path: str | Path) -> dict:
    """Offline: load the unit fixture, run the same formula. Never hits the network."""
    path = Path(fixture_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    cfg = climax_fade_config({"shorts": {"climax_fade": {"enabled": True}}})
    replay = replay_climax_fade(
        data["closes"],
        data["volumes"],
        cfg=cfg,
        fee_bp_side=7.5,
        funding_rate_8h=0.0001,
        bar_hours=4.0,
    )
    n = int(replay.get("n_entries") or 0)
    pf_out = replay.get("pf") if n else None
    return {
        "n_entries": n,
        "n_covers": int(replay.get("n_covers") or 0),
        "pf": pf_out,
        "pnl_pct": replay.get("pnl_pct"),
        "cover_sources": list(replay.get("cover_sources") or []),
        "baseline_pf": BASELINE_PF,
        "baseline_n": BASELINE_N,
        "network": False,
        "exclude": list(EXCLUDE),
    }


def format_report(result: dict) -> str:
    pf = result.get("pf")
    pf_s = "n/a" if pf is None else ("inf" if pf == float("inf") else f"{pf:.3f}")
    return (
        f"climax-fade rescan: n={result.get('n_entries')} covers={result.get('n_covers')} "
        f"PF={pf_s} (baseline {BASELINE_PF} / n={BASELINE_N}) "
        f"network={result.get('network')}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--fixture",
        default=str(
            BOT_ROOT / "tests" / "unit" / "fixtures" / "climax_fade_synthetic_4h.json"
        ),
        help="Synthetic JSON (no network)",
    )
    args = p.parse_args(argv)
    out = rescan_synthetic(args.fixture)
    print(format_report(out))
    payload = {k: v for k, v in out.items() if k != "cover_sources"}
    payload["cover_sources"] = out.get("cover_sources")
    if payload.get("pf") == float("inf"):
        payload["pf"] = "inf"
    print(json.dumps(payload, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
