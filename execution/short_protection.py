"""Default-off protective lifecycle for one confirmed short residual (T220-02).

Not wired into ``GateExecutionAdapter``. Missing
``shorts.testnet_futures.protection`` is off. Off submits nothing, does not
flatten, and does not withdraw a separately authorized COVER.

The planner never constructs an exchange client. A non-synthetic client is
not called. Opening orders are never submitted from here, including after an
uncertain create.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

_PROBLEM_STATUSES = frozenset({"timeout", "rejected", "canceled", "cancelled"})
_KNOWN_STOP_STATUSES = frozenset({"open"}) | _PROBLEM_STATUSES


@dataclass(frozen=True)
class Residual:
    position_id: str
    tenant_id: str
    venue_qty: Decimal | None
    venue_confirmed: bool
    local_lot_qty: Decimal | None
    owned: bool


@dataclass(frozen=True)
class ProtectiveStop:
    id: str
    reduce_only: bool
    qty: Decimal
    status: str


@dataclass(frozen=True)
class FlattenPermit:
    allow: bool
    run_id: str
    tenant_id: str
    position_id: str


@dataclass(frozen=True)
class ProtectionInput:
    run_id: str
    tenant_id: str
    residual: Residual
    stops: tuple[ProtectiveStop, ...] = ()
    uncertain_open: bool = False
    want_open: bool = False
    cover_authorized: bool = False
    orphan_order_ids: tuple[str, ...] = ()
    orphan_position: bool = False
    permit: FlattenPermit | None = None
    flatten_qty: Decimal | None = None


@dataclass(frozen=True)
class PlannedOrder:
    kind: str
    qty: Decimal
    reduce_only: bool
    side: str
    position_id: str


@dataclass(frozen=True)
class ProtectionPlan:
    active: bool
    inhibits_entries: bool
    cover_allowed: bool
    protection_state: str
    reasons: tuple[str, ...]
    stop_orders: tuple[PlannedOrder, ...] = ()
    flatten_orders: tuple[PlannedOrder, ...] = ()
    opening_orders: tuple[PlannedOrder, ...] = ()
    cancel_ids: tuple[str, ...] = ()
    adopted: bool = False
    submitted: bool = False


def protection_enabled(raw: Any) -> bool:
    """Literal True only. Missing, false, and other truthy values stay off."""
    block = _block(raw)
    if not isinstance(block, dict):
        return False
    return block.get("protection") is True


def plan_protection(
    raw: Any,
    incoming: ProtectionInput,
    client: Any = None,
) -> ProtectionPlan:
    plan = _decide(raw, incoming)
    if client is None:
        return plan
    if getattr(client, "synthetic", False) is not True:
        return replace(plan, reasons=plan.reasons + ("client_not_synthetic",), submitted=False)
    sent = False
    for order in plan.stop_orders:
        client.create_stop(
            qty=format(order.qty, "f"),
            reduce_only=order.reduce_only,
            side=order.side,
            position_id=order.position_id,
        )
        sent = True
    for order in plan.flatten_orders:
        client.flatten(
            qty=format(order.qty, "f"),
            reduce_only=order.reduce_only,
            side=order.side,
            position_id=order.position_id,
        )
        sent = True
    return replace(plan, submitted=sent)


def _decide(raw: Any, incoming: ProtectionInput) -> ProtectionPlan:
    if not protection_enabled(raw):
        return ProtectionPlan(
            active=False,
            inhibits_entries=False,
            cover_allowed=bool(incoming.cover_authorized),
            protection_state="inactive",
            reasons=("protection_off",),
        )
    reasons: list[str] = []
    orphans = bool(incoming.orphan_order_ids) or bool(incoming.orphan_position)
    owned = bool(incoming.residual.owned) and bool(str(incoming.residual.position_id or "").strip())
    if incoming.tenant_id != incoming.residual.tenant_id or not str(incoming.tenant_id or "").strip():
        denied = _unknown("tenant_mismatch", incoming, orphans, owned)
        return replace(denied, cover_allowed=False)
    if orphans and not owned:
        return ProtectionPlan(
            active=True,
            inhibits_entries=True,
            cover_allowed=False,
            protection_state="unknown",
            reasons=("orphan_unowned",),
            adopted=False,
        )
    if incoming.uncertain_open:
        reasons.append("uncertain_open")
    venue = _decimal(incoming.residual.venue_qty) if incoming.residual.venue_confirmed else None
    if not incoming.residual.venue_confirmed or venue is None or venue < 0:
        return _unknown("venue_unconfirmed", incoming, orphans, owned, extra=tuple(reasons))
    if orphans:
        reasons.append("orphan_present")
    if any(_stop_is_unreadable(stop) for stop in incoming.stops):
        reasons.append("stop_status_unknown")
        return ProtectionPlan(
            active=True,
            inhibits_entries=True,
            cover_allowed=bool(incoming.cover_authorized and owned),
            protection_state="unknown",
            reasons=tuple(reasons),
        )
    problem = _problem_reason(incoming.stops)
    effective = _effective_stops(incoming.stops, venue)
    inhibits = bool(incoming.uncertain_open or orphans or problem or len(effective) != 1)
    cover_allowed = bool(incoming.cover_authorized and owned)
    flatten_orders, flatten_reason = _flatten(incoming, venue, owned)
    if flatten_reason:
        reasons.append(flatten_reason)
    if venue == 0:
        return ProtectionPlan(
            active=True,
            inhibits_entries=inhibits and bool(orphans or incoming.uncertain_open),
            cover_allowed=cover_allowed,
            protection_state="flat",
            reasons=tuple(reasons) or ("flat",),
            flatten_orders=flatten_orders,
        )
    if problem and not effective:
        reasons.append(problem)
        return ProtectionPlan(
            active=True,
            inhibits_entries=True,
            cover_allowed=cover_allowed,
            protection_state="unprotected" if incoming.residual.venue_confirmed else "unknown",
            reasons=tuple(reasons),
            flatten_orders=flatten_orders,
        )
    if len(effective) > 1:
        reasons.append("duplicate_stop")
        return ProtectionPlan(
            active=True,
            inhibits_entries=True,
            cover_allowed=cover_allowed,
            protection_state="duplicate",
            reasons=tuple(reasons),
            flatten_orders=flatten_orders,
        )
    mismatched = _open_reduce_only(incoming.stops) and not effective
    if mismatched:
        reasons.append("stop_qty_mismatch")
        return ProtectionPlan(
            active=True,
            inhibits_entries=True,
            cover_allowed=cover_allowed,
            protection_state="unprotected",
            reasons=tuple(reasons),
            flatten_orders=flatten_orders,
        )
    if len(effective) == 1:
        return ProtectionPlan(
            active=True,
            inhibits_entries=bool(incoming.uncertain_open or orphans),
            cover_allowed=cover_allowed,
            protection_state="protected",
            reasons=tuple(reasons) or ("protected",),
            flatten_orders=flatten_orders,
        )
    reasons.append("place_stop")
    stop = PlannedOrder(
        "stop",
        venue,
        True,
        "buy",
        incoming.residual.position_id,
    )
    return ProtectionPlan(
        active=True,
        inhibits_entries=bool(incoming.uncertain_open or orphans),
        cover_allowed=cover_allowed,
        protection_state="place_stop",
        reasons=tuple(reasons),
        stop_orders=(stop,),
        flatten_orders=flatten_orders,
    )


def _unknown(
    reason: str,
    incoming: ProtectionInput,
    orphans: bool,
    owned: bool,
    extra: tuple[str, ...] = (),
) -> ProtectionPlan:
    cover_allowed = bool(incoming.cover_authorized and owned and not (orphans and not owned))
    return ProtectionPlan(
        active=True,
        inhibits_entries=True,
        cover_allowed=cover_allowed,
        protection_state="unknown",
        reasons=extra + (reason,),
    )


def _flatten(
    incoming: ProtectionInput,
    venue: Decimal,
    owned: bool,
) -> tuple[tuple[PlannedOrder, ...], str]:
    if incoming.flatten_qty is None and incoming.permit is None:
        return (), ""
    if not _permit_valid(incoming):
        return (), "flatten_denied"
    qty = _decimal(incoming.flatten_qty)
    if qty is None or qty <= 0 or not owned:
        return (), "flatten_denied"
    if qty > venue:
        return (), "would_flip"
    return (
        (PlannedOrder("flatten", qty, True, "buy", incoming.residual.position_id),),
        "",
    )


def _permit_valid(incoming: ProtectionInput) -> bool:
    permit = incoming.permit
    if permit is None or permit.allow is not True:
        return False
    if not all(str(part or "").strip() for part in (permit.run_id, permit.tenant_id, permit.position_id)):
        return False
    return (
        permit.run_id == incoming.run_id
        and permit.tenant_id == incoming.tenant_id
        and permit.position_id == incoming.residual.position_id
        and permit.tenant_id == incoming.residual.tenant_id
    )


def _stop_is_unreadable(stop: ProtectiveStop) -> bool:
    status = str(stop.status).strip().lower()
    if status not in _KNOWN_STOP_STATUSES:
        return True
    return status == "open" and stop.reduce_only is not True


def _effective_stops(stops: tuple[ProtectiveStop, ...], venue: Decimal) -> tuple[ProtectiveStop, ...]:
    found = []
    for stop in stops:
        if stop.reduce_only and str(stop.status).strip().lower() == "open" and _decimal(stop.qty) == venue:
            found.append(stop)
    return tuple(found)


def _open_reduce_only(stops: tuple[ProtectiveStop, ...]) -> bool:
    return any(stop.reduce_only and str(stop.status).strip().lower() == "open" for stop in stops)


def _problem_reason(stops: tuple[ProtectiveStop, ...]) -> str:
    for stop in stops:
        status = str(stop.status).strip().lower()
        if status in _PROBLEM_STATUSES:
            if status == "timeout":
                return "stop_timeout"
            if status == "rejected":
                return "stop_rejected"
            return "stop_canceled"
    return ""


def _block(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return None
    shorts = raw.get("shorts")
    if not isinstance(shorts, dict):
        return None
    return shorts.get("testnet_futures")


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not number.is_finite():
        return None
    return number
