"""Offline short-release acceptance harness (D-E2E, not issued).

Phase 1 judges fixture chains, evidence rows, and a disabled manual manifest.
It does not place orders or treat a green result as an authenticated testnet pass.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from strategies.positions import _ENTRY_SNAPSHOT_KEYS
from tools.short_release_acceptance import (
    CANONICAL_AC_TEXT,
    FAULT_OWNERS,
    INTEGRATION_WIRED,
    attempt_acceptance,
    canonical_ac_hash,
    classify_index,
    classify_row,
    compose_offline_chain,
    judge_fault,
    phase1_evidence_index,
    prepare_disabled_manifest,
    validate_manifest,
)
from tools.short_release_acceptance.constants import (
    AUTHORIZATION_SENTINEL,
    ENTRY_SNAPSHOT_KEYS,
    FROZEN_ORIGIN,
    OWNER_BY_STAGE,
    REDACTION_FLAGS,
)
from tools.short_release_acceptance.manifest import ManifestError

CANDIDATE_SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "short_release_acceptance"
PACKAGE = REPO / "tools" / "short_release_acceptance"
SPEC = REPO / "specs" / "short-release-acceptance-v220" / "04-requirements.md"

_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "ccxt",
        "requests",
        "urllib",
        "socket",
        "http",
        "dotenv",
        "subprocess",
    }
)
_FORBIDDEN_MODULES = frozenset(
    {
        "services.trading_service",
        "services.signal_orchestrator",
        "execution.gate_adapter",
        "execution.recovery",
    }
)


def _candidate() -> dict:
    return json.loads((FIXTURE_DIR / "candidate.json").read_text(encoding="utf-8"))


def _config(*, enabled: bool | None = True, allow_live: bool = False) -> dict:
    raw = {
        "shorts": {
            "enabled": True,
            "allow_live": allow_live,
            "climax_fade": {"enabled": True, "time_cap_hours": 16},
        }
    }
    if enabled is not None:
        raw["short_release_acceptance"] = {"enabled": enabled}
    return raw


def _mode_proof(**overrides) -> dict:
    proof = {
        "readback": True,
        "source": "venue_readback",
        "margin_mode": "isolated",
        "position_mode": "one_way",
        "leverage": "1",
    }
    proof.update(overrides)
    return proof


def _snapshot() -> dict:
    values = {key: "fixture" for key in ENTRY_SNAPSHOT_KEYS}
    values.update(
        {
            "rsi": "81",
            "atr": "2",
            "volume_factor": "3",
            "source": "scanner",
            "fee": "0.10",
            "fill_price": "100",
        }
    )
    values["missing_reasons"] = {}
    return values


def _stages() -> list[dict]:
    candidate = _candidate()
    snapshot = _snapshot()
    decision_payload = {
        "decision": "short_candidate",
        "risk_denied": False,
        "rsi": snapshot["rsi"],
        "atr": snapshot["atr"],
        "volume_factor": snapshot["volume_factor"],
        "source": snapshot["source"],
    }
    names = (
        ("scanner", "s-scan", None),
        ("decision", "s-dec", "s-scan"),
        ("short_intent", "s-intent", "s-dec"),
        ("fill", "fill-1", "s-intent"),
        ("protection", "s-prot", "fill-1"),
        ("cover", "s-cover", "s-prot"),
        ("ledger", "s-led", "s-cover"),
        ("trace", "s-trace", "s-led"),
    )
    payloads = {
        "scanner": {
            "candidate_id": candidate["candidate_id"],
            "signal": None,
            "threshold_status": "unmeasured",
            "raw": candidate["raw"],
        },
        "decision": decision_payload,
        "short_intent": {"side": "short", "order_submitted": False},
        "fill": {
            "confirmed": True,
            "snapshots": [
                {"trade_ids": ["t1"], "delta_qty": "0.4", "cumulative_qty": "0.4"},
                {"trade_ids": ["t2"], "delta_qty": "0.6", "cumulative_qty": "1.0"},
            ],
        },
        "protection": {
            "reduce_only": True,
            "opening_order_ids": [],
            "flatten_order_ids": [],
            "confirmed_residual_qty": "1",
            "effective_stop_ids": ["stop-1"],
            "clearance": {
                "venue_mark_present": True,
                "modeled_liquidation": False,
                "entry": "100",
                "liquidation": "110",
                "tick": "0.1",
                "stop": "109.5",
                "fraction": "0.05",
            },
        },
        "cover": {
            "reduce_only": True,
            "holding_hours": "3",
            "cap_hours": "4",
            "entry_at": "2026-11-10T15:00:00+01:00",
            "settlement_at": "2026-11-10T16:00:00+01:00",
            "exit_at": "2026-11-10T18:00:00+01:00",
            "due_at": "2026-11-10T19:00:00+01:00",
        },
        "ledger": {
            "components": [
                {"id": "gross", "amount": "10"},
                {"id": "fee_open", "amount": "-0.10"},
                {"id": "fee_cover", "amount": "-0.09"},
                {"id": "funding", "amount": "0.02"},
            ],
            "reported_net": "9.83",
            "venue_net": "9.83",
        },
        "trace": {
            "entry_snapshot": snapshot,
            "exit_links": [
                {"exit_id": "s-cover", "entry_order_id": "fill-1", "lot_id": "lot-1"}
            ],
        },
    }
    stages = []
    for name, stage_id, parent_id in names:
        stages.append(
            {
                "stage": name,
                "stage_id": stage_id,
                "parent_id": parent_id,
                "correlation_id": candidate["candidate_id"],
                "tenant_id": "fixture-tenant",
                "owner_contract": OWNER_BY_STAGE[name],
                "payload": payloads[name],
            }
        )
    return stages


def _compose(**overrides) -> dict:
    args = {
        "candidate": _candidate(),
        "config": _config(),
        "mode": "paper",
        "tenant_id": "fixture-tenant",
        "mode_proof": _mode_proof(),
        "stages": _stages(),
        "candidate_sha": CANDIDATE_SHA,
        "scope": "fixture_plumbing",
    }
    args.update(overrides)
    return compose_offline_chain(**args)


def _data_snapshot() -> dict[str, int]:
    root = REPO / "data"
    if not root.exists():
        return {}
    return {
        str(path.relative_to(REPO)): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file()
    }


def test_ac_e2e_1_scanner_to_cover():
    before = _data_snapshot()
    report = _compose()
    assert report["structural_rehearsal_ok"] is True
    assert report["structural_reasons"] == []
    assert report["gate_reasons"] == []
    assert report["rehearsed_short_intents"] == 1
    assert report["production_new_entries"] == 0
    assert report["entry_admitted"] is False
    assert report["production_regime_admission"] is False
    assert report["venue_calls"] == 0
    assert report["network_calls"] == 0
    assert report["data_writes"] == 0
    assert report["orders_submitted"] == 0
    assert report["integration_wired"] is False
    assert INTEGRATION_WIRED is False
    assert report["release_verdict"] == "NOT_VERIFIED"
    assert report["authenticated_testnet_pass"] is False
    assert report["issue_412"] == "closed_not_planned"
    assert report["net_status"] == "consistent"
    assert report["cover_allowed"] is True
    assert report["accepted_ids"][0] == "s-scan"
    assert report["accepted_ids"][-1] == "s-trace"
    assert "execution_buffer_minutes" in report["open_parameters"]
    assert "permit_minutes" in report["open_parameters"]
    assert "exhaustion_thresholds" in report["open_parameters"]
    for contract, status in report["owner_status"].items():
        if status == "missing":
            assert contract in report["blocked_dependencies"]
    assert report["owner_status"]["T220-01"] == "missing"
    assert report["owner_status"]["T220-02"] == "missing"
    assert report["owner_status"]["676"] == "missing"
    assert report["owner_status"]["D-EXH"] == "missing"
    assert report["owner_status"]["T220-05"] == "missing"
    assert report["owner_status"]["T220-04"] == "missing"
    assert report["owner_status"]["D-LIN"] == "missing"
    assert report["acceptance_blocked"] is True
    classified = classify_row(report["evidence_row"], index_sha=CANDIDATE_SHA)
    assert classified["release_verdict"] == "NOT_VERIFIED"
    assert classified["authenticated_testnet_pass"] is False
    forged = copy.deepcopy(report["evidence_row"])
    forged["kind"] = "actual"
    forged["authenticated_testnet_pass"] = True
    forged_result = classify_row(forged, index_sha=CANDIDATE_SHA)
    assert forged_result["authenticated_testnet_pass"] is False
    assert forged_result["release_verdict"] == "NO-GO"
    assert forged_result["reason"] == "producer_not_allowlisted"
    assert _data_snapshot() == before
    committed = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    assert committed["shorts"]["allow_live"] is False
    assert committed["shorts"]["climax_fade"]["enabled"] is True


def test_ac_e2e_1_gates_deny_new_entry():
    stages = _stages()
    denied = _compose(config=_config(enabled=None))
    assert denied["rehearsed_short_intents"] == 0
    assert "flag_off" in denied["gate_reasons"]
    assert denied["cover_allowed"] is True
    assert denied["production_new_entries"] == 0
    missing = _compose(config=_config(enabled=False))
    assert missing["rehearsed_short_intents"] == 0
    live = _compose(mode="live")
    assert live["rehearsed_short_intents"] == 0
    assert "live_denied" in live["gate_reasons"]
    allow_live = _compose(config=_config(allow_live=True))
    assert allow_live["rehearsed_short_intents"] == 0
    assert "live_denied" in allow_live["gate_reasons"]
    malformed = _compose(mode="LIVE")
    assert malformed["rehearsed_short_intents"] == 0
    assert "malformed_mode" in malformed["gate_reasons"]
    set_only = _compose(mode_proof=_mode_proof(readback=False, source="set_response"))
    assert set_only["rehearsed_short_intents"] == 0
    assert "mode_proof_rejected" in set_only["gate_reasons"]
    leverage_two = _compose(mode_proof=_mode_proof(leverage="2"))
    assert leverage_two["rehearsed_short_intents"] == 0
    production = _compose(scope="production")
    assert production["rehearsed_short_intents"] == 0
    assert production["production_new_entries"] == 0
    stock = _candidate()
    stock["asset_class"] = "stock_token"
    assert _compose(candidate=stock)["rehearsed_short_intents"] == 0
    stale = _candidate()
    stale["stale"] = True
    assert _compose(candidate=stale)["rehearsed_short_intents"] == 0
    signaled = _candidate()
    signaled["signal"] = "short"
    assert _compose(candidate=signaled)["rehearsed_short_intents"] == 0
    assert _compose(stages=stages, config=_config(enabled=None))["cover_allowed"] is True


def test_ac_e2e_1_chain_rejects_inconsistent_fixtures():
    foreign = _stages()
    foreign[-1]["tenant_id"] = "other-tenant"
    leaked = _compose(stages=foreign)
    assert leaked["structural_rehearsal_ok"] is False
    assert "wrong_tenant" in leaked["structural_reasons"]
    assert "s-trace" not in leaked["accepted_ids"]

    duplicate = _stages()
    duplicate[3]["payload"]["snapshots"][1]["trade_ids"] = ["t1"]
    assert "duplicate_trade_id" in _compose(stages=duplicate)["structural_reasons"]

    unknown = _stages()
    unknown[6]["payload"]["unknown_components"] = ["fee_open"]
    unknown[6]["payload"]["components"][1]["amount"] = None
    unknown[6]["payload"]["reported_net"] = "0"
    unknown_report = _compose(stages=unknown)
    assert unknown_report["net_status"] == "unmeasurable"
    assert "unknown_cost_as_number" in unknown_report["structural_reasons"]

    unmeasured = _stages()
    unmeasured[6]["payload"]["unknown_components"] = ["funding"]
    unmeasured[6]["payload"]["components"][3]["amount"] = None
    unmeasured[6]["payload"]["reported_net"] = None
    unmeasured[6]["payload"]["venue_net"] = None
    assert _compose(stages=unmeasured)["net_status"] == "unmeasurable"

    exact = _stages()
    exact[6]["payload"]["venue_net"] = "9.84"
    assert _compose(stages=exact)["net_status"] == "consistent"
    over = _stages()
    over[6]["payload"]["venue_net"] = "9.85"
    assert "tolerance_exceeded" in _compose(stages=over)["structural_reasons"]
    widened = _stages()
    widened[6]["payload"]["tolerance_usdt"] = "0.02"
    assert "tolerance_widened" in _compose(stages=widened)["structural_reasons"]

    closer = _stages()
    closer[4]["payload"]["clearance"]["stop"] = "109.6"
    assert "clearance_too_close" in _compose(stages=closer)["structural_reasons"]
    loose = _stages()
    loose[4]["payload"]["clearance"]["fraction"] = "0.04"
    assert "clearance_loosened" in _compose(stages=loose)["structural_reasons"]
    modeled = _stages()
    modeled[4]["payload"]["clearance"]["modeled_liquidation"] = True
    assert "clearance_missing_venue" in _compose(stages=modeled)["structural_reasons"]

    four_hours = _stages()
    four_hours[5]["payload"]["holding_hours"] = "4"
    four_hours[5]["payload"]["exit_at"] = "2026-11-10T19:00:00+01:00"
    four_hours[5]["payload"]["due_at"] = "2026-11-10T19:30:00+01:00"
    assert _compose(stages=four_hours)["structural_rehearsal_ok"] is True
    climax_cap = _stages()
    climax_cap[5]["payload"]["cap_hours"] = "16"
    assert "holding_cap" in _compose(stages=climax_cap)["structural_reasons"]
    day = _stages()
    day[5]["payload"]["holding_hours"] = "24"
    day[5]["payload"]["cap_hours"] = "24"
    assert "holding_cap" in _compose(stages=day)["structural_reasons"]

    extra_key = _stages()
    extra_key[7]["payload"]["entry_snapshot"]["social_score"] = "1"
    assert "snapshot_schema_extended" in _compose(stages=extra_key)["structural_reasons"]
    heuristic = _stages()
    heuristic[7]["payload"]["exit_links"] = [{"symbol": "FIXTURE/USDT", "time": "18:00"}]
    assert "exit_unlinked" in _compose(stages=heuristic)["structural_reasons"]


def _compliant_fault(fault: str) -> dict:
    trace = {
        "fault": fault,
        "owner_contract": FAULT_OWNERS[fault],
        "owner_result": {"deny_new_entry": True},
        "tenant_id": "fixture-tenant",
        "selected_tenant_id": "fixture-tenant",
        "opening_orders": [],
        "flatten_orders": [],
        "cancel_ids": [],
        "orphan_ids": [],
        "adopted": False,
        "cover_allowed": True,
        "reduce_only_covers": [{"id": "cover-1", "qty": "1", "reduce_only": True}],
        "inhibits_entries": True,
        "effective_stop_ids": ["stop-1"],
        "residual_qty": "1",
        "reduce_orders": [{"id": "cover-1", "qty": "1", "reduce_only": True}],
        "side": "short",
        "long_qty": "0",
        "client_order_ids": ["cover-1", "stop-1"],
        "reported_net": None if fault == "unknown_cost" else "9.83",
        "protection_state": "protected",
        "foreign_ids_in_result": False,
        "cache_fallback": False,
    }
    if fault == "stop_rejection":
        trace["effective_stop_ids"] = []
        trace["protection_state"] = "rejected"
    if fault == "restart_orphan":
        trace["orphan_ids"] = ["orphan-1"]
    if fault == "stop_cover_race":
        trace["reduce_orders"] = [
            {"id": "stop-1", "qty": "0.4", "reduce_only": True},
            {"id": "cover-1", "qty": "0.6", "reduce_only": True},
        ]
        trace["client_order_ids"] = ["stop-1", "cover-1"]
    return trace


def test_ac_e2e_2_fault_matrix():
    assert set(FAULT_OWNERS) == {
        "disabled_flag",
        "missing_flag",
        "live_mode",
        "wrong_tenant",
        "stale_source",
        "unknown_cost",
        "redis_timeout",
        "malformed_mode",
        "uncertain_create",
        "restart_orphan",
        "stop_rejection",
        "stop_cover_race",
    }
    for fault in FAULT_OWNERS:
        result = judge_fault(_compliant_fault(fault))
        assert result["invariant_ok"] is True, fault
        assert result["violations"] == [], fault
        assert result["release_verdict"] == "NOT_VERIFIED", fault
        assert result["authenticated_testnet_pass"] is False
        assert result["production_new_entries"] == 0
        assert result["venue_calls"] == 0

    duplicate = _compliant_fault("redis_timeout")
    duplicate["client_order_ids"] = ["cover-1", "cover-1"]
    assert "duplicate" in judge_fault(duplicate)["violations"]
    assert judge_fault(duplicate)["release_verdict"] == "NO-GO"

    flattened = _compliant_fault("stop_rejection")
    flattened["flatten_orders"] = [{"id": "flat-1", "qty": "1"}]
    assert "flatten_not_accepted" in judge_fault(flattened)["violations"]

    reversed_side = _compliant_fault("uncertain_create")
    reversed_side["long_qty"] = "1"
    assert "reversal" in judge_fault(reversed_side)["violations"]

    zero_cost = _compliant_fault("unknown_cost")
    zero_cost["reported_net"] = "0"
    assert "unknown_cost_as_number" in judge_fault(zero_cost)["violations"]

    race = _compliant_fault("stop_cover_race")
    race["reduce_orders"][1]["qty"] = "0.7"
    assert "over_reduce" in judge_fault(race)["violations"]

    adopted = _compliant_fault("restart_orphan")
    adopted["adopted"] = True
    adopted["cancel_ids"] = ["orphan-1"]
    orphan = judge_fault(adopted)
    assert "orphan_adopted" in orphan["violations"]
    assert "orphan_canceled" in orphan["violations"]

    missing_owner = _compliant_fault("stale_source")
    missing_owner["owner_result"] = None
    missing = judge_fault(missing_owner)
    assert missing["invariant_ok"] is False
    assert missing["status"] == "dependency_open"
    assert missing["release_verdict"] == "NOT_VERIFIED"

    opened = _compliant_fault("live_mode")
    opened["opening_orders"] = [{"id": "open-1"}]
    assert "new_entry" in judge_fault(opened)["violations"]

    hidden = _compliant_fault("redis_timeout")
    hidden["cache_fallback"] = True
    assert "hidden_cache_fallback" in judge_fault(hidden)["violations"]

    disabled = _compose(config=_config(enabled=False))
    assert disabled["rehearsed_short_intents"] == 0
    assert disabled["cover_allowed"] is True


def _actual_row(sha: str = CANDIDATE_SHA) -> dict:
    return {
        "kind": "actual",
        "ac_id": "AC-E2E-1",
        "candidate_sha": sha,
        "ac_revision_hash": canonical_ac_hash(),
        "test_status": "passed",
        "reviewers": [
            {"model": "deepseek-v4-pro", "head": sha},
            {"model": "claude-opus-5-5", "head": sha},
        ],
        "tester_status": "passed",
        "tester_head": sha,
        "tester_model": "separate-tester",
        "funding_proof": {
            "kind": "actual",
            "source": "venue_account_cashflow",
            "observed": "actual_settlement",
            "synthetic": False,
            "proof_sha": "ab" * 32,
            "entry_at": "2026-11-10T15:00:00+01:00",
            "settlement_at": "2026-11-10T16:00:00+01:00",
            "exit_at": "2026-11-10T18:00:00+01:00",
            "due_at": "2026-11-10T19:00:00+01:00",
        },
        "producer": "separate_authorized_run",
        "acceptance_executed_by_this_package": False,
        "issue_412_action": "leave_closed_not_planned",
    }


def test_ac_e2e_3_evidence_kind_and_sha_guard():
    index = phase1_evidence_index(CANDIDATE_SHA)
    classified = classify_index(index, expected_sha=CANDIDATE_SHA)
    assert classified["release_verdict"] == "NOT_VERIFIED"
    assert classified["authenticated_testnet_pass"] is False
    assert classified["authenticated_passes"] == 0
    committed = json.loads(
        (REPO / "specs" / "short-release-acceptance-v220" / "evidence-index.json").read_text(
            encoding="utf-8"
        )
    )
    assert committed == phase1_evidence_index(committed["candidate_sha"])
    assert classify_index(committed, expected_sha=committed["candidate_sha"])["authenticated_passes"] == 0
    stale_index = classify_index(committed, expected_sha="a" * 40)
    assert stale_index["release_verdict"] == "NO-GO"
    assert stale_index["reason"] == "stale_sha"

    drill = classify_row(_actual_row(), index_sha=CANDIDATE_SHA)
    assert drill["release_verdict"] == "ACCEPTED"
    assert drill["authenticated_testnet_pass"] is True
    assert classify_index(index, expected_sha=CANDIDATE_SHA)["release_verdict"] == "NOT_VERIFIED"

    stale = _actual_row()
    stale["reviewers"][0]["head"] = "a" * 40
    assert classify_row(stale, index_sha=CANDIDATE_SHA)["release_verdict"] == "NO-GO"
    moved = _actual_row()
    moved["candidate_sha"] = "b" * 40
    assert classify_row(moved, index_sha=CANDIDATE_SHA)["reason"] == "stale_sha"

    missing_funding = _actual_row()
    missing_funding["funding_proof"] = None
    funding = classify_row(missing_funding, index_sha=CANDIDATE_SHA)
    assert funding["release_verdict"] == "NOT_VERIFIED"
    assert "funding_proof" in funding["reason"]

    synthetic = _actual_row()
    synthetic["funding_proof"]["source"] = "synthetic"
    synthetic["funding_proof"]["synthetic"] = True
    assert classify_row(synthetic, index_sha=CANDIDATE_SHA)["release_verdict"] == "NOT_VERIFIED"

    missing_review = _actual_row()
    missing_review["reviewers"] = [{"model": "deepseek-v4-pro", "head": CANDIDATE_SHA}]
    assert "review" in classify_row(missing_review, index_sha=CANDIDATE_SHA)["reason"]
    coder_review = _actual_row()
    coder_review["reviewers"].append({"model": "grok-4.7", "head": CANDIDATE_SHA})
    assert classify_row(coder_review, index_sha=CANDIDATE_SHA)["release_verdict"] == "NOT_VERIFIED"

    fixture_claim = _actual_row()
    fixture_claim["kind"] = "fixture"
    fixture_claim["authenticated_testnet_pass"] = True
    claim = classify_row(fixture_claim, index_sha=CANDIDATE_SHA)
    assert claim["release_verdict"] == "NO-GO"
    assert claim["authenticated_testnet_pass"] is False

    manual = _actual_row()
    manual["kind"] = "manual"
    manual["authenticated_testnet_pass"] = False
    assert classify_row(manual, index_sha=CANDIDATE_SHA)["release_verdict"] == "NOT_VERIFIED"

    reopen = _actual_row()
    reopen["issue_412_action"] = "reopen"
    assert classify_row(reopen, index_sha=CANDIDATE_SHA)["reason"] == "issue_412_untouched"
    executed = _actual_row()
    executed["acceptance_executed_by_this_package"] = True
    assert classify_row(executed, index_sha=CANDIDATE_SHA)["reason"] == "this_package_cannot_execute"

    schema = json.loads(
        (REPO / "specs" / "short-release-acceptance-v220" / "evidence-schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["rows"]["items"]["properties"]["kind"]["enum"] == [
        "fixture",
        "manual",
        "actual",
    ]
    verdicts = schema["properties"]["release_verdict"]["enum"]
    assert "NOT_VERIFIED" in verdicts
    assert "NO-GO" in verdicts
    assert "AUTHENTICATED_PASS" not in verdicts


def _complete_manifest(**overrides) -> dict:
    document = {
        "schema_version": "d-e2e-evidence-1",
        "enabled": False,
        "execution_authorized": False,
        "global_kill_switch": False,
        "tenant_id": "paper-short-sandbox",
        "origin": FROZEN_ORIGIN,
        "actions": ["SHORT", "protective_stop", "COVER"],
        "limits": {
            "leverage": "1",
            "notional_pct_of_virtual_nav": "10",
            "stop_risk_pct": "0.25",
            "aggregate_stop_risk_pct": "0.75",
            "safety_cap_pct": "20",
            "exchange_min_qty": "1",
            "min_qty_fits": True,
        },
        "window": {
            "timezone": "Europe/Berlin",
            "cutoff": "20:00:00",
            "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            "max_holding_hours": "4",
            "same_calendar_day": True,
            "execution_buffer_minutes": None,
            "entry_at": "2026-11-10T15:00:00+01:00",
            "exit_at": "2026-11-10T18:00:00+01:00",
            "settlement_at": "2026-11-10T16:00:00+01:00",
            "due_at": "2026-11-10T19:00:00+01:00",
        },
        "candidate_sha": CANDIDATE_SHA,
        "keys_authorization": AUTHORIZATION_SENTINEL,
        "config_authorization": AUTHORIZATION_SENTINEL,
        "orders_authorization": AUTHORIZATION_SENTINEL,
        "redaction": {flag: True for flag in REDACTION_FLAGS},
        "mode": "paper",
        "allow_live": False,
        "shorts": {"allow_live": False},
    }
    document.update(overrides)
    return document


def test_ac_e2e_4_no_operation_without_manifest(tmp_path, monkeypatch):
    calls: list[str] = []

    def transport() -> None:
        calls.append("called")
        raise AssertionError("transport must not be called")

    monkeypatch.setenv("GATE_API_KEY", "sentinel-not-a-credential")
    without = attempt_acceptance(None, transport=transport)
    assert without["executed"] is False
    assert without["venue_calls"] == 0
    assert without["tenant_enabled"] is False
    assert without["secrets_discovered"] is False
    assert without["authenticated_testnet_pass"] is False
    assert calls == []
    assert "sentinel-not-a-credential" not in json.dumps(without)

    complete = _complete_manifest()
    checked = validate_manifest(complete)
    assert checked["structurally_complete"] is True
    assert checked["executable"] is False
    assert checked["window_executable"] is False
    with_manifest = attempt_acceptance(complete, transport=transport)
    assert with_manifest["executed"] is False
    assert with_manifest["account_mode_changed"] is False
    assert with_manifest["ledger_reset"] is False
    assert with_manifest["deployed"] is False
    assert calls == []

    template = json.loads((FIXTURE_DIR / "manifest.disabled.json").read_text(encoding="utf-8"))
    template_check = validate_manifest(template)
    assert template_check["structurally_complete"] is False
    assert template_check["executable"] is False
    assert attempt_acceptance(template, transport=transport)["executed"] is False

    from tools.short_release_acceptance.manifest import OUTPUT_DIR

    output = OUTPUT_DIR / "disabled-manifest.json"
    written = prepare_disabled_manifest(complete, output)
    assert written["executed"] is False
    saved = json.loads(output.read_text(encoding="utf-8"))
    output.unlink()
    assert saved["enabled"] is False
    assert saved["execution_authorized"] is False
    assert saved["executable"] is False

    with pytest.raises(ManifestError):
        prepare_disabled_manifest(
            _complete_manifest(api_key="not-printed"),
            tmp_path / "secret.json",
        )
    assert not (tmp_path / "secret.json").exists()
    with pytest.raises(ManifestError):
        prepare_disabled_manifest(complete, REPO / "data" / "short-release-must-not-write.json")
    with pytest.raises(ManifestError):
        prepare_disabled_manifest(complete, tmp_path / ".env")
    assert calls == []


def test_ac_e2e_4_manifest_rejects_unfrozen_or_secret_values():
    assert validate_manifest(_complete_manifest())["structurally_complete"] is True
    limits = _complete_manifest()["limits"]
    for field, value in (
        ("notional_pct_of_virtual_nav", "10.01"),
        ("stop_risk_pct", "0.26"),
        ("aggregate_stop_risk_pct", "0.76"),
        ("safety_cap_pct", "21"),
        ("leverage", "2"),
    ):
        changed = copy.deepcopy(limits)
        changed[field] = value
        result = validate_manifest(_complete_manifest(limits=changed))
        assert result["structurally_complete"] is False
        assert result["executable"] is False

    for origin in (
        "https://api-testnet.gateapi.io",
        "https://fx-api-testnet.gateio.ws:443",
        "http://api-testnet.gateapi.io:443",
        "https://api.gateio.ws:443",
    ):
        assert validate_manifest(_complete_manifest(origin=origin))["structurally_complete"] is False

    window = copy.deepcopy(_complete_manifest()["window"])
    window["execution_buffer_minutes"] = 15
    assert "unfrozen_value" in validate_manifest(_complete_manifest(window=window))["reasons"]
    missing_buffer = copy.deepcopy(window)
    missing_buffer["execution_buffer_minutes"] = None
    del missing_buffer["execution_buffer_minutes"]
    assert validate_manifest(_complete_manifest(window=missing_buffer))["structurally_complete"] is True

    for tenant in ("default", "henry", "Henry", ""):
        assert validate_manifest(_complete_manifest(tenant_id=tenant))["structurally_complete"] is False
    secret = _complete_manifest(api_key="not-printed")
    assert "secret_key" in validate_manifest(secret)["reasons"]
    assert "not-printed" not in json.dumps(validate_manifest(secret))
    for action in (["kill_switch"], ["ADL"], ["flatten"], ["deploy"]):
        assert validate_manifest(_complete_manifest(actions=action))["structurally_complete"] is False
    enabled = _complete_manifest(enabled=True, execution_authorized=True)
    refused = validate_manifest(enabled)
    assert refused["executable"] is False
    assert "enable_refused" in refused["reasons"]


def test_entry_snapshot_keys_match_issue_600():
    assert tuple(ENTRY_SNAPSHOT_KEYS) == tuple(_ENTRY_SNAPSHOT_KEYS)


def test_canonical_ac_text_matches_spec():
    body = SPEC.read_text(encoding="utf-8")
    begin = "<!-- AC-E2E-CANONICAL-BEGIN -->"
    end = "<!-- AC-E2E-CANONICAL-END -->"
    chunk = body.split(begin, 1)[1].split(end, 1)[0].strip("\n")
    assert chunk == CANONICAL_AC_TEXT.strip("\n")
    assert len(canonical_ac_hash()) == 64


def test_harness_imports_no_trading_or_network_stack():
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                assert root not in _FORBIDDEN_IMPORT_ROOTS, f"{path.name} imports {name}"
                assert name not in _FORBIDDEN_MODULES, f"{path.name} imports {name}"
        text = path.read_text(encoding="utf-8")
        assert "create_order" not in text
        assert "urlopen" not in text
        assert "importlib" not in text
        assert "import services.trading_service" not in text


def test_compose_does_not_import_trading_stack():
    script = """
import json, sys
from tools.short_release_acceptance import compose_offline_chain
compose_offline_chain(
    candidate={"asset_class": "perp", "as_of": "2026-11-10T12:00:00+00:00", "threshold_status": "unmeasured", "candidate_id": "c", "signal": None},
    config={},
    mode="paper",
    tenant_id="fixture-tenant",
    mode_proof={},
)
banned = ("execution", "ccxt", "requests", "urllib3", "pymongo", "storage", "services", "data_manager")
loaded = []
for name in sys.modules:
    for prefix in banned:
        if name == prefix or name.startswith(prefix + "."):
            loaded.append(name)
print(json.dumps(sorted(set(loaded))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []
