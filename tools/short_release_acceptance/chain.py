"""Offline rehearsal of the short acceptance chain.

The rehearsal links caller-supplied fixture stages. It does not import the
trading service, submit an order, or treat a linked fixture as acceptance.
Production admission stays closed: every frozen regime cell is shut, the
integration hook is unwired, and open parameters fail closed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tools.short_release_acceptance.constants import (
    AC_REVISION,
    ALLOWED_ASSET_CLASSES,
    ENTRY_SNAPSHOT_KEYS,
    FROZEN_CLEARANCE,
    FROZEN_MAX_HOLDING_HOURS,
    FROZEN_TOLERANCE,
    FIXTURE_MODES,
    ISSUE_412_STATE,
    OPEN_PARAMETERS,
    OWNER_BY_STAGE,
    RELEASE_FLAG_KEY,
    REQUIRED_STAGES,
    VERDICT_NO_GO,
    VERDICT_NOT_VERIFIED,
    to_decimal,
)
from tools.short_release_acceptance.manifest import civil_window_reasons, parse_aware
from tools.short_release_acceptance.owners import owner_status
from tools.short_release_acceptance.schema import canonical_ac_hash

# Phase 2 may wire services/trading_service.py. This package must not.
INTEGRATION_WIRED = False


def release_enabled(raw: Any) -> bool:
    """Literal True only. Missing, false, and the legacy climax flag do not count."""
    if not isinstance(raw, dict):
        return False
    block = raw.get(RELEASE_FLAG_KEY)
    if not isinstance(block, dict):
        return False
    return block.get("enabled") is True


def _allow_live(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    shorts = raw.get("shorts")
    if not isinstance(shorts, dict):
        return False
    return shorts.get("allow_live") is True


def _mode_proof_ok(proof: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    if proof.get("readback") is not True:
        return False
    if proof.get("source") == "set_response":
        return False
    if proof.get("margin_mode") != "isolated":
        return False
    if proof.get("position_mode") not in {"one_way", "one-way", "single"}:
        return False
    return to_decimal(proof.get("leverage")) == Decimal("1")


def assess_clearance(block: Any) -> str:
    """Consistency of a supplied short stop. Does not invent a liquidation price."""
    if not isinstance(block, dict):
        return "absent"
    if block.get("venue_mark_present") is not True or block.get("modeled_liquidation") is True:
        return "missing_venue"
    entry = to_decimal(block.get("entry"))
    liquidation = to_decimal(block.get("liquidation"))
    tick = to_decimal(block.get("tick"))
    stop = to_decimal(block.get("stop"))
    fraction = to_decimal(block.get("fraction"))
    if None in (entry, liquidation, tick, stop, fraction):
        return "missing_venue"
    if fraction < FROZEN_CLEARANCE:
        return "loosened"
    if tick <= 0 or entry <= 0 or liquidation <= entry:
        return "missing_venue"
    required = max(tick, fraction * (liquidation - entry))
    gap = liquidation - stop
    if not (entry < stop < liquidation) or gap < required:
        return "too_close"
    return "ok"


def _payload(stage: dict[str, Any]) -> dict[str, Any]:
    payload = stage.get("payload")
    return payload if isinstance(payload, dict) else {}


def _assess_fills(payload: dict[str, Any]) -> tuple[list[str], Decimal | None]:
    reasons: list[str] = []
    snapshots = payload.get("snapshots")
    if payload.get("confirmed") is not True:
        reasons.append("fill_unconfirmed")
    if not isinstance(snapshots, list) or not snapshots:
        reasons.append("fill_missing")
        return reasons, None
    seen: list[str] = []
    cumulative = Decimal("0")
    malformed = False
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            reasons.append("fill_qty_invalid")
            malformed = True
            continue
        trade_ids = snapshot.get("trade_ids")
        if not isinstance(trade_ids, list):
            reasons.append("fill_malformed")
            malformed = True
            continue
        if not trade_ids:
            reasons.append("fill_missing_trade_id")
            malformed = True
            continue
        if any(not isinstance(trade_id, str) or not trade_id for trade_id in trade_ids):
            reasons.append("fill_malformed")
            malformed = True
            continue
        for trade_id in trade_ids:
            if trade_id in seen:
                reasons.append("duplicate_trade_id")
            seen.append(trade_id)
        delta = to_decimal(snapshot.get("delta_qty"))
        reported = to_decimal(snapshot.get("cumulative_qty"))
        if delta is None or reported is None or delta <= 0:
            reasons.append("fill_qty_invalid")
            malformed = True
            continue
        cumulative += delta
        if reported != cumulative:
            reasons.append("fill_cumulative_mismatch")
            malformed = True
    if malformed:
        return reasons, None
    return reasons, cumulative


def _assess_ledger(payload: dict[str, Any], reasons: list[str]) -> str:
    components = payload.get("components")
    unknown = payload.get("unknown_components") or []
    if not isinstance(components, list) or not components:
        reasons.append("ledger_missing")
        return "absent"
    amounts: list[Decimal] = []
    component_unknown = bool(unknown)
    for component in components:
        if not isinstance(component, dict):
            component_unknown = True
            continue
        amount = to_decimal(component.get("amount"))
        if amount is None:
            component_unknown = True
        else:
            amounts.append(amount)
    reported = payload.get("reported_net")
    if component_unknown:
        if reported is not None:
            reasons.append("unknown_cost_as_number")
        return "unmeasurable"
    total = sum(amounts, Decimal("0"))
    reported_amount = to_decimal(reported)
    if reported_amount is None or reported_amount != total:
        reasons.append("net_mismatch")
        return "mismatch"
    if "venue_net" in payload and payload.get("venue_net") is not None:
        venue = to_decimal(payload.get("venue_net"))
        tolerance = to_decimal(payload.get("tolerance_usdt"))
        if tolerance is None:
            tolerance = FROZEN_TOLERANCE
        if tolerance <= 0 or tolerance > FROZEN_TOLERANCE:
            reasons.append("tolerance_widened")
            return "mismatch"
        if venue is None or abs(reported_amount - venue) > tolerance:
            reasons.append("tolerance_exceeded")
            return "mismatch"
    return "consistent"


def _assess_trace(payload: dict[str, Any], decision: dict[str, Any], reasons: list[str]) -> None:
    snapshot = payload.get("entry_snapshot")
    if not isinstance(snapshot, dict):
        reasons.append("snapshot_missing")
        return
    extras = [key for key in snapshot if key not in ENTRY_SNAPSHOT_KEYS and key != "missing_reasons"]
    if extras:
        reasons.append("snapshot_schema_extended")
    missing_reasons = snapshot.get("missing_reasons")
    if not isinstance(missing_reasons, dict):
        missing_reasons = {}
    for key in ENTRY_SNAPSHOT_KEYS:
        if key not in snapshot:
            reasons.append("snapshot_key_missing")
        elif snapshot.get(key) is None and not missing_reasons.get(key):
            reasons.append("missing_reason_absent")
    for field in ("rsi", "atr", "volume_factor", "source"):
        if decision.get(field) != snapshot.get(field):
            reasons.append("lineage_mismatch")
    links = payload.get("exit_links")
    if not isinstance(links, list) or not links:
        reasons.append("exit_unlinked")
        return
    for link in links:
        if not isinstance(link, dict):
            reasons.append("exit_unlinked")
            continue
        if not link.get("entry_order_id") or not link.get("lot_id") or not link.get("exit_id"):
            reasons.append("exit_unlinked")
        if set(link) <= {"symbol", "time"}:
            reasons.append("symbol_time_heuristic")


def _assess_stages(
    stages: Any,
    *,
    tenant_id: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    cover_allowed = True
    if not isinstance(stages, list):
        return {
            "linked": False,
            "reasons": ["stages_missing"],
            "net_status": "absent",
            "cover_allowed": True,
            "accepted_ids": [],
        }
    by_name: dict[str, dict[str, Any]] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            reasons.append("stage_malformed")
            continue
        name = stage.get("stage")
        if not isinstance(name, str):
            reasons.append("stage_malformed")
            continue
        if name in by_name:
            reasons.append("duplicate_stage")
        by_name[name] = stage
        if stage.get("tenant_id") != tenant_id:
            reasons.append("wrong_tenant")
    for name in REQUIRED_STAGES:
        if name not in by_name:
            reasons.append(f"missing_stage:{name}")
        else:
            expected = OWNER_BY_STAGE[name]
            if by_name[name].get("owner_contract") != expected:
                reasons.append(f"owner_mismatch:{name}")
    correlation_id = candidate.get("candidate_id")
    seen_ids: list[str] = []
    previous: str | None = None
    for name in REQUIRED_STAGES:
        stage = by_name.get(name)
        if stage is None:
            continue
        if stage.get("correlation_id") != correlation_id or not correlation_id:
            reasons.append("correlation_mismatch")
        stage_id = stage.get("stage_id")
        if not isinstance(stage_id, str) or not stage_id or stage_id in seen_ids:
            reasons.append("unstable_id")
        else:
            seen_ids.append(stage_id)
        if name == "scanner":
            if stage.get("parent_id") not in (None, correlation_id):
                reasons.append("parent_break")
        elif stage.get("parent_id") != previous:
            reasons.append("parent_break")
        previous = stage_id if isinstance(stage_id, str) else previous

    scanner = by_name.get("scanner")
    if scanner is not None:
        payload = _payload(scanner)
        if payload.get("signal") is not None:
            reasons.append("signal_not_allowed")
        if payload.get("threshold_status") != "unmeasured":
            reasons.append("threshold_not_frozen")
        if payload.get("candidate_id") != candidate.get("candidate_id"):
            reasons.append("candidate_id_mismatch")
    decision = by_name.get("decision")
    decision_payload = _payload(decision) if decision else {}
    if decision is not None:
        if decision_payload.get("decision") != "short_candidate":
            reasons.append("decision_missing")
        if decision_payload.get("risk_denied") is True:
            reasons.append("risk_denied")
    intent = by_name.get("short_intent")
    if intent is not None:
        payload = _payload(intent)
        if payload.get("side") != "short":
            reasons.append("not_short")
        if payload.get("order_submitted") is True:
            reasons.append("order_submitted")
    fill = by_name.get("fill")
    filled_qty: Decimal | None = None
    if fill is not None:
        fill_reasons, filled_qty = _assess_fills(_payload(fill))
        reasons.extend(fill_reasons)
    protection = by_name.get("protection")
    if protection is not None:
        payload = _payload(protection)
        if payload.get("reduce_only") is not True:
            reasons.append("protection_not_reduce_only")
        if payload.get("opening_order_ids"):
            reasons.append("protection_opened")
        if payload.get("flatten_order_ids"):
            reasons.append("unauthorized_flatten")
        residual = to_decimal(payload.get("confirmed_residual_qty"))
        stops = payload.get("effective_stop_ids") or []
        if residual is None or residual <= 0:
            reasons.append("residual_unconfirmed")
        elif not isinstance(stops, list) or len(stops) != 1:
            reasons.append("stop_count")
        clearance = assess_clearance(payload.get("clearance"))
        if clearance != "ok":
            reasons.append(f"clearance_{clearance}")
        if filled_qty is not None and residual is not None and residual > 0:
            tick = None
            clearance_block = payload.get("clearance")
            if isinstance(clearance_block, dict):
                tick = to_decimal(clearance_block.get("tick"))
            limit = FROZEN_TOLERANCE
            if tick is not None and tick > 0:
                limit = min(tick, FROZEN_TOLERANCE)
            if abs(filled_qty - residual) > limit:
                reasons.append("residual_fill_mismatch")
    cover = by_name.get("cover")
    if cover is not None:
        payload = _payload(cover)
        if payload.get("reduce_only") is not True:
            reasons.append("cover_not_reduce_only")
            cover_allowed = False
        holding = to_decimal(payload.get("holding_hours"))
        cap = to_decimal(payload.get("cap_hours"))
        if (
            holding is None
            or cap is None
            or holding <= 0
            or cap <= 0
            or holding > cap
            or cap > FROZEN_MAX_HOLDING_HOURS
            or holding >= Decimal("24")
        ):
            reasons.append("holding_cap")
        entry_at = payload.get("entry_at")
        exit_at = payload.get("exit_at")
        if entry_at is None or exit_at is None:
            reasons.append("window_unattested")
        else:
            start = parse_aware(entry_at)
            end = parse_aware(exit_at)
            if start is None or end is None or holding is None:
                reasons.append("holding_mismatch")
            else:
                elapsed = Decimal(str((end - start).total_seconds()))
                if abs(elapsed - (holding * Decimal("3600"))) > 1:
                    reasons.append("holding_mismatch")
            reasons.extend(
                civil_window_reasons(
                    entry_at,
                    exit_at,
                    payload.get("settlement_at"),
                    payload.get("due_at"),
                    require_funding=payload.get("settlement_at") is not None,
                )
            )
    ledger = by_name.get("ledger")
    net_status = "absent"
    if ledger is not None:
        net_status = _assess_ledger(_payload(ledger), reasons)
    trace = by_name.get("trace")
    if trace is not None:
        _assess_trace(_payload(trace), decision_payload, reasons)
        cover_id = cover.get("stage_id") if isinstance(cover, dict) else None
        fill_id = fill.get("stage_id") if isinstance(fill, dict) else None
        for link in _payload(trace).get("exit_links") or []:
            if not isinstance(link, dict):
                reasons.append("exit_unlinked")
                continue
            if link.get("entry_order_id") != fill_id or link.get("exit_id") != cover_id:
                reasons.append("exit_target_mismatch")
    accepted_ids = seen_ids if "wrong_tenant" not in reasons else []
    return {
        "linked": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "net_status": net_status,
        "cover_allowed": cover_allowed,
        "accepted_ids": accepted_ids,
    }


def _gate_reasons(candidate: Any, config: Any, mode: Any, mode_proof: Any) -> list[str]:
    reasons: list[str] = []
    if not isinstance(candidate, dict):
        return ["candidate_missing"]
    if not release_enabled(config):
        reasons.append("flag_off")
    if mode == "live" or _allow_live(config):
        reasons.append("live_denied")
    if mode not in FIXTURE_MODES:
        reasons.append("malformed_mode")
    if not _mode_proof_ok(mode_proof):
        reasons.append("mode_proof_rejected")
    if candidate.get("asset_class") not in ALLOWED_ASSET_CLASSES:
        reasons.append("asset_class_denied")
    if candidate.get("stale") is True or not candidate.get("as_of"):
        reasons.append("stale_source")
    if candidate.get("signal") is not None:
        reasons.append("signal_not_allowed")
    if candidate.get("threshold_status") != "unmeasured":
        reasons.append("threshold_not_frozen")
    if candidate.get("malformed") is True:
        reasons.append("candidate_malformed")
    return reasons


def compose_offline_chain(
    *,
    candidate: Any,
    config: Any,
    mode: Any,
    tenant_id: str,
    mode_proof: Any,
    stages: Any = None,
    candidate_sha: str | None = None,
    scope: str = "fixture_plumbing",
) -> dict[str, Any]:
    """Judge one offline chain. Network calls and data writes stay at zero."""
    gate = _gate_reasons(candidate, config, mode, mode_proof)
    structural = _assess_stages(
        stages,
        tenant_id=tenant_id,
        candidate=candidate if isinstance(candidate, dict) else {},
    )
    status = owner_status()
    blocked = tuple(contract for contract, state in status.items() if state == "missing")
    rehearsed = (
        1 if scope == "fixture_plumbing" and not gate and structural["linked"] else 0
    )
    no_go_reasons = {
        "fill_malformed",
        "stage_malformed",
        "residual_fill_mismatch",
        "exit_target_mismatch",
    }
    release_verdict = (
        VERDICT_NO_GO
        if no_go_reasons.intersection(structural["reasons"])
        else VERDICT_NOT_VERIFIED
    )
    evidence_row = {
        "row_id": "ac-e2e-1-compose",
        "ac_id": "AC-E2E-1",
        "kind": "fixture",
        "candidate_sha": candidate_sha,
        "ac_revision": AC_REVISION,
        "ac_revision_hash": canonical_ac_hash(),
        "test_status": "not_run",
        "reviewers": [],
        "tester_status": "not_run",
        "tester_head": None,
        "tester_model": None,
        "funding_proof": None,
        "producer": "short_release_acceptance.compose",
        "acceptance_executed_by_this_package": False,
        "issue_412_action": "leave_closed_not_planned",
        "authenticated_testnet_pass": False,
        "notes": "Offline fixture rehearsal. Not an authenticated testnet result.",
    }
    return {
        "rehearsed_short_intents": rehearsed,
        "production_new_entries": 0,
        "entry_admitted": False,
        "production_regime_admission": False,
        "regime_reason": "no_open_cell",
        "venue_calls": 0,
        "network_calls": 0,
        "data_writes": 0,
        "orders_submitted": 0,
        "integration_wired": INTEGRATION_WIRED,
        "release_verdict": release_verdict,
        "authenticated_testnet_pass": False,
        "issue_412": ISSUE_412_STATE,
        "structural_rehearsal_ok": structural["linked"],
        "structural_reasons": structural["reasons"],
        "gate_reasons": gate,
        "net_status": structural["net_status"],
        "cover_allowed": structural["cover_allowed"],
        "accepted_ids": structural["accepted_ids"],
        "owner_status": status,
        "blocked_dependencies": blocked,
        "open_parameters": OPEN_PARAMETERS,
        "evidence_row": evidence_row,
        "acceptance_blocked": True,
    }
