"""Shared mode-proof contract. Identical on the admission and expiry PRs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from strategies.short_mode_proof import (
    CUTOFF_LOCAL,
    DEFAULT_MAX_AGE_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
    MAX_EXPOSURE_PCT,
    MAX_HOLD_HOURS,
    clock_skew_seconds,
    mode_proof_max_age_seconds,
    opened_regimes,
    parse_sandbox_mode_proof,
    tightened_cutoff,
    tightened_decimal,
)

_NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def _cfg(**paper_start):
    return {"shorts": {"testnet_futures": {"paper_start": paper_start}}}


def _proof(**overrides):
    body = {
        "tenant_id": "fixture-tenant",
        "market": "paper",
        "live": False,
        "readback_ok": True,
        "margin_mode": "isolated",
        "position_mode": "one_way",
        "leverage": "1",
        "as_of": int(_NOW.timestamp()),
    }
    body.update(overrides)
    return body


def test_config_cannot_raise_the_60s_ceiling():
    assert mode_proof_max_age_seconds({}) == (DEFAULT_MAX_AGE_SECONDS, None)
    assert mode_proof_max_age_seconds(_cfg(mode_proof_max_age_seconds=120)) == (60, None)
    assert mode_proof_max_age_seconds(_cfg(mode_proof_max_age_seconds=30)) == (30, None)
    stale = parse_sandbox_mode_proof(
        _proof(as_of=int(_NOW.timestamp()) - 61),
        now=_NOW,
        config_raw=_cfg(mode_proof_max_age_seconds=120),
    )
    assert stale.allowed is False
    assert stale.reason == "mode_proof_stale"
    fresh = parse_sandbox_mode_proof(
        _proof(as_of=int(_NOW.timestamp()) - 30),
        now=_NOW,
        config_raw=_cfg(mode_proof_max_age_seconds=30),
    )
    assert fresh.allowed is True
    tightened = parse_sandbox_mode_proof(
        _proof(as_of=int(_NOW.timestamp()) - 31),
        now=_NOW,
        config_raw=_cfg(mode_proof_max_age_seconds=30),
    )
    assert tightened.allowed is False


def test_config_cannot_raise_the_5s_skew_cap():
    assert clock_skew_seconds({}) == (MAX_CLOCK_SKEW_SECONDS, None)
    assert clock_skew_seconds(_cfg(clock_skew_seconds=30)) == (5, None)
    assert clock_skew_seconds(_cfg(clock_skew_seconds=2)) == (2, None)
    assert clock_skew_seconds(_cfg(clock_skew_seconds=-1))[0] is None
    assert clock_skew_seconds(_cfg(clock_skew_seconds=-1))[1] == "paper_start_invalid"


def test_future_dated_proof_is_denied():
    result = parse_sandbox_mode_proof(
        _proof(as_of=int((_NOW + timedelta(seconds=1)).timestamp())),
        now=_NOW,
    )
    assert result.allowed is False
    assert result.reason == "mode_proof_stale"


def test_paper_start_presence_and_non_finite_ceilings():
    from strategies.short_mode_proof import paper_start_present

    assert paper_start_present({}) is False
    assert paper_start_present({"shorts": {"testnet_futures": {}}}) is False
    assert paper_start_present({"shorts": {"testnet_futures": {"paper_start": {}}}}) is True
    assert parse_sandbox_mode_proof(_proof(as_of=True), now=_NOW).reason == "mode_proof_stale"


def test_isolated_one_way_is_required():
    cross = parse_sandbox_mode_proof(_proof(margin_mode="cross"), now=_NOW)
    hedge = parse_sandbox_mode_proof(_proof(position_mode="hedge"), now=_NOW)
    ok = parse_sandbox_mode_proof(_proof(), now=_NOW)
    assert cross.allowed is False and cross.reason == "mode_proof_missing"
    assert hedge.allowed is False and hedge.reason == "mode_proof_missing"
    assert ok.allowed is True
    assert ok.margin_mode == "isolated"
    assert ok.position_mode == "one_way"


def test_unknown_tenant_missing_timestamp_and_missing_clock_deny():
    unknown = parse_sandbox_mode_proof(_proof(tenant_id="unknown"), now=_NOW)
    blank = parse_sandbox_mode_proof(_proof(tenant_id="  "), now=_NOW)
    missing_stamp = dict(_proof())
    del missing_stamp["as_of"]
    no_stamp = parse_sandbox_mode_proof(missing_stamp, now=_NOW)
    no_clock = parse_sandbox_mode_proof(_proof(), now=None)
    assert unknown.allowed is False
    assert blank.allowed is False
    assert no_stamp.allowed is False
    assert no_stamp.reason == "mode_proof_stale"
    assert no_clock.allowed is False
    assert no_clock.reason == "mode_proof_stale"


def test_bad_config_shapes_do_not_widen_or_parse():
    from strategies.short_mode_proof import opened_regimes as regimes
    from strategies.short_mode_proof import tightened_decimal, tightened_int

    assert tightened_int(_cfg(mode_proof_max_age_seconds=True), "mode_proof_max_age_seconds", 60)[1] == "paper_start_invalid"
    assert tightened_decimal(_cfg(max_exposure_pct=True), "max_exposure_pct", MAX_EXPOSURE_PCT)[1] == "paper_start_invalid"
    zero, zero_reason = tightened_decimal(_cfg(max_exposure_pct="0"), "max_exposure_pct", MAX_EXPOSURE_PCT)
    assert zero == Decimal("0") and zero_reason is None
    bad, bad_reason = tightened_decimal(_cfg(max_exposure_pct="nope"), "max_exposure_pct", MAX_EXPOSURE_PCT)
    assert bad is None and bad_reason == "paper_start_invalid"
    for raw in ("NaN", "Infinity", "-1"):
        denied, reason = tightened_decimal(_cfg(max_exposure_pct=raw), "max_exposure_pct", MAX_EXPOSURE_PCT)
        assert denied is None and reason == "paper_start_invalid"
    assert tightened_cutoff(_cfg(cutoff_local="19")) == CUTOFF_LOCAL
    assert regimes(_cfg(opened_regimes=None)) == frozenset()
    assert regimes(_cfg(opened_regimes="")) == frozenset()
    assert regimes(_cfg(opened_regimes="RANGE,unknown")) == frozenset({"RANGE"})
    assert regimes(_cfg(opened_regimes=["RANGE", "unknown", ""])) == frozenset({"RANGE"})
    assert parse_sandbox_mode_proof(_proof(as_of=True), now=_NOW).allowed is False
    assert parse_sandbox_mode_proof(_proof(as_of=_NOW), now=_NOW).allowed is True
    assert parse_sandbox_mode_proof(
        _proof(as_of=str(int(_NOW.timestamp()))),
        now=_NOW,
    ).allowed is True
    assert parse_sandbox_mode_proof(_proof(live=True), now=_NOW).reason == "live_denied"
    no_live = _proof()
    del no_live["live"]
    missing_live = parse_sandbox_mode_proof(no_live, now=_NOW)
    assert missing_live.allowed is False
    assert missing_live.reason == "mode_proof_missing"
    missing_readback = _proof()
    del missing_readback["readback_ok"]
    assert parse_sandbox_mode_proof(missing_readback, now=_NOW).allowed is False
    assert parse_sandbox_mode_proof(_proof(as_of=1.5), now=_NOW).allowed is False


def test_numeric_config_cannot_loosen_frozen_ceilings():
    loose = _cfg(
        max_exposure_pct="25",
        max_hold_hours="5",
        cutoff_local="21:00:00",
        opened_regimes=["RANGE"],
    )
    assert tightened_decimal(loose, "max_exposure_pct", MAX_EXPOSURE_PCT) == (Decimal("10"), None)
    assert tightened_decimal(loose, "max_hold_hours", MAX_HOLD_HOURS) == (Decimal("4"), None)
    assert tightened_cutoff(loose) == CUTOFF_LOCAL
    tight = _cfg(max_exposure_pct="5", max_hold_hours="3", cutoff_local="19:00:00")
    assert tightened_decimal(tight, "max_exposure_pct", MAX_EXPOSURE_PCT) == (Decimal("5"), None)
    assert tightened_decimal(tight, "max_hold_hours", MAX_HOLD_HOURS) == (Decimal("3"), None)
    assert tightened_cutoff(tight).hour == 19
    assert tightened_cutoff(_cfg(cutoff_local="19:00:30")).second == 30
    assert parse_sandbox_mode_proof(_proof(as_of="-abc"), now=_NOW).allowed is False
    assert parse_sandbox_mode_proof(_proof(market=""), now=_NOW).reason == "mode_proof_missing"
    assert opened_regimes({}) == frozenset()
    assert opened_regimes(loose) == frozenset({"RANGE"})
