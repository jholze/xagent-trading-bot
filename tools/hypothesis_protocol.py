"""Hypothesis kill rules for strategy evidence.

Encodes the shared scout / trial-ledger rules. It does not run a backtest.
A future strategy PR calls ``validate_payload`` on one evidence object, and
CI runs ``validate_directory`` on every JSON file in ``docs/evidence/``.

Numbers are the handover values. Nothing here is a new threshold.

Current cost model is trial-ledger s2 (2026-10-26), which replaced the
flat slippage lines:

- taker fee 0.2% per side
- slippage per side by class: stable 0.10%, other 0.20%, volatile 0.35%
- stress slippage = max(0.3%, class slippage)
- hard stress slippage = max(s1, class slippage)
- s1 = 0.5% per side for a small-cap alt, else 0.3% per side

The earlier brief line "hard stress 0.5%" is the small-cap s1 value, not a
flat rate for every name. s2 is the later pre-registration and is the rule
this module checks.

Status is PASS only when every check passes. CI treats any other status as
a failure. Severity, highest first: INVALID, KILL, SUSPICIOUS, UNCLEAR,
WITHDRAWN, PASS.

n_trades < 100 is UNCLEAR (s3: the top-10 result is not a pass; the scout
list also names fewer than 100 trades as a kill). Both mean not PASS.
Sharpe > 3 is SUSPICIOUS (scout kill list; not a silent pass).
DSR < 0.95 is KILL.
Walk-forward must win at least 2 of 3 windows.
Edge must be > 0 in both halves and > 0 after removing the top 10 entries.
Group median edge must be > 0.
excluded_entry_pct > 10 is UNCLEAR.
Hold-out entries < 100 is WITHDRAWN.
Same-bar fill is INVALID (the run is discarded, not a result).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_TRADES = 100
SHARPE_SUSPICIOUS_ABOVE = 3.0
DSR_MIN = 0.95
WALK_FORWARD_MIN_WINS = 2
WALK_FORWARD_WINDOWS = 3
TAKER_FEE_PCT_PER_SIDE = 0.2
SLIPPAGE_PCT_PER_SIDE = {
    "stable": 0.10,
    "other": 0.20,
    "volatile": 0.35,
}
STRESS_SLIPPAGE_FLOOR_PCT = 0.3
HARD_STRESS_SMALL_CAP_PCT = 0.5
HARD_STRESS_OTHER_PCT = 0.3
EXCLUDED_ENTRY_PCT_MAX = 10.0
HOLDOUT_MIN_ENTRIES = 100
PARAMETER_CLIFF_GRID_PCT = 25.0
TOP10_REMOVAL = 10

_SEVERITY = {
    "PASS": 0,
    "WITHDRAWN": 1,
    "UNCLEAR": 2,
    "SUSPICIOUS": 3,
    "KILL": 4,
    "INVALID": 5,
}

_REQUIRED = (
    "n_trades",
    "sharpe",
    "dsr",
    "trial_count_n",
    "same_bar_fill",
    "one_bar_delay",
    "point_in_time_universe",
    "fees_applied",
    "slippage_applied",
    "funding",
    "asset_class",
    "cost_tier",
    "taker_fee_pct_per_side",
    "slippage_pct_per_side",
    "walk_forward_wins",
    "walk_forward_windows",
    "edge_without_top10",
    "edge_half_a",
    "edge_half_b",
    "group_median_edge",
    "excluded_entry_pct",
    "holdout_entries",
    "parameter_cliff",
    "parameter_cliff_grid_pct",
    "signal_against_higher_frame",
    "buying_into_loss",
)


@dataclass
class ProtocolResult:
    status: str
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def expected_slippage_pct(
    asset_class: str,
    cost_tier: str,
    *,
    small_cap: bool,
) -> float:
    """Slippage percent per side for the s2 paper cost model."""
    if asset_class not in SLIPPAGE_PCT_PER_SIDE:
        raise ValueError(f"unknown asset_class {asset_class!r}")
    base = SLIPPAGE_PCT_PER_SIDE[asset_class]
    if cost_tier == "base":
        return base
    if cost_tier == "stress":
        return max(STRESS_SLIPPAGE_FLOOR_PCT, base)
    if cost_tier == "hard_stress":
        s1 = HARD_STRESS_SMALL_CAP_PCT if small_cap else HARD_STRESS_OTHER_PCT
        return max(s1, base)
    raise ValueError(f"unknown cost_tier {cost_tier!r}")


def _as_float(payload: dict, key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _as_bool(payload: dict, key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return None


def _bump(findings: list[tuple[str, str]], status: str, reason: str) -> None:
    findings.append((status, reason))


def validate_payload(payload: Any) -> ProtocolResult:
    """Return the worst status and every reason. Never raises on bad input."""
    if not isinstance(payload, dict):
        return ProtocolResult("INVALID", ["payload_not_object"])

    findings: list[tuple[str, str]] = []
    missing = [key for key in _REQUIRED if key not in payload]
    if missing:
        _bump(findings, "INVALID", "missing=" + ",".join(missing))

    n_trades = _as_int(payload, "n_trades")
    sharpe = _as_float(payload, "sharpe")
    dsr = _as_float(payload, "dsr")
    trial_n = _as_int(payload, "trial_count_n")
    if "n_trades" in payload and n_trades is None:
        _bump(findings, "INVALID", "n_trades_not_int")
    if "sharpe" in payload and sharpe is None:
        _bump(findings, "INVALID", "sharpe_not_number")
    if "dsr" in payload and dsr is None:
        _bump(findings, "INVALID", "dsr_not_number")
    if "trial_count_n" in payload and (trial_n is None or trial_n < 1):
        _bump(findings, "INVALID", "trial_count_n_invalid")

    if n_trades is not None and n_trades < MIN_TRADES:
        _bump(findings, "UNCLEAR", f"min_trades<{MIN_TRADES}")

    if sharpe is not None and sharpe > SHARPE_SUSPICIOUS_ABOVE:
        _bump(findings, "SUSPICIOUS", f"sharpe>{SHARPE_SUSPICIOUS_ABOVE:g}")

    if dsr is not None and dsr < DSR_MIN:
        _bump(findings, "KILL", f"dsr<{DSR_MIN:g}")

    same_bar = _as_bool(payload, "same_bar_fill")
    if "same_bar_fill" in payload and same_bar is None:
        _bump(findings, "INVALID", "same_bar_fill_not_bool")
    elif same_bar is True:
        _bump(findings, "INVALID", "same_bar_fill")

    for flag, kill_reason in (
        ("one_bar_delay", "missing_one_bar_delay"),
        ("point_in_time_universe", "missing_point_in_time_universe"),
        ("fees_applied", "fees_not_applied"),
        ("slippage_applied", "slippage_not_applied"),
    ):
        value = _as_bool(payload, flag)
        if flag in payload and value is None:
            _bump(findings, "INVALID", f"{flag}_not_bool")
        elif value is False:
            _bump(findings, "KILL", kill_reason)

    if "funding" in payload:
        funding = payload.get("funding")
        if funding != "not_applicable" and _as_float(payload, "funding") is None:
            _bump(findings, "INVALID", "funding_missing_or_invalid")

    asset_class = payload.get("asset_class")
    cost_tier = payload.get("cost_tier")
    small_cap = payload.get("small_cap", False)
    if "asset_class" in payload and asset_class not in SLIPPAGE_PCT_PER_SIDE:
        _bump(findings, "INVALID", "asset_class_invalid")
    if "cost_tier" in payload and cost_tier not in ("base", "stress", "hard_stress"):
        _bump(findings, "INVALID", "cost_tier_invalid")
    if cost_tier == "hard_stress" and (
        "small_cap" not in payload or not isinstance(small_cap, bool)
    ):
        _bump(findings, "INVALID", "small_cap_required_for_hard_stress")
    elif not isinstance(small_cap, bool):
        small_cap = False
        if "small_cap" in payload:
            _bump(findings, "INVALID", "small_cap_not_bool")

    fee = _as_float(payload, "taker_fee_pct_per_side")
    slip = _as_float(payload, "slippage_pct_per_side")
    if "taker_fee_pct_per_side" in payload and fee is None:
        _bump(findings, "INVALID", "taker_fee_not_number")
    if "slippage_pct_per_side" in payload and slip is None:
        _bump(findings, "INVALID", "slippage_not_number")
    if (
        isinstance(asset_class, str)
        and asset_class in SLIPPAGE_PCT_PER_SIDE
        and cost_tier in ("base", "stress", "hard_stress")
        and isinstance(small_cap, bool)
        and fee is not None
        and slip is not None
    ):
        if abs(fee - TAKER_FEE_PCT_PER_SIDE) > 1e-9:
            _bump(findings, "KILL", "taker_fee_not_0.2pct_per_side")
        expected = expected_slippage_pct(
            asset_class, cost_tier, small_cap=small_cap
        )
        if abs(slip - expected) > 1e-9:
            _bump(
                findings,
                "KILL",
                f"slippage_expected_{expected:g}_got_{slip:g}",
            )

    wins = _as_int(payload, "walk_forward_wins")
    windows = _as_int(payload, "walk_forward_windows")
    if "walk_forward_wins" in payload and wins is None:
        _bump(findings, "INVALID", "walk_forward_wins_not_int")
    if "walk_forward_windows" in payload and windows is None:
        _bump(findings, "INVALID", "walk_forward_windows_not_int")
    if windows is not None and windows != WALK_FORWARD_WINDOWS:
        _bump(findings, "KILL", f"walk_forward_windows!={WALK_FORWARD_WINDOWS}")
    elif wins is not None and wins < WALK_FORWARD_MIN_WINS:
        _bump(
            findings,
            "KILL",
            f"walk_forward<{WALK_FORWARD_MIN_WINS}_of_{WALK_FORWARD_WINDOWS}",
        )

    for key, label in (
        ("edge_half_a", "edge_half_a"),
        ("edge_half_b", "edge_half_b"),
        ("group_median_edge", "group_median"),
    ):
        edge = _as_float(payload, key)
        if key in payload and edge is None:
            _bump(findings, "INVALID", f"{key}_not_number")
        elif edge is not None and edge <= 0:
            _bump(findings, "KILL", f"{label}<=0")

    top10 = _as_float(payload, "edge_without_top10")
    if "edge_without_top10" in payload and top10 is None:
        _bump(findings, "INVALID", "edge_without_top10_not_number")
    elif top10 is not None and top10 <= 0:
        if n_trades is not None and n_trades < MIN_TRADES:
            _bump(findings, "UNCLEAR", "top10_removal_unclear_lt_100_trades")
        else:
            _bump(findings, "KILL", "edge_without_top10<=0")

    excluded = _as_float(payload, "excluded_entry_pct")
    if "excluded_entry_pct" in payload and excluded is None:
        _bump(findings, "INVALID", "excluded_entry_pct_not_number")
    elif excluded is not None and excluded > EXCLUDED_ENTRY_PCT_MAX:
        _bump(findings, "UNCLEAR", f"excluded_entry_pct>{EXCLUDED_ENTRY_PCT_MAX:g}")

    holdout = _as_int(payload, "holdout_entries")
    if "holdout_entries" in payload and holdout is None:
        _bump(findings, "INVALID", "holdout_entries_not_int")
    elif holdout is not None and holdout < HOLDOUT_MIN_ENTRIES:
        _bump(findings, "WITHDRAWN", f"holdout_entries<{HOLDOUT_MIN_ENTRIES}")

    cliff = _as_bool(payload, "parameter_cliff")
    grid = _as_float(payload, "parameter_cliff_grid_pct")
    if "parameter_cliff" in payload and cliff is None:
        _bump(findings, "INVALID", "parameter_cliff_not_bool")
    if "parameter_cliff_grid_pct" in payload and grid is None:
        _bump(findings, "INVALID", "parameter_cliff_grid_pct_not_number")
    elif grid is not None and abs(grid - PARAMETER_CLIFF_GRID_PCT) > 1e-9:
        _bump(findings, "INVALID", "parameter_cliff_grid_not_25")
    if cliff is True:
        _bump(findings, "KILL", "parameter_cliff")

    against = _as_bool(payload, "signal_against_higher_frame")
    if "signal_against_higher_frame" in payload and against is None:
        _bump(findings, "INVALID", "signal_against_higher_frame_not_bool")
    elif against is True:
        _bump(findings, "KILL", "signal_against_higher_frame")

    into_loss = _as_bool(payload, "buying_into_loss")
    if "buying_into_loss" in payload and into_loss is None:
        _bump(findings, "INVALID", "buying_into_loss_not_bool")
    elif into_loss is True:
        _bump(findings, "KILL", "buying_into_loss")

    if not findings:
        return ProtocolResult("PASS", [])
    status = max(findings, key=lambda item: _SEVERITY[item[0]])[0]
    reasons = [reason for _status, reason in findings]
    return ProtocolResult(status, reasons)


def validate_directory(path: Path) -> list[tuple[Path, ProtocolResult]]:
    """Validate every ``*.json`` file. An empty directory is an empty list."""
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    results: list[tuple[Path, ProtocolResult]] = []
    for file in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            results.append((file, ProtocolResult("INVALID", [f"json_error:{exc.msg}"])))
            continue
        results.append((file, validate_payload(payload)))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate hypothesis evidence JSON")
    parser.add_argument(
        "--evidence",
        default="docs/evidence",
        help="Directory of evidence JSON files (default: docs/evidence)",
    )
    args = parser.parse_args(argv)
    directory = Path(args.evidence)
    if not directory.is_dir():
        print(f"hypothesis protocol: no evidence dir {directory}")
        return 0
    results = validate_directory(directory)
    if not results:
        print(f"hypothesis protocol: no JSON in {directory}")
        return 0
    failed = False
    for file, result in results:
        print(f"{file}: {result.status} {', '.join(result.reasons)}")
        if not result.passed:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
