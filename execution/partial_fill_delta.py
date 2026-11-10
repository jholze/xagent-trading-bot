"""Book only the new quantity and new trade ids of one Gate order (#676).

This is the missing delta rule for the existing order tracker. It does not
poll, does not write a portfolio, and is not called from
``execution/gate_adapter.py`` or ``execution/recovery.py`` while package A
holds those files. It is not behind a feature flag: #676 is a bug fix, and
the rule applies whenever a caller uses it.

``partial_open_needs_revisit`` is the extra predicate. Callers OR it with
``execution.recovery._needs_order_reconcile``. It does not replace the
ACTIVE or fee-unknown rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_OPEN = "open"
_CANCELED = frozenset({"cancelled", "canceled"})
_TERMINAL_FINISH = _CANCELED | frozenset({"filled", "ioc", "stp", "poc", "fok"})


@dataclass(frozen=True)
class TrackerState:
    booked_qty: Decimal
    trade_ids: frozenset[str]
    phase: str


@dataclass(frozen=True)
class FillSnapshot:
    cumulative_qty: Decimal
    trade_ids: tuple[str, ...]
    finish_as: str
    requested_qty: Decimal


@dataclass(frozen=True)
class FillDelta:
    accepted: bool
    reason: str
    qty: Decimal | None
    trade_ids: tuple[str, ...]


@dataclass(frozen=True)
class Revisit:
    eligible: bool
    reason: str


def empty_state() -> TrackerState:
    return TrackerState(Decimal("0"), frozenset(), "unseen")


def apply_fill_snapshot(
    state: TrackerState,
    snapshot: FillSnapshot,
) -> tuple[TrackerState, FillDelta]:
    """Apply one cumulative snapshot. Rejects leave ``state`` unchanged."""
    cumulative = _decimal(snapshot.cumulative_qty)
    requested = _decimal(snapshot.requested_qty)
    booked = _decimal(state.booked_qty)
    if cumulative is None or requested is None or booked is None:
        return state, FillDelta(False, "unknown_qty", None, ())
    if cumulative < 0 or requested <= 0 or booked < 0:
        return state, FillDelta(False, "unknown_qty", None, ())
    if cumulative > requested:
        return state, FillDelta(False, "above_requested", None, ())
    finish = str(snapshot.finish_as or "").strip().lower()
    if finish not in {_OPEN, "filled"} and finish not in _CANCELED:
        return state, FillDelta(False, "unknown_finish", None, ())
    ids, id_error = _trade_ids(snapshot.trade_ids)
    if id_error:
        return state, FillDelta(False, id_error, None, ())
    if cumulative < booked:
        return state, FillDelta(False, "qty_regression", None, ())
    if cumulative > 0 and not ids:
        return state, FillDelta(False, "unknown_trade_id", None, ())
    if booked > 0 and not state.trade_ids <= frozenset(ids):
        return state, FillDelta(False, "unknown_trade_id", None, ())
    phase = _phase_for(finish, cumulative, requested)
    if state.phase in {"canceled_remainder", "filled"}:
        if _same_terminal(state, cumulative, ids, phase):
            return state, FillDelta(True, "idempotent", Decimal("0"), ())
        return state, FillDelta(False, "terminal", None, ())
    new_ids = tuple(trade_id for trade_id in ids if trade_id not in state.trade_ids)
    delta = cumulative - booked
    if delta == 0 and not new_ids:
        kept = TrackerState(booked, state.trade_ids, phase)
        return kept, FillDelta(True, "idempotent", Decimal("0"), ())
    if delta == 0 or not new_ids:
        return state, FillDelta(False, "unknown_increment", None, ())
    updated = TrackerState(cumulative, state.trade_ids | frozenset(new_ids), phase)
    return updated, FillDelta(True, "booked", delta, new_ids)


def partial_open_needs_revisit(order: dict) -> Revisit:
    """True only for a non-terminal partial that the old flag would drop."""
    if not isinstance(order, dict):
        return Revisit(False, "not_partial")
    status = str(order.get("status") or "").strip().lower()
    if status not in {"partially_filled", "partial"}:
        return Revisit(False, "not_partial")
    finish = str(order.get("finish_as") or "").strip().lower()
    if finish in _TERMINAL_FINISH:
        return Revisit(False, "terminal")
    if finish == _OPEN:
        return Revisit(True, "open_partial")
    return Revisit(False, "unknown_finish")


def dump_state(state: TrackerState) -> dict:
    return {
        "booked_qty": format(state.booked_qty, "f"),
        "trade_ids": sorted(state.trade_ids),
        "phase": state.phase,
    }


def load_state(payload: dict) -> TrackerState:
    if not isinstance(payload, dict):
        raise ValueError("state payload is not an object")
    booked = _decimal(payload.get("booked_qty"))
    if booked is None or booked < 0:
        raise ValueError("booked_qty is unknown")
    raw_ids = payload.get("trade_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("trade_ids is unknown")
    ids, error = _trade_ids(tuple(raw_ids))
    if error:
        raise ValueError(error)
    phase = payload.get("phase")
    if phase not in {"unseen", "open_partial", "canceled_remainder", "filled"}:
        raise ValueError("phase is unknown")
    return TrackerState(booked, frozenset(ids), phase)


def _phase_for(finish: str, cumulative: Decimal, requested: Decimal) -> str:
    if finish in _CANCELED:
        return "canceled_remainder"
    if finish == "filled" or (requested > 0 and cumulative == requested):
        return "filled"
    return "open_partial"


def _same_terminal(
    state: TrackerState,
    cumulative: Decimal,
    ids: tuple[str, ...],
    phase: str,
) -> bool:
    return (
        state.phase == phase
        and state.booked_qty == cumulative
        and state.trade_ids == frozenset(ids)
    )


def _trade_ids(raw: tuple) -> tuple[tuple[str, ...], str]:
    found: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return (), "unknown_trade_id"
        trade_id = item.strip()
        if trade_id in seen:
            return (), "duplicate_trade_id"
        seen.add(trade_id)
        found.append(trade_id)
    return tuple(found), ""


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not number.is_finite():
        return None
    return number
