"""#676 delta booking and restart. Does not change frozen finish_as assertions.

The production tracker is not wired to this helper yet. These tests pin the
contract the existing tracker should call.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import OrderStatus
from execution.partial_fill_delta import (
    FillSnapshot,
    apply_fill_snapshot,
    dump_state,
    empty_state,
    load_state,
    partial_open_needs_revisit,
)
from execution.recovery import _needs_order_reconcile


def _snap(qty: str, ids: tuple[str, ...], finish: str, requested: str = "10") -> FillSnapshot:
    return FillSnapshot(Decimal(qty), ids, finish, Decimal(requested))


def test_ac_676_first_partial_stays_tracked_and_books_once():
    state, delta = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), "open"))
    assert delta.accepted is True
    assert delta.qty == Decimal("4")
    assert delta.trade_ids == ("t1",)
    assert state.booked_qty == Decimal("4")
    assert state.phase == "open_partial"
    revisit = partial_open_needs_revisit(
        {"status": "partially_filled", "needs_reconcile": False, "finish_as": "open"}
    )
    assert revisit.eligible is True
    assert revisit.reason == "open_partial"
    assert _needs_order_reconcile(
        {"status": OrderStatus.PARTIALLY_FILLED.value, "needs_reconcile": False}
    ) is False


def test_ac_676_later_cumulative_books_only_the_new_quantity_and_trade_id():
    state, first = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), "open"))
    state, second = apply_fill_snapshot(state, _snap("7", ("t1", "t2"), "open"))
    assert first.qty == Decimal("4")
    assert second.accepted is True
    assert second.qty == Decimal("3")
    assert second.trade_ids == ("t2",)
    assert state.booked_qty == Decimal("7")
    assert state.trade_ids == frozenset({"t1", "t2"})


def test_ac_676_cumulative_increase_without_a_new_trade_id_does_not_book():
    state, _first = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), "open"))
    kept, delta = apply_fill_snapshot(state, _snap("7", ("t1",), "open"))
    assert delta.accepted is False
    assert delta.reason == "unknown_increment"
    assert delta.qty is None
    assert kept == state
    assert kept.booked_qty == Decimal("4")


def test_ac_676_restart_is_idempotent_after_state_roundtrip():
    state, _first = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), "open"))
    state, second = apply_fill_snapshot(state, _snap("7", ("t1", "t2"), "open"))
    assert second.qty == Decimal("3")
    restored = load_state(dump_state(state))
    replayed, delta = apply_fill_snapshot(restored, _snap("7", ("t1", "t2"), "open"))
    assert delta.accepted is True
    assert delta.qty == Decimal("0")
    assert delta.trade_ids == ()
    assert replayed.booked_qty == Decimal("7")
    assert replayed.trade_ids == restored.trade_ids


def test_ac_676_canceled_remainder_retains_filled_quantity():
    state, _first = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), "open"))
    state, delta = apply_fill_snapshot(state, _snap("4", ("t1",), "cancelled"))
    assert delta.qty == Decimal("0")
    assert state.phase == "canceled_remainder"
    assert state.booked_qty == Decimal("4")
    grown, extra = apply_fill_snapshot(state, _snap("6", ("t1", "t2"), "cancelled"))
    assert extra.reason == "terminal_growth"
    assert grown.booked_qty == Decimal("4")
    revisit = partial_open_needs_revisit(
        {"status": "partially_filled", "finish_as": "cancelled", "needs_reconcile": False}
    )
    assert revisit.eligible is False
    assert revisit.reason == "terminal"


def test_ac_676_cancel_snapshot_books_the_new_fill_then_keeps_it():
    state, _first = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), "open"))
    state, delta = apply_fill_snapshot(state, _snap("6", ("t1", "t2"), "cancelled"))
    assert delta.qty == Decimal("2")
    assert delta.trade_ids == ("t2",)
    assert state.booked_qty == Decimal("6")
    assert state.phase == "canceled_remainder"
    replayed, again = apply_fill_snapshot(load_state(dump_state(state)), _snap("6", ("t1", "t2"), "cancelled"))
    assert again.qty == Decimal("0")
    assert replayed.booked_qty == Decimal("6")


def test_ac_676_revisit_does_not_change_active_or_fee_unknown():
    active = {"status": OrderStatus.ACTIVE.value, "needs_reconcile": False}
    fee_unknown = {
        "status": OrderStatus.EXECUTED.value,
        "needs_reconcile": True,
        "fee_unknown": True,
    }
    assert _needs_order_reconcile(active) is True
    assert partial_open_needs_revisit(active).eligible is False
    assert partial_open_needs_revisit(active).reason == "not_partial"
    assert _needs_order_reconcile(fee_unknown) is True
    assert partial_open_needs_revisit(fee_unknown).eligible is False
    combined_active = _needs_order_reconcile(active) or partial_open_needs_revisit(active).eligible
    combined_fee = _needs_order_reconcile(fee_unknown) or partial_open_needs_revisit(fee_unknown).eligible
    assert combined_active is True
    assert combined_fee is True


def test_ac_676_unknown_qty_finish_or_trade_id_does_not_book_zero():
    state = empty_state()
    kept, missing_id = apply_fill_snapshot(state, _snap("4", (), "open"))
    assert missing_id.reason == "unknown_trade_id"
    assert missing_id.qty is None
    assert kept == state
    kept, bad_finish = apply_fill_snapshot(state, _snap("4", ("t1",), ""))
    assert bad_finish.reason == "unknown_finish"
    assert bad_finish.qty is None
    assert kept.booked_qty == Decimal("0")
    kept, regressed = apply_fill_snapshot(
        load_state({"booked_qty": "4", "trade_ids": ["t1"], "phase": "open_partial"}),
        _snap("3", ("t1",), "open"),
    )
    assert regressed.reason == "qty_regression"
    assert regressed.qty is None
    assert kept.booked_qty == Decimal("4")


@pytest.mark.parametrize("finish", ["ioc", "stp", "poc", "fok"])
def test_ac_676_positive_terminal_finish_books_the_delta(finish):
    state, delta = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), finish))
    assert delta.accepted is True
    assert delta.qty == Decimal("4")
    assert state.booked_qty == Decimal("4")
    if finish == "ioc":
        assert state.phase == "filled"
    else:
        assert state.phase == "canceled_remainder"


@pytest.mark.parametrize(
    "finish",
    ["trader_not_enough", "depth_not_enough", "small", "liquidate_cancelled"],
)
def test_ac_676_zero_fill_names_with_a_positive_qty_still_book(finish):
    state, delta = apply_fill_snapshot(empty_state(), _snap("4", ("t1",), finish))
    assert delta.qty == Decimal("4")
    assert state.phase == "canceled_remainder"
    assert state.booked_qty == Decimal("4")


def test_ac_676_stored_partial_without_finish_as_stays_eligible():
    stored = {
        "status": "partially_filled",
        "needs_reconcile": False,
        "filled_qty": 4,
        "symbol": "BASE/USDT",
        "execution": {"amount": 4, "filled_qty_gross": 4},
    }
    revisit = partial_open_needs_revisit(stored)
    assert revisit.eligible is True
    assert revisit.reason == "open_partial"
    assert "finish_as" not in stored


def test_ac_676_open_snapshot_that_completes_the_request_is_filled():
    state, delta = apply_fill_snapshot(empty_state(), _snap("10", ("t1",), "open", "10"))
    assert delta.qty == Decimal("10")
    assert state.phase == "filled"
    replayed, again = apply_fill_snapshot(load_state(dump_state(state)), _snap("10", ("t1",), "open", "10"))
    assert again.reason == "idempotent"
    assert again.qty == Decimal("0")
    assert replayed.booked_qty == Decimal("10")


@pytest.mark.parametrize(
    ("qty", "requested", "reason"),
    [
        (None, "10", "unknown_qty"),
        ("nan", "10", "unknown_qty"),
        ("-1", "10", "unknown_qty"),
        (True, "10", "unknown_qty"),
        ("4", "0", "unknown_qty"),
        ("4", "-1", "unknown_qty"),
        ("11", "10", "above_requested"),
    ],
)
def test_ac_676_unknown_or_impossible_quantities_leave_state_unchanged(qty, requested, reason):
    state = empty_state()
    cumulative = qty if qty is None or qty is True or qty == "nan" else Decimal(str(qty))
    if qty == "nan":
        cumulative = "nan"
    snap = FillSnapshot(cumulative, ("t1",), "open", Decimal(requested))
    kept, delta = apply_fill_snapshot(state, snap)
    assert delta.accepted is False
    assert delta.reason == reason
    assert delta.qty is None
    assert kept == state


def test_ac_676_duplicate_trade_id_missing_prior_id_and_zero_delta_with_a_new_id():
    state = empty_state()
    kept, duplicate = apply_fill_snapshot(state, _snap("4", ("t1", "t1"), "open"))
    assert duplicate.reason == "duplicate_trade_id"
    assert kept == state
    booked, _first = apply_fill_snapshot(state, _snap("4", ("t1",), "open"))
    kept, dropped = apply_fill_snapshot(booked, _snap("7", ("t2",), "open"))
    assert dropped.reason == "unknown_trade_id"
    assert kept == booked
    kept, extra = apply_fill_snapshot(booked, _snap("4", ("t1", "t2"), "open"))
    assert extra.reason == "unknown_increment"
    assert extra.qty is None
    assert kept == booked


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("nope", "not an object"),
        ({"booked_qty": "nan", "trade_ids": [], "phase": "unseen"}, "booked_qty"),
        ({"booked_qty": "1", "trade_ids": "t1", "phase": "unseen"}, "trade_ids"),
        ({"booked_qty": "1", "trade_ids": [""], "phase": "unseen"}, "unknown_trade_id"),
        ({"booked_qty": "1", "trade_ids": ["t1"], "phase": "nope"}, "phase"),
    ],
)
def test_ac_676_load_state_rejects_unknown_payloads(payload, message):
    with pytest.raises(ValueError, match=message):
        load_state(payload)
