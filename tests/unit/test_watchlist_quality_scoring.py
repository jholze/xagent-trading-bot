"""W2 WQE shadow scoring unit tests (#126)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.watchlist_quality.config import score_weights, wqe_mode, wqe_shadow_active
from services.watchlist_quality.memory_bias import MemoryWqeInput
from services.watchlist_quality.scoring import (
    score_coin,
    score_liquidity,
    score_momentum,
    score_narrative,
    score_regime_fit,
)
from services.watchlist_quality.engine import run_shadow_score, score_watchlist


def test_weights_normalize():
    w = score_weights({})
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert set(w) == {"liquidity", "momentum", "narrative", "memory", "regime_fit"}


def test_mode_defaults_off():
    assert wqe_mode({}) == "off"
    assert wqe_shadow_active({}) is False
    assert wqe_shadow_active({"watchlist_quality": {"mode": "shadow"}}) is True


def test_liquidity_increases_with_volume():
    low = score_liquidity(quote_vol_24h=10_000)
    high = score_liquidity(quote_vol_24h=5_000_000)
    assert high > low
    assert 0 <= low <= 1 and 0 <= high <= 1


def test_score_coin_with_prefer_memory():
    mem = MemoryWqeInput(
        symbol="H/USDT",
        entry_bias="prefer",
        size_bias=1.1,
        memory_score=0.65,
        hard_exclude_new_add=False,
        ttl_active=False,
        scope="",
        rationale="strong",
        source="profile",
    )
    sc = score_coin(
        "H/USDT",
        quote_vol_24h=2_000_000,
        change_24h_pct=5.0,
        cmc_rank=3,
        source="cmc_trending",
        memory=mem,
        regime_size_mult=1.0,
    )
    assert 0 <= sc.quality_score <= 1
    assert sc.scores["memory"] == pytest.approx(0.65)
    assert "memory_prefer" in sc.flags
    assert "vol_ok" in sc.flags
    assert sc.tier_hint in ("T1", "T2", "T3")


def test_score_coin_soft_block_flags():
    mem = MemoryWqeInput(
        symbol="BDX/USDT",
        entry_bias="soft_block",
        size_bias=0.5,
        memory_score=0.1,
        hard_exclude_new_add=True,
        ttl_active=True,
        scope="all_new",
        rationale="gross",
        source="profile",
    )
    sc = score_coin(
        "BDX/USDT",
        quote_vol_24h=1000,
        memory=mem,
    )
    assert "memory_soft_block" in sc.flags
    assert "memory_hard_exclude_new" in sc.flags
    assert sc.scores["memory"] < 0.5


def test_run_shadow_off_skips():
    out = run_shadow_score(
        [{"symbol": "A/USDT", "quote_vol_24h": 1e6}],
        config={"watchlist_quality": {"mode": "off"}},
        persist=False,
    )
    assert out.get("skipped") is True
    assert out["mode"] == "off"


def test_run_shadow_scores_and_no_behavior_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # isolate score file write
    monkeypatch.setenv("DEMO_MODE", "0")

    coins = [
        {
            "symbol": "A/USDT",
            "quote_vol_24h": 3_000_000,
            "change_24h": 4,
            "cmc_rank": 2,
            "source": "cmc_trending",
        },
        {
            "symbol": "B/USDT",
            "quote_vol_24h": 5_000,
            "change_24h": 80,
            "cmc_rank": 40,
            "source": "cmc_trending",
        },
    ]
    cfg = {"watchlist_quality": {"mode": "shadow"}}

    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input",
        side_effect=lambda sym, **kw: MemoryWqeInput(
            symbol=sym,
            entry_bias="neutral",
            size_bias=1.0,
            memory_score=0.5,
            hard_exclude_new_add=False,
            ttl_active=False,
            scope="",
            rationale="",
            source="default",
        ),
    ), patch(
        "services.watchlist_quality.engine._regime_hints",
        return_value=(1.0, "allow"),
    ):
        summary = run_shadow_score(coins, config=cfg, persist=True)

    assert summary["mode"] == "shadow"
    assert summary["scored"] == 2
    assert summary["behavior_change"] is False
    assert summary["score_p50"] is not None
    assert summary.get("persisted") is True


def test_score_watchlist_batch():
    with patch(
        "services.watchlist_quality.scoring.get_memory_wqe_input",
        side_effect=lambda sym, **kw: MemoryWqeInput(
            symbol=sym,
            entry_bias="neutral",
            size_bias=1.0,
            memory_score=0.5,
            hard_exclude_new_add=False,
            ttl_active=False,
            scope="",
            rationale="",
            source="default",
        ),
    ), patch(
        "services.watchlist_quality.engine._regime_hints",
        return_value=(None, None),
    ):
        out = score_watchlist(
            [{"symbol": "X/USDT", "quote_vol_24h": 1e6}],
            config={"watchlist_quality": {"mode": "shadow"}},
        )
    assert len(out) == 1
    assert out[0].symbol == "X/USDT"


def test_narrative_rank():
    top = score_narrative(cmc_rank=1, source="cmc_trending")
    low = score_narrative(cmc_rank=45, source="cmc_trending")
    assert top > low


def test_momentum_bands():
    assert 0 <= score_momentum(change_24h_pct=3) <= 1
    assert 0 <= score_momentum(change_24h_pct=100) <= 1


def test_regime_fit():
    assert score_regime_fit(size_mult=1.2) > score_regime_fit(size_mult=0.2)
    assert score_regime_fit(sensor_policy="block") < 0.5
