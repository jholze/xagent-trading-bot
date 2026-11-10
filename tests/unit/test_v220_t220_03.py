"""T220-03 Rev 1: frozen paper/testnet start profile, default-off.

Owner freeze 2026-11-10 18:40 Europe/Berlin, items 5, 7 and 9. Item 6's
minute buffer is not set here. Missing profile still denies. Stock-token
deferral is short-path only.
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
    sandbox_start_defaults,
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
    "notional": "1000",
    "stop_risk": "25",
    "existing_short_stop_risk": "50",
    "nav": "10000",
    "quote_quantum": "0.01",
    "metadata": {"schema": "fixture"},
    "reservations": {"pending": "0", "uncertain": "0", "reserved": "0"},
    "opened_regimes": "RANGE",
}


def _ready_candidate(**overrides):
    body = {
        "symbol": "AAA/USDT",
        "product_kind": "perp",
        "qty": "1",
        "exchange_min_qty": "1",
        "regime": "RANGE",
        "regime_as_of": _NOW.isoformat(),
        "regime_max_age_seconds": 60,
    }
    body.update(overrides)
    return body


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
    assert shorts["max_margin_pct"] == 20
    assert shorts["leverage_default"] == 2
    assert "sandbox_admission_v220" not in shorts
    assert sandbox_admission_enabled(raw) is False
    assert sandbox_start_defaults() == {
        "leverage": "1",
        "max_exposure_pct": "10",
        "max_stop_loss_pct": "0.25",
        "max_stop_loss_pct_all": "0.75",
        "legacy_margin_cap_pct": "20",
        "opened_regimes": "",
        "stock_phase": "defer_new_shorts",
        "min_qty_policy": "exchange_min",
    }


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
    for slot in ("stop_risk", "nav", "reservations", "profile_id", "metadata"):
        assert slot not in echoed
    assert echoed.get("max_exposure_pct") == "10"
    assert "0" not in echoed.values()


@pytest.mark.parametrize(
    "slot",
    ["profile_id", "notional", "stop_risk", "nav", "metadata", "reservations", "quote_quantum", "existing_short_stop_risk"],
)
def test_ac2_each_absent_slot_denies(slot):
    profile = dict(_PROFILE)
    profile.pop(slot)
    decision = _decide(profile, candidate=_ready_candidate())
    assert decision.admitted is False
    assert "risk_profile_missing" in decision.reasons
    assert slot not in dict(decision.parameters)


def test_ac2_omitted_leverage_uses_start_default():
    decision = _decide(_PROFILE, candidate=_ready_candidate())
    assert dict(decision.parameters)["leverage"] == "1"
    assert decision.admitted is True
    assert decision.reasons == ()
    assert decision.action == "sandbox_short_candidate"


def test_ac2_unknown_reservation_is_not_zero():
    profile = dict(_PROFILE)
    profile["reservations"] = {"pending": "1", "reserved": "1"}
    decision = _decide(profile, candidate=_ready_candidate())
    assert decision.admitted is False
    assert "reservation_unknown" in decision.reasons or "risk_profile_missing" in decision.reasons
    assert "reservations" not in dict(decision.parameters)


def test_ac2_float_slot_is_not_a_profile():
    profile = dict(_PROFILE)
    profile["nav"] = 10000.0
    decision = _decide(profile, candidate=_ready_candidate())
    assert decision.admitted is False
    assert "risk_profile_missing" in decision.reasons
    assert "nav" not in dict(decision.parameters)


def test_freeze_exact_limits_and_one_quantum():
    ok = _decide(_PROFILE, candidate=_ready_candidate())
    assert ok.admitted is True
    over_exposure = dict(_PROFILE)
    over_exposure["notional"] = "1000.01"
    over_exposure["margin"] = "1000.01"
    denied = _decide(over_exposure, candidate=_ready_candidate())
    assert denied.admitted is False
    assert "exposure_cap" in denied.reasons
    over_stop = dict(_PROFILE)
    over_stop["stop_risk"] = "25.01"
    assert "stop_loss_cap" in _decide(over_stop, candidate=_ready_candidate()).reasons
    over_book = dict(_PROFILE)
    over_book["existing_short_stop_risk"] = "50.01"
    assert "stop_loss_book_cap" in _decide(over_book, candidate=_ready_candidate()).reasons
    loose = dict(_PROFILE)
    loose["max_exposure_pct"] = "25"
    assert "cap_loosened" in _decide(loose, candidate=_ready_candidate()).reasons
    raised = dict(_PROFILE)
    raised["legacy_margin_cap_pct"] = "25"
    assert "cap_loosened" in _decide(raised, candidate=_ready_candidate()).reasons
    two_x = dict(_PROFILE)
    two_x["leverage"] = "2"
    assert "leverage_above_start" in _decide(two_x, candidate=_ready_candidate()).reasons


def test_freeze_min_qty_and_unknown_book():
    assert "below_min_qty" in _decide(
        _PROFILE, candidate=_ready_candidate(qty="0.5", exchange_min_qty="1")
    ).reasons
    missing_min = _ready_candidate()
    del missing_min["exchange_min_qty"]
    assert "min_qty_unknown" in _decide(_PROFILE, candidate=missing_min).reasons
    no_book = dict(_PROFILE)
    del no_book["existing_short_stop_risk"]
    assert "risk_profile_missing" in _decide(no_book, candidate=_ready_candidate()).reasons


def test_freeze_regime_cells_start_closed():
    closed = dict(_PROFILE)
    closed["opened_regimes"] = ""
    decision = _decide(closed, candidate=_ready_candidate())
    assert decision.admitted is False
    assert "regime_closed" in decision.reasons
    assert decision.regime_applied is True
    unknown = _ready_candidate(regime="unknown")
    assert "regime_unknown" in _decide(_PROFILE, candidate=unknown).reasons
    stale = _ready_candidate(regime_as_of="2026-11-10T14:00:00+00:00")
    assert "regime_stale" in _decide(_PROFILE, candidate=stale).reasons
    exact = _ready_candidate(regime_max_age_seconds=0)
    assert _decide(_PROFILE, candidate=exact).admitted is True
    aged = _ready_candidate(regime_as_of="2026-11-10T14:58:59+00:00", regime_max_age_seconds=60)
    assert "regime_stale" in _decide(_PROFILE, candidate=aged).reasons


def test_ac4_live_and_missing_proof_deny():
    live_cfg = {"shorts": {"sandbox_admission_v220": {"enabled": True}, "allow_live": True}}
    live = _decide(config_raw=live_cfg, candidate=_ready_candidate())
    assert live.admitted is False
    assert "live_denied" in live.reasons

    proof_live = dict(_PAPER_PROOF)
    proof_live["live"] = True
    marked = _decide(mode_proof=proof_live, candidate=_ready_candidate())
    assert "live_denied" in marked.reasons

    mainnet = dict(_PAPER_PROOF)
    mainnet["market"] = "mainnet"
    assert "live_denied" in _decide(mode_proof=mainnet, candidate=_ready_candidate()).reasons

    missing = _decide(mode_proof=None, candidate=_ready_candidate())
    assert missing.admitted is False
    assert "mode_proof_missing" in missing.reasons

    no_readback = dict(_PAPER_PROOF)
    del no_readback["readback_ok"]
    unread = _decide(mode_proof=no_readback, candidate=_ready_candidate())
    assert "mode_proof_missing" in unread.reasons

    empty_tenant = dict(_PAPER_PROOF)
    empty_tenant["tenant_id"] = ""
    assert "mode_proof_missing" in _decide(mode_proof=empty_tenant, candidate=_ready_candidate()).reasons


def test_ac4_long_is_not_flipped():
    decision = _decide(
        candidate=_ready_candidate(existing_side="long", existing_qty="1")
    )
    assert decision.admitted is False
    assert decision.action == "none"
    assert "long_not_flipped" in decision.reasons
    assert decision.action not in {"flip", "sell", "short", "cover"}


def test_ac4_explicit_zero_long_is_flat():
    decision = _decide(
        candidate=_ready_candidate(existing_side="long", existing_qty="0")
    )
    assert "long_not_flipped" not in decision.reasons
    assert decision.admitted is True


def test_ac4_reduce_only_cover_stays_permitted():
    denied = _decide(profile=None)
    assert denied.admitted is False
    assert cover_permitted_when_entry_denied(position_side="short", reduce_only=True) is True
    assert cover_permitted_when_entry_denied(position_side="short", reduce_only=False) is False
    assert cover_permitted_when_entry_denied(position_side="long", reduce_only=True) is False


def test_ac5_stock_deferral_is_not_a_global_ban():
    stock = _decide(_PROFILE, candidate=_ready_candidate(product_kind="stock_token"))
    unclear = _decide(_PROFILE, candidate=_ready_candidate(product_kind="unknown"))
    perp = _decide(_PROFILE, candidate=_ready_candidate(symbol="BBB/USDT", product_kind="perp"))
    assert stock.admitted is False
    assert "short_path_deferred" in stock.reasons
    assert stock.short_path_deferred is True
    assert stock.stock_exclusion_applied is False
    assert "product_ban" not in stock.reasons
    assert "short_path_deferred" in unclear.reasons
    assert perp.admitted is True
    assert perp.stock_exclusion_applied is False
    assert perp.short_path_deferred is False


def test_module_has_no_risk_manager_edit():
    import strategies.short_admission as admission

    source = Path(admission.__file__).read_text(encoding="utf-8")
    assert "risk_manager" not in source
    assert "exclude_symbols" not in source
