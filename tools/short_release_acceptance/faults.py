"""Safety invariants for the D-E2E fault matrix.

The judge does not invent an owner's deny reason. A trace without that
owner's result stays unverified. Emergency flatten is not accepted while
permit minutes and amounts are unmeasured.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tools.short_release_acceptance.constants import (
    FAULT_OWNERS,
    VERDICT_NO_GO,
    VERDICT_NOT_VERIFIED,
    to_decimal,
)


def _string_ids(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, list):
        return None
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        cleaned.append(item)
    return cleaned


def judge_fault(trace: Any) -> dict[str, Any]:
    if not isinstance(trace, dict):
        return _result(False, ["trace_malformed"], VERDICT_NO_GO, "dependency_open")
    fault = trace.get("fault")
    if fault not in FAULT_OWNERS:
        return _result(False, ["unknown_fault"], VERDICT_NO_GO, "dependency_open")
    if trace.get("owner_contract") != FAULT_OWNERS[fault]:
        return _result(False, ["owner_not_cited"], VERDICT_NOT_VERIFIED, "dependency_open")
    owner_result = trace.get("owner_result")
    if not isinstance(owner_result, dict) or owner_result.get("deny_new_entry") is not True:
        return _result(False, ["owner_result_missing"], VERDICT_NOT_VERIFIED, "dependency_open")

    client_ids = _string_ids(trace.get("client_order_ids"))
    orphan_ids = _string_ids(trace.get("orphan_ids"))
    cancel_ids = _string_ids(trace.get("cancel_ids"))
    reduce_orders = trace.get("reduce_orders")
    if (
        client_ids is None
        or orphan_ids is None
        or cancel_ids is None
        or (reduce_orders is not None and not isinstance(reduce_orders, list))
    ):
        return _result(False, ["trace_malformed"], VERDICT_NO_GO, "fixture")

    violations: list[str] = []
    if trace.get("opening_orders"):
        violations.append("new_entry")
    if trace.get("flatten_orders"):
        violations.append("flatten_not_accepted")
    if trace.get("cover_allowed") is not True:
        violations.append("cover_disabled")
    covers = trace.get("reduce_only_covers") or []
    if not isinstance(covers, list) or not covers:
        violations.append("reduce_only_cover_missing")
    elif any(not isinstance(item, dict) or item.get("reduce_only") is not True for item in covers):
        violations.append("reduce_only_cover_missing")
    if len(client_ids) != len(set(client_ids)):
        violations.append("duplicate")
    if trace.get("side") == "short":
        long_qty = to_decimal(trace.get("long_qty") if trace.get("long_qty") is not None else "0")
        if long_qty is None or long_qty > 0:
            violations.append("reversal")
    if trace.get("tenant_id") != trace.get("selected_tenant_id"):
        violations.append("wrong_tenant")
    if trace.get("foreign_ids_in_result"):
        violations.append("tenant_leak")
    if trace.get("cache_fallback") is True:
        violations.append("hidden_cache_fallback")

    residual = to_decimal(trace.get("residual_qty"))
    if residual is None:
        violations.append("residual_unknown")
    else:
        total = Decimal("0")
        for order in reduce_orders or []:
            if not isinstance(order, dict) or order.get("reduce_only") is not True:
                violations.append("reduce_order_invalid")
                continue
            qty = to_decimal(order.get("qty"))
            if qty is None or qty <= 0:
                violations.append("reduce_order_invalid")
                continue
            total += qty
        if total > residual:
            violations.append("over_reduce")
        stops = trace.get("effective_stop_ids") or []
        if residual > 0 and isinstance(stops, list) and len(stops) > 1:
            violations.append("multiple_stops")

    if fault == "unknown_cost" and trace.get("reported_net") is not None:
        violations.append("unknown_cost_as_number")
    if fault == "stop_rejection":
        if trace.get("protection_state") not in {"unprotected", "unknown", "rejected"}:
            violations.append("stop_state_hidden")
        if trace.get("inhibits_entries") is not True:
            violations.append("entries_not_inhibited")
    if fault == "uncertain_create" and trace.get("opening_orders"):
        violations.append("resubmit_after_uncertain_create")
    if fault == "restart_orphan":
        if trace.get("adopted") is True:
            violations.append("orphan_adopted")
        if set(orphan_ids) & set(cancel_ids):
            violations.append("orphan_canceled")
        if trace.get("inhibits_entries") is not True:
            violations.append("entries_not_inhibited")
    if fault in {"redis_timeout", "stale_source", "malformed_mode", "live_mode"} and trace.get(
        "inhibits_entries"
    ) is not True:
        violations.append("entries_not_inhibited")

    unique = list(dict.fromkeys(violations))
    if unique:
        return _result(False, unique, VERDICT_NO_GO, "fixture")
    return _result(True, [], VERDICT_NOT_VERIFIED, "fixture")


def _result(ok: bool, violations: list[str], verdict: str, status: str) -> dict[str, Any]:
    return {
        "invariant_ok": ok,
        "violations": violations,
        "release_verdict": verdict,
        "status": status,
        "authenticated_testnet_pass": False,
        "production_new_entries": 0,
        "venue_calls": 0,
    }
