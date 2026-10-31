"""Kill-rule pins for tools.hypothesis_protocol. Thresholds are the handover."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.hypothesis_protocol import (
    HARD_STRESS_OTHER_PCT,
    HARD_STRESS_SMALL_CAP_PCT,
    MIN_TRADES,
    STRESS_SLIPPAGE_FLOOR_PCT,
    expected_slippage_pct,
    validate_directory,
    validate_payload,
)


def _pass(**overrides):
    payload = {
        "n_trades": MIN_TRADES,
        "sharpe": 3.0,
        "dsr": 0.95,
        "trial_count_n": 50,
        "same_bar_fill": False,
        "one_bar_delay": True,
        "point_in_time_universe": True,
        "fees_applied": True,
        "slippage_applied": True,
        "funding": "not_applicable",
        "asset_class": "other",
        "cost_tier": "base",
        "small_cap": False,
        "taker_fee_pct_per_side": 0.2,
        "slippage_pct_per_side": 0.20,
        "walk_forward_wins": 2,
        "walk_forward_windows": 3,
        "edge_without_top10": 0.01,
        "edge_half_a": 0.01,
        "edge_half_b": 0.01,
        "group_median_edge": 0.01,
        "excluded_entry_pct": 10.0,
        "holdout_entries": 100,
        "parameter_cliff": False,
        "parameter_cliff_grid_pct": 25,
        "signal_against_higher_frame": False,
        "buying_into_loss": False,
    }
    payload.update(overrides)
    return payload


def test_passing_payload_is_pass():
    result = validate_payload(_pass())
    assert result.status == "PASS"
    assert result.reasons == []


def test_min_trades_below_100_is_unclear():
    result = validate_payload(_pass(n_trades=99))
    assert result.status == "UNCLEAR"
    assert any(reason.startswith("min_trades") for reason in result.reasons)


def test_sharpe_above_3_is_suspicious():
    result = validate_payload(_pass(sharpe=3.0001))
    assert result.status == "SUSPICIOUS"
    assert result.status != "PASS"


def test_sharpe_at_3_is_not_suspicious():
    assert validate_payload(_pass(sharpe=3)).status == "PASS"


def test_dsr_below_0_95_kills():
    result = validate_payload(_pass(dsr=0.9499))
    assert result.status == "KILL"
    assert any(reason.startswith("dsr<") for reason in result.reasons)


def test_dsr_at_0_95_passes():
    assert validate_payload(_pass(dsr=0.95)).status == "PASS"


@pytest.mark.parametrize(
    ("asset_class", "tier", "small_cap", "slip"),
    [
        ("stable", "base", False, 0.10),
        ("other", "base", False, 0.20),
        ("volatile", "base", False, 0.35),
        ("stable", "stress", False, max(STRESS_SLIPPAGE_FLOOR_PCT, 0.10)),
        ("other", "stress", False, max(STRESS_SLIPPAGE_FLOOR_PCT, 0.20)),
        ("volatile", "stress", False, max(STRESS_SLIPPAGE_FLOOR_PCT, 0.35)),
        ("other", "hard_stress", False, max(HARD_STRESS_OTHER_PCT, 0.20)),
        ("volatile", "hard_stress", True, max(HARD_STRESS_SMALL_CAP_PCT, 0.35)),
        ("stable", "hard_stress", True, max(HARD_STRESS_SMALL_CAP_PCT, 0.10)),
    ],
)
def test_cost_model_per_class(asset_class, tier, small_cap, slip):
    assert expected_slippage_pct(asset_class, tier, small_cap=small_cap) == slip
    result = validate_payload(
        _pass(
            asset_class=asset_class,
            cost_tier=tier,
            small_cap=small_cap,
            slippage_pct_per_side=slip,
        )
    )
    assert result.status == "PASS", result.reasons


def test_wrong_class_slippage_kills():
    result = validate_payload(_pass(asset_class="volatile", slippage_pct_per_side=0.10))
    assert result.status == "KILL"


def test_hard_stress_without_small_cap_flag_is_invalid():
    payload = _pass(cost_tier="hard_stress", slippage_pct_per_side=0.30)
    del payload["small_cap"]
    result = validate_payload(payload)
    assert result.status == "INVALID"
    assert "small_cap_required_for_hard_stress" in result.reasons


def test_walk_forward_needs_two_of_three():
    assert validate_payload(_pass(walk_forward_wins=2, walk_forward_windows=3)).passed
    killed = validate_payload(_pass(walk_forward_wins=1, walk_forward_windows=3))
    assert killed.status == "KILL"
    short = validate_payload(_pass(walk_forward_wins=2, walk_forward_windows=2))
    assert short.status == "KILL"


def test_halves_and_median_must_be_positive():
    assert validate_payload(_pass(edge_half_a=0)).status == "KILL"
    assert validate_payload(_pass(edge_half_b=-0.01)).status == "KILL"
    assert validate_payload(_pass(group_median_edge=0)).status == "KILL"


def test_top10_removal_must_stay_positive():
    killed = validate_payload(_pass(edge_without_top10=0))
    assert killed.status == "KILL"
    unclear = validate_payload(_pass(n_trades=50, edge_without_top10=0))
    assert unclear.status == "UNCLEAR"
    assert "top10_removal_unclear_lt_100_trades" in unclear.reasons


def test_excluded_above_10_percent_is_unclear():
    assert validate_payload(_pass(excluded_entry_pct=10)).status == "PASS"
    result = validate_payload(_pass(excluded_entry_pct=10.0001))
    assert result.status == "UNCLEAR"


def test_holdout_under_100_is_withdrawn():
    result = validate_payload(_pass(holdout_entries=99))
    assert result.status == "WITHDRAWN"


def test_same_bar_fill_is_invalid():
    assert validate_payload(_pass(same_bar_fill=True)).status == "INVALID"


def test_parameter_cliff_and_higher_frame_and_buying_the_loss():
    assert validate_payload(_pass(parameter_cliff=True)).status == "KILL"
    assert validate_payload(_pass(signal_against_higher_frame=True)).status == "KILL"
    assert validate_payload(_pass(buying_into_loss=True)).status == "KILL"
    assert validate_payload(_pass(parameter_cliff_grid_pct=20)).status == "INVALID"


def test_missing_protocol_field_is_invalid():
    payload = _pass()
    del payload["one_bar_delay"]
    assert validate_payload(payload).status == "INVALID"


def test_directory_skips_when_empty_and_fails_bad_json(tmp_path: Path):
    empty = tmp_path / "evidence-empty"
    empty.mkdir()
    assert validate_directory(empty) == []
    bad = empty / "trial.json"
    bad.write_text("{", encoding="utf-8")
    results = validate_directory(empty)
    assert results[0][1].status == "INVALID"
    good = empty / "ok.json"
    good.write_text(json.dumps(_pass()), encoding="utf-8")
    bad.unlink()
    assert validate_directory(empty)[0][1].passed
