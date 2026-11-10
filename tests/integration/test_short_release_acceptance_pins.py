"""One negative assertion per acceptance check, so removing that check fails."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

from tools.short_release_acceptance import (
    canonical_ac_hash,
    classify_index,
    classify_row,
    judge_fault,
    prepare_disabled_manifest,
    validate_manifest,
)
from tools.short_release_acceptance.manifest import OUTPUT_DIR, civil_window_reasons

_SPEC = importlib.util.spec_from_file_location(
    "short_release_offline_helpers",
    Path(__file__).with_name("test_short_release_offline.py"),
)
_helpers = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_helpers)

CANDIDATE_SHA = _helpers.CANDIDATE_SHA
_actual_row = _helpers._actual_row
_complete_manifest = _helpers._complete_manifest
_compose = _helpers._compose
_compliant_fault = _helpers._compliant_fault
_config = _helpers._config
_stages = _helpers._stages


def _reasons(stages) -> list[str]:
    return _compose(stages=stages)["structural_reasons"]


def _accepted_index(sha: str = CANDIDATE_SHA) -> dict:
    rows = []
    for ac_id in ("AC-E2E-1", "AC-E2E-2", "AC-E2E-3", "AC-E2E-4"):
        row = _actual_row(sha)
        row["ac_id"] = ac_id
        rows.append(row)
    return {
        "candidate_sha": sha,
        "ac_revision_hash": canonical_ac_hash(),
        "rows": rows,
    }


def test_index_requires_the_real_candidate_sha():
    current = _accepted_index()
    assert classify_index(current, expected_sha=CANDIDATE_SHA)["release_verdict"] == "ACCEPTED"
    old = _accepted_index("f" * 40)
    stale_rows = classify_index(old, expected_sha=CANDIDATE_SHA)
    assert stale_rows["release_verdict"] == "NO-GO"
    assert stale_rows["authenticated_testnet_pass"] is False
    header = _accepted_index()
    header["candidate_sha"] = "f" * 40
    header_result = classify_index(header, expected_sha=CANDIDATE_SHA)
    assert header_result["reason"] == "stale_sha"
    missing = _accepted_index()
    del missing["candidate_sha"]
    assert classify_index(missing, expected_sha=CANDIDATE_SHA)["reason"] == "stale_sha"
    assert classify_index(current, expected_sha="not-a-sha")["reason"] == "expected_sha_invalid"


def test_classify_index_accepted_path_requires_every_row_and_every_ac():
    mixed = _accepted_index()
    mixed["rows"][0]["kind"] = "fixture"
    mixed["rows"][0]["authenticated_testnet_pass"] = False
    assert classify_index(mixed, expected_sha=CANDIDATE_SHA)["release_verdict"] == "NOT_VERIFIED"
    same_ac = _accepted_index()
    for row in same_ac["rows"]:
        row["ac_id"] = "AC-E2E-1"
    assert classify_index(same_ac, expected_sha=CANDIDATE_SHA)["reason"] == "ac_coverage_incomplete"
    wrong_hash = _accepted_index()
    wrong_hash["ac_revision_hash"] = "0" * 64
    assert classify_index(wrong_hash, expected_sha=CANDIDATE_SHA)["reason"] == "ac_revision_mismatch"


def test_actual_rows_require_allowlisted_producer_and_tester_identity():
    spaced = _actual_row()
    spaced["producer"] = "separate_authorized_run "
    assert classify_row(spaced, index_sha=CANDIDATE_SHA)["reason"] == "producer_not_allowlisted"
    relabeled = _compose()["evidence_row"]
    relabeled.update(
        {
            "kind": "actual",
            "test_status": "passed",
            "reviewers": _actual_row()["reviewers"],
            "tester_status": "passed",
            "tester_head": CANDIDATE_SHA,
            "tester_model": "separate-tester",
            "funding_proof": _actual_row()["funding_proof"],
            "ac_revision_hash": canonical_ac_hash(),
        }
    )
    assert classify_row(relabeled, index_sha=CANDIDATE_SHA)["release_verdict"] == "NO-GO"
    coder = _actual_row()
    coder["tester_model"] = "Grok-4.7"
    assert "tester" in classify_row(coder, index_sha=CANDIDATE_SHA)["reason"]
    reviewer = _actual_row()
    reviewer["tester_model"] = "DeepSeek-V4-Pro"
    assert "tester" in classify_row(reviewer, index_sha=CANDIDATE_SHA)["reason"]
    stale_tester = _actual_row()
    stale_tester["tester_head"] = "b" * 40
    assert classify_row(stale_tester, index_sha=CANDIDATE_SHA)["reason"] == "stale_sha"
    missing_head = _actual_row()
    missing_head["tester_head"] = None
    assert "tester" in classify_row(missing_head, index_sha=CANDIDATE_SHA)["reason"]
    bad_status = _actual_row()
    bad_status["tester_status"] = "failed"
    assert "tester" in classify_row(bad_status, index_sha=CANDIDATE_SHA)["reason"]
    bad_test = _actual_row()
    bad_test["test_status"] = "skipped"
    assert "test_status" in classify_row(bad_test, index_sha=CANDIDATE_SHA)["reason"]
    bad_hash = _actual_row()
    bad_hash["ac_revision_hash"] = "1" * 64
    assert "ac_revision" in classify_row(bad_hash, index_sha=CANDIDATE_SHA)["reason"]
    reopen = _actual_row()
    reopen["issue_412_action"] = "Reopen"
    assert classify_row(reopen, index_sha=CANDIDATE_SHA)["reason"] == "issue_412_untouched"
    absent = _actual_row()
    del absent["issue_412_action"]
    assert classify_row(absent, index_sha=CANDIDATE_SHA)["reason"] == "issue_412_untouched"


def test_each_funding_proof_and_window_check():
    def rejected(field, value) -> str:
        row = _actual_row()
        row["funding_proof"][field] = value
        return classify_row(row, index_sha=CANDIDATE_SHA)["release_verdict"]

    assert rejected("observed", "indicative") == "NOT_VERIFIED"
    assert rejected("kind", "fixture") == "NOT_VERIFIED"
    assert rejected("synthetic", True) == "NOT_VERIFIED"
    assert rejected("source", "synthetic") == "NOT_VERIFIED"
    assert rejected("proof_sha", "ab") == "NOT_VERIFIED"
    assert rejected("proof_sha", "zz" * 32) == "NOT_VERIFIED"
    assert rejected("proof_sha", "0" * 64) == "NOT_VERIFIED"
    weekend = civil_window_reasons(
        "2026-11-14T15:00:00+01:00",
        "2026-11-14T18:00:00+01:00",
    )
    assert weekend == ["weekend_entry"]
    entry_cutoff = civil_window_reasons(
        "2026-11-10T20:00:00+01:00",
        "2026-11-10T21:00:00+01:00",
    )
    assert "entry_after_cutoff" in entry_cutoff
    exit_cutoff = civil_window_reasons(
        "2026-11-10T18:00:00+01:00",
        "2026-11-10T20:00:01+01:00",
    )
    assert exit_cutoff == ["exit_after_cutoff"]
    overnight = civil_window_reasons(
        "2026-11-13T18:00:00+01:00",
        "2026-11-14T10:00:00+01:00",
    )
    assert "overnight" in overnight
    funding = civil_window_reasons(
        "2026-11-10T15:00:00+01:00",
        "2026-11-10T18:00:00+01:00",
        "2026-11-10T18:00:00+01:00",
        "2026-11-10T19:00:00+01:00",
        require_funding=True,
    )
    assert funding == ["funding_window"]
    assert civil_window_reasons("2026-11-10T15:00:00", "2026-11-10T18:00:00+01:00") == [
        "naive_or_missing_timestamp"
    ]
    long_hold = civil_window_reasons(
        "2026-11-10T10:00:00+01:00",
        "2026-11-10T16:00:00+01:00",
    )
    assert long_hold == ["holding_cap"]


def test_manifest_allowlists_and_unfrozen_values(tmp_path):
    assert validate_manifest(_complete_manifest(mode="testnet"))["structurally_complete"] is True
    assert "mode_rejected" in validate_manifest(_complete_manifest(mode="live"))["reasons"]
    assert "mode_rejected" in validate_manifest(_complete_manifest(mode="TESTNET"))["reasons"]
    missing_mode = _complete_manifest()
    del missing_mode["mode"]
    assert "mode_rejected" in validate_manifest(missing_mode)["reasons"]
    assert "allow_live_refused" in validate_manifest(_complete_manifest(allow_live=True))["reasons"]
    missing_live = _complete_manifest()
    del missing_live["allow_live"]
    assert "allow_live_refused" in validate_manifest(missing_live)["reasons"]
    shorts = {"allow_live": True}
    assert "allow_live_refused" in validate_manifest(_complete_manifest(shorts=shorts))["reasons"]
    for key, value in (
        ("permit_minutes", 30),
        ("permit_amount", "1000"),
        ("exhaustion_thresholds", {"rsi": 80}),
    ):
        document = _complete_manifest()
        document[key] = value
        assert "unfrozen_value" in validate_manifest(document)["reasons"]
    assert "placeholder_sha" in validate_manifest(_complete_manifest(candidate_sha="0" * 40))["reasons"]
    assert "kill_switch_refused" in validate_manifest(_complete_manifest(global_kill_switch=True))["reasons"]
    assert "execution_authorization_refused" in validate_manifest(
        _complete_manifest(execution_authorized=True)
    )["reasons"]
    assert "action_unknown" in validate_manifest(_complete_manifest(actions=["SHORT", "NOPE"]))["reasons"]
    assert "redaction_incomplete" in validate_manifest(
        _complete_manifest(redaction={"secrets_redacted": False})
    )["reasons"]
    assert "keys_authorization_missing" in validate_manifest(
        _complete_manifest(keys_authorization="in-the-file")
    )["reasons"]
    limits = copy.deepcopy(_complete_manifest()["limits"])
    limits["min_qty_fits"] = False
    assert "min_qty_fits_unknown" in validate_manifest(_complete_manifest(limits=limits))["reasons"]
    window = copy.deepcopy(_complete_manifest()["window"])
    window["weekdays"] = ["Mon"]
    assert "weekdays" in validate_manifest(_complete_manifest(window=window))["reasons"]
    window = copy.deepcopy(_complete_manifest()["window"])
    window["cutoff"] = "21:00:00"
    assert "cutoff" in validate_manifest(_complete_manifest(window=window))["reasons"]
    window = copy.deepcopy(_complete_manifest()["window"])
    window["timezone"] = "UTC"
    assert "timezone" in validate_manifest(_complete_manifest(window=window))["reasons"]
    window = copy.deepcopy(_complete_manifest()["window"])
    window["max_holding_hours"] = "5"
    assert "max_holding" in validate_manifest(_complete_manifest(window=window))["reasons"]
    for key in (
        "gate_api_key",
        "api-key",
        "apiSecret",
        "secret_key",
        "privateKey",
        "GATE_API_SECRET",
        "x-api-key",
        "bearer_token",
    ):
        document = _complete_manifest()
        document[key] = "x"
        assert "secret_key" in validate_manifest(document)["reasons"], key
    for value in ("API_KEY=abc", "Bearer eyJabc", "ab" * 20, "Ab+/" * 8):
        document = _complete_manifest(notes=value)
        result = validate_manifest(document)
        assert "secret_value" in result["reasons"], value
        assert value not in str(result["reasons"])
    outside = tmp_path / "manifest.json"
    with pytest.raises(Exception, match="output_dir_required"):
        prepare_disabled_manifest(_complete_manifest(), outside)
    assert not outside.exists()
    secret_path = OUTPUT_DIR / "secret.json"
    with pytest.raises(Exception, match="secret_refused"):
        prepare_disabled_manifest(_complete_manifest(notes="API_KEY=abc"), secret_path)
    assert not secret_path.exists()


def test_chain_negative_checks_are_each_visible():
    assert _compose(config=_config())["rehearsed_short_intents"] == 1
    truthy = _config()
    truthy["short_release_acceptance"]["enabled"] = "true"
    assert _compose(config=truthy)["rehearsed_short_intents"] == 0
    numeric = _config()
    numeric["short_release_acceptance"]["enabled"] = 1
    assert _compose(config=numeric)["rehearsed_short_intents"] == 0
    stock = _helpers._candidate()
    stock["asset_class"] = "STOCK_TOKEN"
    assert "asset_class_denied" in _compose(candidate=stock)["gate_reasons"]

    stages = _stages()
    stages[6]["correlation_id"] = "other"
    assert "correlation_mismatch" in _reasons(stages)
    stages = _stages()
    stages[5]["parent_id"] = "s-scan"
    assert "parent_break" in _reasons(stages)
    stages = _stages()
    stages[3]["owner_contract"] = "someone-else"
    assert "owner_mismatch:fill" in _reasons(stages)
    stages = _stages()
    stages[3]["payload"]["snapshots"][1]["cumulative_qty"] = "0.9"
    assert "fill_cumulative_mismatch" in _reasons(stages)
    stages = _stages()
    stages[3]["payload"]["confirmed"] = False
    assert "fill_unconfirmed" in _reasons(stages)
    stages = _stages()
    stages[3]["payload"]["snapshots"][0]["trade_ids"] = "t1"
    malformed = _compose(stages=stages)
    assert "fill_malformed" in malformed["structural_reasons"]
    assert malformed["release_verdict"] == "NO-GO"
    stages = _stages()
    stages[4]["payload"]["effective_stop_ids"] = []
    assert "stop_count" in _reasons(stages)
    stages = _stages()
    stages[4]["payload"]["confirmed_residual_qty"] = "0"
    assert "residual_unconfirmed" in _reasons(stages)
    stages = _stages()
    stages[4]["payload"]["confirmed_residual_qty"] = "5"
    mismatch = _compose(stages=stages)
    assert "residual_fill_mismatch" in mismatch["structural_reasons"]
    assert mismatch["release_verdict"] == "NO-GO"
    stages = _stages()
    stages[4]["payload"]["confirmed_residual_qty"] = "1.02"
    assert "residual_fill_mismatch" in _reasons(stages)
    stages = _stages()
    stages[4]["payload"]["confirmed_residual_qty"] = "1.01"
    assert "residual_fill_mismatch" not in _reasons(stages)
    stages = _stages()
    stages[5]["payload"]["exit_at"] = "2026-11-10T16:00:00+01:00"
    assert "holding_mismatch" in _reasons(stages)
    stages = _stages()
    stages[5]["payload"]["entry_at"] = "2026-11-14T15:00:00+01:00"
    stages[5]["payload"]["exit_at"] = "2026-11-14T18:00:00+01:00"
    stages[5]["payload"]["settlement_at"] = "2026-11-14T16:00:00+01:00"
    stages[5]["payload"]["due_at"] = "2026-11-14T19:00:00+01:00"
    assert "weekend_entry" in _reasons(stages)
    stages = _stages()
    stages[7]["payload"]["entry_snapshot"]["rsi"] = "1"
    assert "lineage_mismatch" in _reasons(stages)
    stages = _stages()
    stages[7]["payload"]["entry_snapshot"]["rsi"] = None
    assert "missing_reason_absent" in _reasons(stages)
    stages = _stages()
    stages[2]["payload"]["side"] = "long"
    assert "not_short" in _reasons(stages)
    stages = _stages()
    stages[2]["payload"]["order_submitted"] = True
    assert "order_submitted" in _reasons(stages)
    stages = _stages()
    stages[4]["payload"]["reduce_only"] = False
    assert "protection_not_reduce_only" in _reasons(stages)
    stages = _stages()
    stages[5]["payload"]["reduce_only"] = False
    report = _compose(stages=stages)
    assert "cover_not_reduce_only" in report["structural_reasons"]
    assert report["cover_allowed"] is False
    stages = _stages()
    stages[4]["payload"]["flatten_order_ids"] = ["flat-1"]
    assert "unauthorized_flatten" in _reasons(stages)
    stages = _stages()
    stages[1]["payload"]["risk_denied"] = True
    assert "risk_denied" in _reasons(stages)
    stages = _stages()
    stages[6]["payload"]["reported_net"] = "9.00"
    assert "net_mismatch" in _reasons(stages)
    stages = _stages()
    stages[0]["payload"]["threshold_status"] = "passed"
    assert "threshold_not_frozen" in _reasons(stages)
    stages = _stages()
    stages.append(copy.deepcopy(stages[0]))
    assert "duplicate_stage" in _reasons(stages)
    stages = _stages()
    stages[7]["payload"]["exit_links"][0]["exit_id"] = "other-exit"
    linked = _compose(stages=stages)
    assert "exit_target_mismatch" in linked["structural_reasons"]
    assert linked["release_verdict"] == "NO-GO"
    assert linked["rehearsed_short_intents"] == 0


def test_fault_negative_checks_do_not_crash():
    wrong_owner = _compliant_fault("stale_source")
    wrong_owner["owner_contract"] = "T220-02"
    assert "owner_not_cited" in judge_fault(wrong_owner)["violations"]
    tenant = _compliant_fault("wrong_tenant")
    tenant["tenant_id"] = "other"
    assert "wrong_tenant" in judge_fault(tenant)["violations"]
    leak = _compliant_fault("wrong_tenant")
    leak["foreign_ids_in_result"] = True
    assert "tenant_leak" in judge_fault(leak)["violations"]
    hidden = _compliant_fault("stop_rejection")
    hidden["protection_state"] = "protected"
    assert "stop_state_hidden" in judge_fault(hidden)["violations"]
    stops = _compliant_fault("stop_cover_race")
    stops["effective_stop_ids"] = ["stop-1", "stop-2"]
    assert "multiple_stops" in judge_fault(stops)["violations"]
    cover = _compliant_fault("live_mode")
    cover["cover_allowed"] = False
    assert "cover_disabled" in judge_fault(cover)["violations"]
    bare = _compliant_fault("live_mode")
    bare["reduce_only_covers"] = []
    assert "reduce_only_cover_missing" in judge_fault(bare)["violations"]
    redis = _compliant_fault("redis_timeout")
    redis["inhibits_entries"] = False
    assert "entries_not_inhibited" in judge_fault(redis)["violations"]
    unknown = _compliant_fault("uncertain_create")
    unknown["residual_qty"] = "nope"
    assert "residual_unknown" in judge_fault(unknown)["violations"]
    nested = _compliant_fault("redis_timeout")
    nested["client_order_ids"] = [["nested"]]
    malformed = judge_fault(nested)
    assert malformed["release_verdict"] == "NO-GO"
    assert "trace_malformed" in malformed["violations"]
    orphans = _compliant_fault("restart_orphan")
    orphans["orphan_ids"] = [{"id": "orphan-1"}]
    orphan_result = judge_fault(orphans)
    assert orphan_result["release_verdict"] == "NO-GO"
    assert "trace_malformed" in orphan_result["violations"]
