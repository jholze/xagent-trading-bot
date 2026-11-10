"""T220-03 draft: missing risk profile denies. No AC-03a/b/c numbers or regime table.

Synthetic slot values below are test tokens, not production limits and not
copies of Owner-Freeze-Werte.md. AC-03d stock exclusion is not exercised as
a ban: stock and perp must receive the same reasons.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategies.short_admission import (
    AdmissionDecision,
    cover_permitted_when_entry_denied,
    evaluate_short_admission,
    sandbox_admission_enabled,
)

_REPO = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)

_PAPER_PROOF = {
    "tenant_id": "fixture-tenant",
    "market": "paper",
    "live": False,
    "readback_ok": True,
    "as_of": "2026-11-10T15:00:00+00:00",
}

_PROFILE = {
    "profile_id": "fixture-profile",
    "leverage": "2",
    "notional": "10",
    "margin": "5",
    "stop_risk": "1",
    "nav": "100",
    "metadata": {"schema": "fixture"},
    "reservations": {"pending": "0", "uncertain": "0", "reserved": "0"},
}


def _on() -> dict:
    return {"shorts": {"sandbox_admission_v220": {"enabled": True}, "allow_live": False}}


def _decide(profile=_PROFILE, *, candidate=None, **kwargs) -> AdmissionDecision:
    payload = {
        "config_raw": _on(),
        "profile": profile,
        "mode_proof": _PAPER_PROOF,
        "now": _NOW,
    }
    payload.update(kwargs)
    return evaluate_short_admission(
        candidate if candidate is not None else {"symbol": "AAA/USDT"},
        **payload,
    )


def test_ac1_flag_missing_or_not_true_denies():
    assert sandbox_admission_enabled({}) is False
    assert sandbox_admission_enabled({"shorts": {}}) is False
    assert sandbox_admission_enabled({"shorts": {"sandbox_admission_v220": {}}}) is False
    assert sandbox_admission_enabled(
        {"shorts": {"sandbox_admission_v220": {"enabled": False}}}
    ) is False
    assert sandbox_admission_enabled(
        {"shorts": {"sandbox_admission_v220": {"enabled": "true"}}}
    ) is False
    climax = {"shorts": {"climax_fade": {"enabled": True}, "allow_live": False}}
    assert sandbox_admission_enabled(climax) is False
    decision = evaluate_short_admission(
        {"symbol": "AAA/USDT"},
        config_raw=climax,
        profile=_PROFILE,
        mode_proof=_PAPER_PROOF,
        now=_NOW,
    )
    assert decision.admitted is False
    assert decision.action == "none"
    assert decision.reasons == ("sandbox_disabled",)
    assert decision.parameters == ()
    assert decision.regime_applied is False
    assert decision.stock_exclusion_applied is False


def test_committed_config_is_unchanged():
    raw = json.loads((_REPO / "config.json").read_text(encoding="utf-8"))
    shorts = raw["shorts"]
    assert shorts["allow_live"] is False
    assert "sandbox_admission_v220" not in shorts
    assert sandbox_admission_enabled(raw) is False


def _assert_profile_missing(profile):
    decision = _decide(profile)
    assert decision.admitted is False
    assert decision.action == "none"
    assert "risk_profile_missing" in decision.reasons
    assert decision.regime_applied is False
    assert "owner_freeze_pending" not in decision.reasons


@pytest.mark.parametrize(
    "profile",
    [None, {}, "unknown", [], {"profile_id": "unknown"}],
)
def test_ac2_missing_risk_profile_denies(profile):
    _assert_profile_missing(profile)


def test_ac2_missing_slot_is_not_filled():
    partial = {"leverage": "2", "notional": Decimal("10")}
    decision = _decide(partial)
    assert decision.admitted is False
    assert "risk_profile_missing" in decision.reasons
    echoed = dict(decision.parameters)
    assert echoed["leverage"] == "2"
    assert echoed["notional"] == "10"
    for slot in ("margin", "stop_risk", "nav", "reservations", "profile_id", "metadata"):
        assert slot not in echoed
    assert "0" not in echoed.values()


@pytest.mark.parametrize(
    "slot",
    ["profile_id", "leverage", "notional", "margin", "stop_risk", "nav", "metadata", "reservations"],
)
def test_ac2_each_absent_slot_denies(slot):
    profile = dict(_PROFILE)
    profile.pop(slot)
    decision = _decide(profile)
    assert "risk_profile_missing" in decision.reasons
    assert slot not in dict(decision.parameters)


def test_ac2_unknown_reservation_is_not_zero():
    profile = dict(_PROFILE)
    profile["reservations"] = {"pending": "1", "reserved": "1"}
    decision = _decide(profile)
    assert "risk_profile_missing" in decision.reasons
    assert "reservations" not in dict(decision.parameters)


def test_ac2_float_slot_is_not_a_profile():
    profile = dict(_PROFILE)
    profile["leverage"] = 2.0
    decision = _decide(profile)
    assert "risk_profile_missing" in decision.reasons
    assert "leverage" not in dict(decision.parameters)


def test_ac3_supplied_profile_is_plumbed_not_admitted():
    profile = {
        "profile_id": "fixture-profile",
        "leverage": Decimal("2"),
        "notional": "10",
        "margin": 5,
        "stop_risk": "1",
        "nav": "100",
        "metadata": {"schema": "fixture", "source": "unit"},
        "reservations": {"pending": "0", "uncertain": Decimal("0"), "reserved": 0},
        "regime": "RANGE",
        "ignored_extra": "kept",
    }
    decision = _decide(profile)
    assert decision.admitted is False
    assert decision.action == "none"
    assert decision.reasons == ("owner_freeze_pending",)
    assert decision.regime_applied is False
    assert decision.stock_exclusion_applied is False
    echoed = dict(decision.parameters)
    assert echoed["leverage"] == "2"
    assert echoed["notional"] == "10"
    assert echoed["margin"] == "5"
    assert echoed["stop_risk"] == "1"
    assert echoed["nav"] == "100"
    assert echoed["metadata"] == "schema=fixture,source=unit"
    assert echoed["reservations"] == "pending=0,uncertain=0,reserved=0"
    assert echoed["regime"] == "RANGE"
    assert echoed["ignored_extra"] == "kept"
    later = _decide(profile, now=datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc))
    assert later.reasons == decision.reasons
    assert later.parameters == decision.parameters


def test_ac3_regime_label_is_not_a_table():
    left = dict(_PROFILE)
    right = dict(_PROFILE)
    left["regime"] = "RANGE"
    right["regime"] = "SQUEEZE"
    a = _decide(left)
    b = _decide(right)
    assert a.reasons == b.reasons == ("owner_freeze_pending",)
    assert a.regime_applied is False and b.regime_applied is False
    assert dict(a.parameters)["regime"] == "RANGE"
    assert dict(b.parameters)["regime"] == "SQUEEZE"


def test_ac4_live_and_missing_proof_deny():
    live_cfg = {"shorts": {"sandbox_admission_v220": {"enabled": True}, "allow_live": True}}
    live = _decide(config_raw=live_cfg)
    assert live.admitted is False
    assert "live_denied" in live.reasons

    proof_live = dict(_PAPER_PROOF)
    proof_live["live"] = True
    marked = _decide(mode_proof=proof_live)
    assert "live_denied" in marked.reasons

    mainnet = dict(_PAPER_PROOF)
    mainnet["market"] = "mainnet"
    assert "live_denied" in _decide(mode_proof=mainnet).reasons

    missing = _decide(mode_proof=None)
    assert missing.admitted is False
    assert "mode_proof_missing" in missing.reasons
    assert "owner_freeze_pending" not in missing.reasons

    no_readback = dict(_PAPER_PROOF)
    del no_readback["readback_ok"]
    unread = _decide(mode_proof=no_readback)
    assert "mode_proof_missing" in unread.reasons

    empty_tenant = dict(_PAPER_PROOF)
    empty_tenant["tenant_id"] = ""
    assert "mode_proof_missing" in _decide(mode_proof=empty_tenant).reasons


def test_ac4_long_is_not_flipped():
    decision = _decide(
        candidate={"symbol": "AAA/USDT", "existing_side": "long", "existing_qty": "1"}
    )
    assert decision.admitted is False
    assert decision.action == "none"
    assert "long_not_flipped" in decision.reasons
    assert decision.action not in {"flip", "sell", "short", "cover"}


def test_ac4_explicit_zero_long_is_flat():
    decision = _decide(
        candidate={"symbol": "AAA/USDT", "existing_side": "long", "existing_qty": "0"}
    )
    assert "long_not_flipped" not in decision.reasons
    assert decision.reasons == ("owner_freeze_pending",)


def test_ac4_reduce_only_cover_stays_permitted():
    denied = _decide(profile=None)
    assert denied.admitted is False
    assert cover_permitted_when_entry_denied(position_side="short", reduce_only=True) is True
    assert cover_permitted_when_entry_denied(position_side="short", reduce_only=False) is False
    assert cover_permitted_when_entry_denied(position_side="long", reduce_only=True) is False


def test_ac5_stock_token_is_not_excluded():
    stock = _decide(candidate={"symbol": "AAA/USDT", "product_kind": "stock_token"})
    perp = _decide(candidate={"symbol": "BBB/USDT", "product_kind": "perp"})
    assert stock.reasons == perp.reasons == ("owner_freeze_pending",)
    assert stock.stock_exclusion_applied is False
    assert perp.stock_exclusion_applied is False
    assert dict(stock.parameters)["product_kind"] == "stock_token"
    assert "stock_excluded" not in stock.reasons
    assert "product_ban" not in stock.reasons


def test_module_has_no_freeze_table_or_risk_manager():
    import strategies.short_admission as admission

    source = Path(admission.__file__).read_text(encoding="utf-8")
    assert "risk_manager" not in source
    assert "REGIME_MATRIX" not in source
    assert "Owner-Freeze" not in source
    assert "exclude_symbols" not in source
