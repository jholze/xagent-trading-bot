"""W4 enforce, W5 universe, AI4 soak, AI5 sort — shipped modules."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.watchlist_quality.enforce import (
    apply_enforce_tiers,
    buy_allowed,
    filter_new_adds_memory,
    regime_caps,
)
from services.watchlist_quality.soft import apply_soft_watchlist
from services.watchlist_quality.soak import compute_ai_agreement_metrics, format_soak_report
from services.watchlist_quality.universe import rank_cmc_candidates_by_wqe, sensor_universe


def test_regime_caps_defaults():
    assert regime_caps({}, regime="neutral")["T2"] == 4
    assert regime_caps({}, regime="risk-on")["T1"] == 12
    assert regime_caps({}, regime="risk-off")["T3"] == 0


def test_apply_enforce_tiers_caps_and_pos():
    coins = [
        {"symbol": "POS/USDT", "quality_score": 0.1, "is_open": True},
        {"symbol": "A/USDT", "quality_score": 0.9, "tier_hint": "T1", "quote_vol_24h": 2e6},
        {"symbol": "B/USDT", "quality_score": 0.85, "tier_hint": "T1", "quote_vol_24h": 2e6},
        {"symbol": "C/USDT", "quality_score": 0.5, "tier_hint": "T2", "quote_vol_24h": 1e6},
        {"symbol": "D/USDT", "quality_score": 0.2, "tier_hint": "T3", "quote_vol_24h": 1e6},
    ]
    cfg = {
        "watchlist_quality": {
            "mode": "enforce",
            "regime_caps": {"neutral": {"T1": 1, "T2": 1, "T3": 0}},
            "drop_t3": True,
        }
    }
    out = apply_enforce_tiers(
        coins, open_symbols={"POS/USDT"}, config=cfg, regime="neutral"
    )
    syms = [c["symbol"] for c in out]
    assert syms[0] == "POS/USDT"
    assert "A/USDT" in syms  # top T1
    assert "B/USDT" not in syms  # capped
    assert "D/USDT" not in syms  # T3 dropped
    assert "C/USDT" in syms


def test_buy_allowed_enforce_min_score_and_memory():
    cfg = {"watchlist_quality": {"mode": "enforce", "min_buy_score": 0.5}}
    ok, reason = buy_allowed(
        "X/USDT",
        scored_row={"symbol": "X/USDT", "quality_score": 0.3, "tier_hint": "T2"},
        config=cfg,
    )
    assert ok is False
    assert "min_buy_score" in reason

    ok2, reason2 = buy_allowed(
        "Y/USDT",
        scored_row={
            "symbol": "Y/USDT",
            "quality_score": 0.7,
            "tier_hint": "T2",
            "hard_exclude_new_add": True,
            "source": "cmc_trending",
        },
        config=cfg,
        source="cmc_trending",
        is_new_add=True,
    )
    assert ok2 is False
    assert "memory" in reason2


def test_buy_allowed_off_mode():
    ok, reason = buy_allowed("Z/USDT", config={"watchlist_quality": {"mode": "off"}})
    assert ok is True
    assert reason == "wqe_off"


def test_filter_new_adds_memory():
    coins = [
        {"symbol": "BASE/USDT", "hard_exclude_new_add": True},
        {"symbol": "NEW/USDT", "hard_exclude_new_add": True, "source": "cmc_trending"},
        {"symbol": "OK/USDT", "hard_exclude_new_add": False},
    ]
    out = filter_new_adds_memory(
        coins, base_symbols={"BASE/USDT"}, open_symbols=set()
    )
    syms = [c["symbol"] for c in out]
    assert "BASE/USDT" in syms
    assert "NEW/USDT" not in syms
    assert "OK/USDT" in syms


def test_sensor_universe_filters_by_tier(monkeypatch):
    store = {
        "coins": [
            {"symbol": "T1/USDT", "tier_hint": "T1", "quality_score": 0.8},
            {"symbol": "T2/USDT", "tier_hint": "T2", "quality_score": 0.5},
            {"symbol": "T2L/USDT", "tier_hint": "T2", "quality_score": 0.2},
            {"symbol": "T3/USDT", "tier_hint": "T3", "quality_score": 0.1},
        ]
    }
    with patch(
        "services.watchlist_quality.universe.load_quality_scores", return_value=store
    ):
        cands = [
            {"symbol": "T1/USDT", "active": True},
            {"symbol": "T2/USDT", "active": True},
            {"symbol": "T2L/USDT", "active": True},
            {"symbol": "T3/USDT", "active": True},
        ]
        out = sensor_universe(
            cands,
            config={"watchlist_quality": {"mode": "enforce", "min_buy_score": 0.4}},
        )
    syms = [c["symbol"] for c in out]
    assert "T1/USDT" in syms
    assert "T2/USDT" in syms
    assert "T2L/USDT" not in syms
    assert "T3/USDT" not in syms


def test_rank_cmc_by_wqe():
    store = {
        "coins": [
            {"symbol": "A/USDT", "quality_shadow_ai": 0.3},
            {"symbol": "B/USDT", "quality_shadow_ai": 0.9},
        ]
    }
    with patch(
        "services.watchlist_quality.universe.load_quality_scores", return_value=store
    ):
        ranked = rank_cmc_candidates_by_wqe(
            ["A/USDT", "B/USDT"],
            config={"watchlist_quality": {"mode": "soft"}},
        )
    assert ranked == ["B/USDT", "A/USDT"]


def test_ai5_soft_sort_prefers_shadow_ai():
    coins = [
        {
            "symbol": "A/USDT",
            "quote_vol_24h": 2e6,
            "quality_score": 0.9,
            "quality_shadow_ai": 0.2,
        },
        {
            "symbol": "B/USDT",
            "quote_vol_24h": 2e6,
            "quality_score": 0.3,
            "quality_shadow_ai": 0.85,
        },
    ]
    out = apply_soft_watchlist(coins, open_symbols=set(), min_quote_vol_usd=100)
    assert [c["symbol"] for c in out] == ["B/USDT", "A/USDT"]


def test_soak_metrics_from_payload():
    payload = {
        "mode": "shadow",
        "updated_at": "2026-07-25T00:00:00Z",
        "coins": [
            {
                "symbol": "A/USDT",
                "quality_score": 0.6,
                "quality_shadow_ai": 0.5,
                "ai": {"source": "ok", "stance": "demote"},
            },
            {
                "symbol": "B/USDT",
                "quality_score": 0.4,
                "quality_shadow_ai": 0.55,
                "ai": {"source": "ok", "stance": "boost"},
            },
            {
                "symbol": "C/USDT",
                "quality_score": 0.5,
                "quality_shadow_ai": 0.5,
                "ai": {"source": "error"},
            },
        ],
    }
    m = compute_ai_agreement_metrics(payload)
    assert m["n"] == 3
    assert m["ai_ok"] == 2
    assert m["ai_error"] == 1
    assert m["demote_n"] == 1
    assert m["boost_n"] == 1
    assert m["mean_abs_delta"] is not None
    text = format_soak_report(m)
    assert "WQE soak" in text
    assert "demote=1" in text
