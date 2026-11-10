"""Frozen paper/testnet start values, 2026-11-10 18:40 Europe/Berlin.

Items 1, 2, 4, 10 and the item 8 permit default. Not a live or strategy approval.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from execution.paper_testnet_freeze import (
    FROZEN_HOST,
    LEGACY_HOST,
    adl_new_short,
    clearance_stop,
    emergency_close,
    failover_hosts,
    frozen_paper_start,
    futures_order_url_allowed,
    origin_allowed,
    reconcile_usdt,
)

ROOT = Path(__file__).resolve().parents[2]
ORDER = "https://api-testnet.gateapi.io:443/api/v4/futures/usdt/orders"


def _committed() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def test_committed_config_has_the_frozen_paper_start_values():
    raw = _committed()
    shorts = raw["shorts"]
    assert shorts["allow_live"] is False
    assert shorts["liquidation_buffer"] == 0.05
    block = shorts["testnet_futures"]
    assert block["enabled"] is False
    assert block["tenants"] == []
    start = block["paper_start"]
    assert start["_frozen_at"] == "2026-11-10T18:40+01:00"
    assert start["liq_clearance_fraction"] == "0.05"
    assert start["liq_clearance_min_ticks"] == 1
    assert start["host_allowlist"] == [FROZEN_HOST]
    assert start["legacy_host_fallback"] is False
    assert start["reconciliation_tolerance_usdt"] == "0.01"
    assert start["reconciliation_tolerance_may_widen"] is False
    assert start["adl_block_new_short"] == ["warning", "elevated", "unknown"]
    assert start["adl_keep_protection"] is True
    assert start["emergency_permit_default"] is False
    assert start["emergency_auto_close"] is False
    assert "limit_minutes" not in start
    assert "limit_usdt" not in start
    assert frozen_paper_start(raw)["host_allowlist"] == [FROZEN_HOST]


def test_item1_clearance_keeps_five_percent_and_one_tick_and_only_tightens():
    at_limit = clearance_stop(entry="100", liquidation="110", tick="0.1", proposed_stop="109.5")
    assert at_limit.allowed is True
    assert at_limit.value == Decimal("109.5")
    closer = clearance_stop(entry="100", liquidation="110", tick="0.1", proposed_stop="109.6")
    assert closer.allowed is True
    assert closer.reason == "tightened"
    assert closer.value == Decimal("109.5")
    tick_binds = clearance_stop(entry="100", liquidation="110", tick="1", proposed_stop="109.5")
    assert tick_binds.value == Decimal("109")
    tighter_fraction = clearance_stop(
        entry="100", liquidation="110", tick="0.1", proposed_stop="109.5", fraction="0.10"
    )
    assert tighter_fraction.value == Decimal("109")
    loosened = clearance_stop(entry="100", liquidation="110", tick="0.1", fraction="0.01")
    assert loosened.allowed is False
    assert loosened.reason == "clearance_loosened"
    assert loosened.value is None
    for kwargs in (
        {"entry": None, "liquidation": "110", "tick": "0.1"},
        {"entry": "100", "liquidation": None, "tick": "0.1"},
        {"entry": "100", "liquidation": "110", "tick": None},
        {"entry": "100", "liquidation": "110", "tick": "0"},
    ):
        missing = clearance_stop(**kwargs)
        assert missing.allowed is False
        assert missing.reason == "no_exchange_data"
        assert missing.keep_protection is True


def test_item2_host_allowlist_is_exactly_the_testnet_origin_without_fx_fallback():
    assert origin_allowed(ORDER).allowed is True
    assert futures_order_url_allowed(ORDER).allowed is True
    implicit = futures_order_url_allowed(
        "https://api-testnet.gateapi.io/api/v4/futures/usdt/orders"
    )
    assert implicit.allowed is True
    legacy = f"https://{LEGACY_HOST}:443/api/v4/futures/usdt/orders"
    assert origin_allowed(legacy).reason == "legacy_host_rejected"
    assert futures_order_url_allowed("https://api.gateio.ws/api/v4/futures/usdt/orders").reason == "origin_rejected"
    assert origin_allowed("https://api-testnet.gateapi.io:8443/api/v4/futures/usdt/orders").reason == "origin_rejected"
    assert origin_allowed("http://api-testnet.gateapi.io:443/api/v4/futures/usdt/orders").reason == "origin_rejected"
    assert origin_allowed(
        "https://user:secret@api-testnet.gateapi.io:443/api/v4/futures/usdt/orders"
    ).reason == "origin_rejected"
    assert "secret" not in repr(origin_allowed(
        "https://user:secret@api-testnet.gateapi.io:443/api/v4/futures/usdt/orders"
    ))
    widened = origin_allowed(ORDER, [FROZEN_HOST, f"https://{LEGACY_HOST}:443"])
    assert widened.reason == "allowlist_not_frozen"
    assert failover_hosts(FROZEN_HOST) == ()
    assert failover_hosts(LEGACY_HOST) == ()


def test_item4_tolerance_is_one_cent_and_does_not_widen():
    exact = reconcile_usdt("10", "10.01", tolerance="0.01", quantum="0.000001")
    assert exact.allowed is True
    assert exact.value == Decimal("0.01")
    step_over = reconcile_usdt("10", "10.010001", tolerance="0.01", quantum="0.000001")
    assert step_over.allowed is False
    assert step_over.reason == "above_tolerance"
    widened = reconcile_usdt("10", "10.02", tolerance="0.02")
    assert widened.reason == "tolerance_widened"
    assert widened.allowed is False
    unknown = reconcile_usdt("10", None)
    assert unknown.reason == "unknown_amount"
    assert unknown.value is None
    tighter = reconcile_usdt("10", "10.01", tolerance="0.005")
    assert tighter.allowed is False
    assert tighter.reason == "above_tolerance"


def test_item10_adl_warning_elevated_or_unknown_blocks_a_new_short_and_keeps_protection():
    for state in ("warning", "elevated", "unknown", None, "", "stale", "WARNING"):
        decision = adl_new_short(state)
        assert decision.allowed is False
        assert decision.keep_protection is True
        assert decision.auto_close is False
    assert adl_new_short("warning").reason == "adl_warning"
    assert adl_new_short("elevated").reason == "adl_elevated"
    assert adl_new_short(None).reason == "adl_unknown"


def test_item8_emergency_permit_defaults_off_and_does_not_auto_close():
    raw = _committed()
    assert emergency_close(raw).reason == "emergency_off"
    assert emergency_close(raw).auto_close is False
    assert emergency_close(raw, permit={"allow": True, "limit_minutes": 5, "limit_usdt": "1"}).reason == (
        "permit_not_an_auto_close"
    )
    forced = json.loads(json.dumps(raw))
    forced["shorts"]["testnet_futures"]["paper_start"]["emergency_auto_close"] = True
    assert emergency_close(forced).reason == "auto_close_forbidden"
    assert emergency_close(forced).auto_close is False
