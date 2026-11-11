"""T220-03 Rev 1: frozen paper/testnet start profile, default-off.

Owner freeze 2026-11-10 18:40 Europe/Berlin, items 5, 7 and 9. Item 6's
minute buffer is not set here. Missing profile still denies. Stock-token
deferral is short-path only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    "margin_mode": "isolated",
    "position_mode": "one_way",
    "leverage": "1",
    "as_of": int(_NOW.timestamp()),
}

_PROFILE = {
    "profile_id": "fixture-profile",
    "notional": "1000",
    "open_short_notional": "0",
    "stop_risk": "25",
    "existing_short_stop_risk": "50",
    "nav": "10000",
    "quote_quantum": "0.01",
    "metadata": {"schema": "fixture"},
    "reservations": {"pending": "0", "uncertain": "0", "reserved": "0"},
}


def _ready_candidate(**overrides):
    body = {
        "symbol": "AAA/USDT",
        "product_kind": "perp",
        "qty": "1",
        "exchange_min_qty": "1",
        "existing_side": "flat",
        "existing_qty": "0",
        "regime": "RANGE",
        "regime_as_of": _NOW.isoformat(),
    }
    body.update(overrides)
    return body


def _on(*, opened=(), regime_max_age_seconds=60, **shorts) -> dict:
    block = {
        "sandbox_admission_v220": {"enabled": True},
        "allow_live": False,
        "testnet_futures": {
            "paper_start": {
                "opened_regimes": list(opened),
                "regime_max_age_seconds": regime_max_age_seconds,
                "mode_proof_max_age_seconds": 60,
            }
        },
    }
    block.update(shorts)
    return {"shorts": block}


def _decide(profile=_PROFILE, *, candidate=None, **kwargs) -> AdmissionDecision:
    payload = {
        "config_raw": _on(opened=("RANGE",)),
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
    assert "sandbox_v220" not in shorts
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
    assert "above_min_qty" in _decide(
        _PROFILE, candidate=_ready_candidate(qty="2", exchange_min_qty="1")
    ).reasons
    missing_min = _ready_candidate()
    del missing_min["exchange_min_qty"]
    assert "min_qty_unknown" in _decide(_PROFILE, candidate=missing_min).reasons
    no_book = dict(_PROFILE)
    del no_book["existing_short_stop_risk"]
    assert "risk_profile_missing" in _decide(no_book, candidate=_ready_candidate()).reasons


def test_freeze_regime_cells_start_closed():
    decision = _decide(_PROFILE, candidate=_ready_candidate(), config_raw=_on())
    assert decision.admitted is False
    assert "regime_closed" in decision.reasons
    assert decision.regime_applied is True
    attempt = dict(_PROFILE)
    attempt["opened_regimes"] = "RANGE"
    forced = _decide(attempt, candidate=_ready_candidate(), config_raw=_on())
    assert forced.admitted is False
    assert "regime_opened_without_freeze" in forced.reasons
    unknown = _ready_candidate(regime="unknown")
    assert "regime_unknown" in _decide(_PROFILE, candidate=unknown).reasons
    stale = _ready_candidate(regime_as_of="2026-11-10T14:00:00+00:00")
    assert "regime_stale" in _decide(_PROFILE, candidate=stale).reasons
    exact = _ready_candidate()
    assert _decide(_PROFILE, candidate=exact).admitted is True
    aged = _ready_candidate(
        regime_as_of="2026-11-10T14:58:59+00:00",
        regime_max_age_seconds=99999,
    )
    assert "regime_stale" in _decide(_PROFILE, candidate=aged).reasons
    future = _ready_candidate(regime_as_of="2026-11-10T16:00:00+00:00")
    assert "regime_stale" in _decide(_PROFILE, candidate=future).reasons


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


def test_regime_age_boundary_margin_and_flat_qty():
    fresh = _ready_candidate(regime_as_of=(_NOW - timedelta(seconds=60)).isoformat())
    assert _decide(_PROFILE, candidate=fresh).admitted is True
    stale = _ready_candidate(regime_as_of=(_NOW - timedelta(seconds=61)).isoformat())
    assert "regime_stale" in _decide(_PROFILE, candidate=stale).reasons
    tight = _on(opened=("RANGE",), regime_max_age_seconds=30)
    aged = _ready_candidate(regime_as_of=(_NOW - timedelta(seconds=31)).isoformat())
    assert "regime_stale" in _decide(_PROFILE, candidate=aged, config_raw=tight).reasons
    still = _ready_candidate(regime_as_of=(_NOW - timedelta(seconds=30)).isoformat())
    assert _decide(_PROFILE, candidate=still, config_raw=tight).admitted is True
    mismatch = dict(_PROFILE)
    mismatch["margin"] = "1"
    assert "margin_notional_mismatch" in _decide(mismatch, candidate=_ready_candidate()).reasons
    assert "flat_qty_nonzero" in _decide(
        _PROFILE, candidate=_ready_candidate(existing_qty="5")
    ).reasons
    assert "existing_qty_invalid" in _decide(
        _PROFILE, candidate=_ready_candidate(existing_side="short", existing_qty="-3")
    ).reasons


def test_paper_start_without_opened_regimes_stays_closed():
    cfg = {
        "shorts": {
            "sandbox_admission_v220": {"enabled": True},
            "allow_live": False,
            "testnet_futures": {
                "enabled": False,
                "tenants": [],
                "paper_start": {"mode_proof_max_age_seconds": 60},
            },
        }
    }
    decision = _decide(_PROFILE, candidate=_ready_candidate(), config_raw=cfg)
    assert decision.admitted is False
    assert "regime_closed" in decision.reasons
    assert decision.regime_applied is True


def test_zero_quantum_and_negative_slots_deny():
    zero_q = dict(_PROFILE)
    zero_q["quote_quantum"] = "0"
    assert "quantum_unknown" in _decide(zero_q).reasons
    negative = dict(_PROFILE)
    negative["notional"] = "-1"
    assert "notional_unknown" in _decide(negative).reasons
    negative_stop = dict(_PROFILE)
    negative_stop["stop_risk"] = "-1"
    assert "stop_risk_unknown" in _decide(negative_stop).reasons
    negative_book = dict(_PROFILE)
    negative_book["existing_short_stop_risk"] = "-1"
    assert "stop_book_unknown" in _decide(negative_book).reasons


def test_negative_open_notional_and_unknown_side_deny():
    shrunk = dict(_PROFILE)
    shrunk["open_short_notional"] = "-100"
    shrunk["notional"] = "1100"
    denied = _decide(shrunk, candidate=_ready_candidate())
    assert denied.admitted is False
    assert "open_notional_unknown" in denied.reasons
    assert "existing_side_unknown" in _decide(
        _PROFILE, candidate=_ready_candidate(existing_side="foo")
    ).reasons


def test_total_exposure_includes_open_book_and_reservations():
    booked = dict(_PROFILE)
    booked["open_short_notional"] = "500"
    booked["notional"] = "100"
    booked["reservations"] = {"pending": "400", "uncertain": "0", "reserved": "0"}
    assert _decide(booked, candidate=_ready_candidate()).admitted is True
    over = dict(booked)
    over["reservations"] = {"pending": "400.01", "uncertain": "0", "reserved": "0"}
    assert "exposure_cap" in _decide(over, candidate=_ready_candidate()).reasons


def test_nav_now_side_and_margin_cap_mutants():
    zero = dict(_PROFILE)
    zero["nav"] = "0"
    assert "nav_unknown" in _decide(zero, candidate=_ready_candidate()).reasons
    negative = dict(_PROFILE)
    negative["nav"] = "-1"
    assert "nav_unknown" in _decide(negative, candidate=_ready_candidate()).reasons
    missing_clock = _decide(_PROFILE, candidate=_ready_candidate(), now=None)
    assert missing_clock.admitted is False
    assert "now_missing" in missing_clock.reasons
    raised = _on(opened=("RANGE",), max_margin_pct=25)
    assert "legacy_cap_raised" in _decide(
        _PROFILE, candidate=_ready_candidate(), config_raw=raised
    ).reasons
    bare_long = _ready_candidate(existing_side="long")
    del bare_long["existing_qty"]
    assert "long_not_flipped" in _decide(_PROFILE, candidate=bare_long).reasons
    assert "existing_qty_invalid" in _decide(
        _PROFILE, candidate=_ready_candidate(existing_qty="nope")
    ).reasons
    missing_side = _ready_candidate()
    del missing_side["existing_side"]
    assert "existing_side_unknown" in _decide(_PROFILE, candidate=missing_side).reasons
    stale_proof = dict(_PAPER_PROOF)
    stale_proof["as_of"] = int(_NOW.timestamp()) - 61
    assert "mode_proof_stale" in _decide(
        _PROFILE, candidate=_ready_candidate(), mode_proof=stale_proof
    ).reasons
    fresh_proof = dict(_PAPER_PROOF)
    fresh_proof["as_of"] = int(_NOW.timestamp()) - 60
    assert _decide(_PROFILE, candidate=_ready_candidate(), mode_proof=fresh_proof).admitted is True


def test_module_has_no_risk_manager_edit():
    import strategies.short_admission as admission

    source = Path(admission.__file__).read_text(encoding="utf-8")
    assert "risk_manager" not in source
    assert "exclude_symbols" not in source
