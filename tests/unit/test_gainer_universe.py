"""Unit tests for gainer universe (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.gainer_universe.filters import passes_spot_usdt_filter
from services.gainer_universe.inject import (
    expand_candidates_for_trade,
    merge_expand_into_trade,
    merge_gainers_into_observe,
)
from services.gainer_universe.scanner import (
    build_eligible,
    compute_streaks,
    filter_and_rank_live,
)
from services.exit_rotation import (
    apply_exit_section_overlay,
    exit_rotation_config,
)


def test_filter_drops_leverage_keeps_stock():
    assert passes_spot_usdt_filter("BTC/USDT")
    assert not passes_spot_usdt_filter("BTC3L/USDT")
    assert not passes_spot_usdt_filter("ETH3S/USDT")
    assert passes_spot_usdt_filter("NVDAX/USDT")  # stock token ok
    assert not passes_spot_usdt_filter("USDC/USDT")


def test_filter_blacklist_base():
    assert not passes_spot_usdt_filter(
        "SCAM/USDT", blacklist_bases=["SCAM"]
    )


def test_live_rank_mock_tickers():
    tickers = {
        "AAA/USDT": {"last": 1, "percentage": 50, "quoteVolume": 2_000_000},
        "BBB/USDT": {"last": 1, "percentage": 10, "quoteVolume": 2_000_000},
        "BTC3L/USDT": {"last": 1, "percentage": 99, "quoteVolume": 9_000_000},
        "LOW/USDT": {"last": 1, "percentage": 80, "quoteVolume": 100},
    }
    cfg = {
        "min_volume_usdt_24h": 500_000,
        "live_top_n": 10,
        "blacklist_suffixes": ["3L", "3S"],
    }
    ranked = filter_and_rank_live(tickers, cfg)
    syms = [r["symbol"] for r in ranked]
    assert syms[0] == "AAA/USDT"
    assert "BTC3L/USDT" not in syms
    assert "LOW/USDT" not in syms


def test_streaks_and_eligible_prev_day():
    today = datetime.now(timezone.utc).date()
    d0 = (today - timedelta(days=2)).isoformat()
    d1 = (today - timedelta(days=1)).isoformat()
    hist = {
        d0: [
            {"symbol": "HOT/USDT", "day_ret_pct": 20, "rank": 1},
            {"symbol": "X/USDT", "day_ret_pct": 15, "rank": 2},
        ],
        d1: [
            {"symbol": "HOT/USDT", "day_ret_pct": 12, "rank": 1},
            {"symbol": "Y/USDT", "day_ret_pct": 10, "rank": 2},
        ],
    }
    cfg = {
        "streak_min_days_in_top20": 2,
        "streak_lookback_days": 3,
        "enable_continuation": True,
        "continuation_max_chase_pct_today": 15,
        "expand_inject_max": 40,
        "prev_top_ttl_hours": 36,
    }
    streaks = compute_streaks(hist, cfg)
    assert any(s["symbol"] == "HOT/USDT" for s in streaks)

    live = [{"symbol": "HOT/USDT", "pct_24h": 5, "rank": 1}]
    elig = build_eligible(hist, live, streaks, cfg)
    # prev day tops include HOT and Y
    sources = {e["symbol"]: e["source"] for e in elig}
    assert sources.get("Y/USDT") == "gate_prev_top"
    assert "HOT/USDT" in sources


def test_ttl_expiry_skips_inject():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state = {
        "eligible": [
            {
                "symbol": "OLD/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": past,
            }
        ]
    }
    coins = expand_candidates_for_trade(state, {"expand_inject_max": 10, "enabled": True, "mode": "trade_expand"})
    assert coins == []


def test_shadow_no_trade_merge():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    state = {
        "eligible": [
            {
                "symbol": "HOT/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            }
        ]
    }
    root = {"gainer_universe": {"enabled": True, "mode": "shadow", "expand_inject_max": 40, "trade_max_with_expand": 80}}
    out = merge_expand_into_trade(trade, state, root_config=root)
    assert len(out) == 1
    assert out[0]["symbol"] == "BTC/USDT"


def test_trade_expand_injects():
    trade = [{"symbol": "BTC/USDT", "active": True}]
    fut = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    state = {
        "eligible": [
            {
                "symbol": "HOT/USDT",
                "source": "gate_prev_top",
                "rank": 1,
                "day_ret": 20,
                "eligible_until": fut,
            }
        ]
    }
    root = {
        "gainer_universe": {
            "enabled": True,
            "mode": "trade_expand",
            "expand_inject_max": 40,
            "trade_max_with_expand": 80,
        }
    }
    out = merge_expand_into_trade(trade, state, root_config=root)
    syms = {c["symbol"] for c in out}
    assert "BTC/USDT" in syms
    assert "HOT/USDT" in syms


def test_observe_merge_adds_live():
    observe = [{"symbol": "BTC/USDT", "active": True}]
    state = {
        "live_top": [{"symbol": "AAA/USDT", "pct_24h": 30, "rank": 1}],
        "eligible": [],
    }
    out = merge_gainers_into_observe(observe, state, {"enabled": True, "mode": "shadow"})
    syms = {c["symbol"] for c in out}
    assert "BTC/USDT" in syms
    assert "AAA/USDT" in syms


def test_rot_mid_overlay():
    root = {"exit_rotation": {"enabled": True, "profile": "rot_mid"}}
    ttp = apply_exit_section_overlay(
        {"arm_gain_pct": 15, "min_gain_pct": 10, "trail_pct": 6},
        "trailing_take_profit",
        root_config=root,
    )
    assert ttp["arm_gain_pct"] == 10
    assert ttp["min_gain_pct"] == 6
    assert ttp["trail_pct"] == 6  # untouched

    pml = apply_exit_section_overlay(
        {"max_hours": 96, "arm_gain_pct": 3},
        "profit_max_lifetime",
        root_config=root,
    )
    assert pml["max_hours"] == 48


def test_rot_base_identity():
    root = {"exit_rotation": {"enabled": False, "profile": "rot_mid"}}
    ttp = apply_exit_section_overlay(
        {"arm_gain_pct": 15},
        "trailing_take_profit",
        root_config=root,
    )
    assert ttp["arm_gain_pct"] == 15


def test_exit_rotation_config_invalid_profile():
    er = exit_rotation_config({"exit_rotation": {"enabled": True, "profile": "nope"}})
    assert er["profile"] == "base"
