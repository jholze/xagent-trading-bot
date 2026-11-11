"""T220-02 Rev 2 protection contract. Synthetic venue only. Non-issued draft.

Maps ``specs/short-testnet-protection-v220/04-requirements.md``. The planner
is not wired into the adapter. Venue residual is supplied by the fixture;
this file does not import the unmerged #676 helper.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from execution.short_protection import (
    FlattenPermit,
    ProtectionInput,
    ProtectiveStop,
    Residual,
    plan_protection,
    protection_enabled,
)

TENANT = "tenant-a"
POSITION = "pos-1"


class SyntheticVenue:
    synthetic = True

    def __init__(self):
        self.calls: list[tuple] = []

    def create_stop(self, **kwargs):
        self.calls.append(("stop", kwargs))

    def create_open(self, **kwargs):
        self.calls.append(("open", kwargs))

    def flatten(self, **kwargs):
        self.calls.append(("flatten", kwargs))

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))


class RealShapedVenue(SyntheticVenue):
    synthetic = False


def _cfg(protection: bool | None = True) -> dict:
    block: dict = {"enabled": False, "tenants": []}
    if protection is not None:
        block["protection"] = protection
    return {
        "shorts": {
            "allow_live": False,
            "climax_fade": {"enabled": True},
            "testnet_futures": block,
        }
    }


def _residual(
    venue: str | None = "5",
    *,
    confirmed: bool = True,
    local: str | None = "5",
    owned: bool = True,
    position_id: str = POSITION,
) -> Residual:
    return Residual(
        position_id=position_id,
        tenant_id=TENANT,
        venue_qty=None if venue is None else Decimal(venue),
        venue_confirmed=confirmed,
        local_lot_qty=None if local is None else Decimal(local),
        owned=owned,
    )


def _inp(**overrides) -> ProtectionInput:
    base = dict(
        run_id="run-1",
        tenant_id=TENANT,
        residual=_residual(),
        stops=(),
        uncertain_open=False,
        want_open=False,
        cover_authorized=False,
        orphan_order_ids=(),
        orphan_position=False,
        permit=None,
        flatten_qty=None,
    )
    base.update(overrides)
    return ProtectionInput(**base)


def _stop(qty: str, status: str = "open", *, reduce_only: bool = True, stop_id: str = "s1") -> ProtectiveStop:
    return ProtectiveStop(stop_id, reduce_only, Decimal(qty), status)


def _permit(**overrides) -> FlattenPermit:
    base = dict(allow=True, run_id="run-1", tenant_id=TENANT, position_id=POSITION)
    base.update(overrides)
    return FlattenPermit(**base)


def test_ac_t02_missing_flag_submits_nothing_and_keeps_cover():
    for raw in (
        {},
        {"shorts": {"allow_live": True, "climax_fade": {"enabled": True}}},
        {"shorts": {"testnet_futures": {"enabled": True, "tenants": [TENANT]}}},
        _cfg(False),
        _cfg(None),
    ):
        assert protection_enabled(raw) is False
        client = SyntheticVenue()
        plan = plan_protection(
            raw,
            _inp(cover_authorized=True, want_open=True, flatten_qty=Decimal("5"), permit=_permit()),
            client,
        )
        assert plan.active is False
        assert plan.inhibits_entries is False
        assert plan.cover_allowed is True
        assert plan.protection_state == "inactive"
        assert plan.stop_orders == ()
        assert plan.flatten_orders == ()
        assert plan.opening_orders == ()
        assert client.calls == []


def test_ac_t02_confirmed_residual_gets_exactly_one_reduce_only_stop():
    client = SyntheticVenue()
    plan = plan_protection(_cfg(), _inp(), client)
    assert plan.protection_state == "place_stop"
    assert len(plan.stop_orders) == 1
    assert plan.stop_orders[0].qty == Decimal("5")
    assert plan.stop_orders[0].reduce_only is True
    assert plan.stop_orders[0].side == "buy"
    assert client.calls == [("stop", {
        "qty": "5",
        "reduce_only": True,
        "side": "buy",
        "position_id": POSITION,
    })]
    existing = plan_protection(_cfg(), _inp(stops=(_stop("5"),)))
    assert existing.protection_state == "protected"
    assert existing.stop_orders == ()
    duplicate = plan_protection(_cfg(), _inp(stops=(_stop("5", stop_id="s1"), _stop("5", stop_id="s2"))))
    assert duplicate.protection_state == "duplicate"
    assert duplicate.stop_orders == ()
    assert duplicate.cancel_ids == ()
    assert duplicate.inhibits_entries is True


def test_ac_t02_uncertain_create_does_not_open_again():
    client = SyntheticVenue()
    plan = plan_protection(
        _cfg(),
        _inp(uncertain_open=True, want_open=True, stops=(_stop("5"),)),
        client,
    )
    assert plan.opening_orders == ()
    assert plan.inhibits_entries is True
    assert "uncertain_open" in plan.reasons
    assert all(call[0] != "open" for call in client.calls)
    assert client.calls == []


def test_ac_t02_stop_timeout_reject_and_cancel_report_unprotected():
    for status, reason in (("timeout", "stop_timeout"), ("rejected", "stop_rejected"), ("canceled", "stop_canceled")):
        client = SyntheticVenue()
        plan = plan_protection(
            _cfg(),
            _inp(stops=(_stop("5", status),), cover_authorized=True),
            client,
        )
        assert plan.protection_state == "unprotected"
        assert plan.inhibits_entries is True
        assert reason in plan.reasons
        assert plan.cover_allowed is True
        assert plan.stop_orders == ()
        assert plan.flatten_orders == ()
        assert plan.opening_orders == ()
        assert client.calls == []


def test_ac_t02_unknown_venue_reports_unknown_and_does_not_flatten():
    client = SyntheticVenue()
    plan = plan_protection(
        _cfg(),
        _inp(residual=_residual(None, confirmed=False, local="5"), permit=_permit(), flatten_qty=Decimal("5")),
        client,
    )
    assert plan.protection_state == "unknown"
    assert plan.inhibits_entries is True
    assert plan.flatten_orders == ()
    assert plan.stop_orders == ()
    assert client.calls == []


def test_ac_t02_flatten_needs_a_scoped_permit_and_cannot_flip():
    client = SyntheticVenue()
    denied = plan_protection(_cfg(), _inp(flatten_qty=Decimal("5"), stops=(_stop("5"),)), client)
    assert denied.flatten_orders == ()
    assert "flatten_denied" in denied.reasons
    wrong = plan_protection(
        _cfg(),
        _inp(flatten_qty=Decimal("5"), permit=_permit(position_id="other"), stops=(_stop("5"),)),
        client,
    )
    assert wrong.flatten_orders == ()
    assert client.calls == []
    over = plan_protection(
        _cfg(),
        _inp(flatten_qty=Decimal("6"), permit=_permit(), stops=(_stop("5"),)),
        client,
    )
    assert over.flatten_orders == ()
    assert "would_flip" in over.reasons
    assert all(call[0] != "flatten" for call in client.calls)
    allowed = plan_protection(
        _cfg(),
        _inp(flatten_qty=Decimal("5"), permit=_permit(), stops=(_stop("5"),)),
        client,
    )
    assert len(allowed.flatten_orders) == 1
    assert allowed.flatten_orders[0].qty == Decimal("5")
    assert allowed.flatten_orders[0].reduce_only is True
    assert allowed.flatten_orders[0].side == "buy"
    assert ("flatten", {"qty": "5", "reduce_only": True, "side": "buy", "position_id": POSITION}) in client.calls


def test_ac_t02_partial_cover_and_adl_use_venue_residual_not_the_local_lot():
    cover = plan_protection(_cfg(), _inp(residual=_residual("6", local="10")))
    assert cover.stop_orders[0].qty == Decimal("6")
    assert cover.stop_orders[0].qty != Decimal("10")
    adl = plan_protection(_cfg(), _inp(residual=_residual("2", local="10")))
    assert adl.stop_orders[0].qty == Decimal("2")
    stale = plan_protection(_cfg(), _inp(residual=_residual("6", local="10"), stops=(_stop("10"),)))
    assert stale.protection_state == "unprotected"
    assert stale.stop_orders == ()
    assert stale.cancel_ids == ()
    assert "stop_qty_mismatch" in stale.reasons


def test_ac_t02_orphan_without_a_lot_blocks_entries_and_is_not_adopted():
    client = SyntheticVenue()
    plan = plan_protection(
        _cfg(),
        _inp(
            residual=_residual(confirmed=False, venue=None, local=None, owned=False, position_id=""),
            orphan_order_ids=("orphan-1",),
            orphan_position=True,
            cover_authorized=True,
        ),
        client,
    )
    assert plan.inhibits_entries is True
    assert plan.adopted is False
    assert plan.cancel_ids == ()
    assert plan.stop_orders == ()
    assert plan.cover_allowed is False
    assert client.calls == []
    owned = plan_protection(
        _cfg(),
        _inp(orphan_order_ids=("orphan-1",), cover_authorized=True),
        client,
    )
    assert owned.inhibits_entries is True
    assert owned.cancel_ids == ()
    assert owned.cover_allowed is True
    assert len(owned.stop_orders) == 1
    assert all(call[0] != "cancel" for call in client.calls)


def test_ac_t02_real_shaped_client_is_not_called():
    client = RealShapedVenue()
    plan = plan_protection(
        _cfg(),
        _inp(flatten_qty=Decimal("1"), permit=_permit()),
        client,
    )
    assert client.calls == []
    assert plan.submitted is False
    assert "client_not_synthetic" in plan.reasons


@pytest.mark.parametrize("status", ["NEW", "pending"])
def test_ac_t02_unknown_stop_status_does_not_plan_another_stop(status):
    plan = plan_protection(_cfg(), _inp(stops=(_stop("5", status),)))
    assert plan.protection_state == "unknown"
    assert plan.inhibits_entries is True
    assert plan.stop_orders == ()
    assert "stop_status_unknown" in plan.reasons


def test_ac_t02_open_stop_without_reduce_only_does_not_plan_another_stop():
    plan = plan_protection(_cfg(), _inp(stops=(_stop("5", reduce_only=False),)))
    assert plan.protection_state == "unknown"
    assert plan.inhibits_entries is True
    assert plan.stop_orders == ()


def test_ac_t02_tenant_mismatch_does_not_allow_cover():
    residual = _residual()
    plan = plan_protection(
        _cfg(),
        _inp(tenant_id="other-tenant", residual=residual, cover_authorized=True),
    )
    assert plan.protection_state == "unknown"
    assert "tenant_mismatch" in plan.reasons
    assert plan.cover_allowed is False
    assert plan.inhibits_entries is True


def test_ac_t02_truthy_synthetic_attribute_is_not_called():
    client = MagicMock()
    client.synthetic = "true"
    plan = plan_protection(_cfg(), _inp(), client)
    assert plan.submitted is False
    assert "client_not_synthetic" in plan.reasons
    client.create_stop.assert_not_called()
    client.flatten.assert_not_called()
    bare = MagicMock()
    bare_plan = plan_protection(_cfg(), _inp(), bare)
    assert bare_plan.submitted is False
    bare.create_stop.assert_not_called()


@pytest.mark.parametrize(
    "permit",
    [
        _permit(allow=False),
        _permit(allow="true"),
        _permit(run_id="other-run"),
        _permit(tenant_id="other-tenant"),
        _permit(run_id=""),
        _permit(tenant_id=""),
        _permit(position_id=""),
    ],
)
def test_ac_t02_permit_fields_must_match_literally(permit):
    client = SyntheticVenue()
    plan = plan_protection(
        _cfg(),
        _inp(flatten_qty=Decimal("5"), permit=permit, stops=(_stop("5"),)),
        client,
    )
    assert plan.flatten_orders == ()
    assert client.calls == []
