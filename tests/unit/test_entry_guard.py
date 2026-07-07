"""Unit tests for 15m entry-aware sell guard."""

from datetime import datetime, timedelta

import pytest

from strategies.entry_guard import (
    Pump15mState,
    classify_15m_pump_state,
    entry_guard_config,
    entry_sell_allowed,
    filter_sell_candidates,
    is_fresh_guarded_entry,
    is_guarded_entry,
)

WINNER_CFG = {
    "enabled": True,
    "sources": ["entry_sensor_15m"],
    "fresh_entry_window_minutes": 120,
    "vol_spike_mult": 2.0,
    "vol_exhaustion_15m_max": 0.85,
    "exhaustion_min_gain_pct": 5.0,
    "mega_pump_gain_pct": 12.0,
    "block_loss_sells_minutes": 15,
    "by_tier": {
        "meme": {"min_hold_minutes": 30, "min_gain_structure_pct": 6},
        "volatile": {"min_hold_minutes": 45, "min_gain_structure_pct": 8},
        "normal": {"min_hold_minutes": 60, "min_gain_structure_pct": 10},
        "large_cap": {"min_hold_minutes": 90, "min_gain_structure_pct": 12},
    },
}


def _fresh_position(*, minutes_ago: float = 3.0) -> dict:
    entry_at = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "entry_source": "entry_sensor_15m",
        "entry_at": entry_at,
        "first_buy_at": entry_at,
        "strategy_tier": "volatile",
    }


def _continuation_metrics() -> dict:
    return {"volume_spike_ratio": 2.8, "price_momentum": True, "body_atr_ratio": 0.5}


class TestEntryGuardClassification:
    def test_continuation_on_vol_spike_and_momentum(self):
        state = classify_15m_pump_state(_continuation_metrics(), gain_pct=2.0, cfg=WINNER_CFG)
        assert state == Pump15mState.CONTINUATION

    def test_exhaustion_on_gain_with_fading_vol(self):
        metrics = {"volume_spike_ratio": 0.6, "price_momentum": False}
        state = classify_15m_pump_state(metrics, gain_pct=8.0, cfg=WINNER_CFG)
        assert state == Pump15mState.EXHAUSTION

    def test_neutral_without_signals(self):
        assert classify_15m_pump_state(None, 3.0, cfg=WINNER_CFG) == Pump15mState.NEUTRAL


class TestEntryGuardEligibility:
    def test_guarded_only_for_configured_sources(self):
        pos = {"entry_source": "entry_sensor_15m"}
        assert is_guarded_entry(pos, WINNER_CFG)
        assert not is_guarded_entry({"entry_source": "auto"}, WINNER_CFG)

    def test_fresh_window_respects_age(self):
        pos = _fresh_position(minutes_ago=30)
        assert is_fresh_guarded_entry(pos, WINNER_CFG)
        old = _fresh_position(minutes_ago=150)
        assert not is_fresh_guarded_entry(old, WINNER_CFG)


class TestEntrySellAllowed:
    def test_blocks_bb_upper_during_continuation_whipsaw(self):
        """DOGE-style: 15m entry → bb_upper sell after ~3m with pump still active."""
        allowed, reason = entry_sell_allowed(
            position=_fresh_position(minutes_ago=2.3),
            strategy_params={"volatility_tier": "volatile"},
            sell_source="bb_upper",
            action="SELL_PARTIAL_30",
            gain_pct=1.5,
            ta_bearish=False,
            metrics_15m=_continuation_metrics(),
            cfg=WINNER_CFG,
        )
        assert not allowed
        assert "continuation" in reason

    def test_allows_mega_pump_exit(self):
        allowed, reason = entry_sell_allowed(
            position=_fresh_position(minutes_ago=5),
            strategy_params={"volatility_tier": "volatile"},
            sell_source="bb_upper",
            action="SELL_PARTIAL_30",
            gain_pct=14.0,
            ta_bearish=False,
            metrics_15m=_continuation_metrics(),
            cfg=WINNER_CFG,
        )
        assert allowed
        assert reason == ""

    def test_allows_exhaustion_structure_sell(self):
        metrics = {"volume_spike_ratio": 0.7, "price_momentum": False}
        allowed, _ = entry_sell_allowed(
            position=_fresh_position(minutes_ago=10),
            strategy_params={"volatility_tier": "volatile"},
            sell_source="bb_upper",
            action="SELL_PARTIAL_30",
            gain_pct=8.0,
            ta_bearish=False,
            metrics_15m=metrics,
            cfg=WINNER_CFG,
        )
        assert allowed

    def test_always_allows_stop_loss(self):
        allowed, _ = entry_sell_allowed(
            position=_fresh_position(minutes_ago=2),
            strategy_params={"volatility_tier": "volatile"},
            sell_source="x_stop_loss",
            action="SELL_STOP_FULL",
            gain_pct=-8.0,
            ta_bearish=True,
            metrics_15m=_continuation_metrics(),
            cfg=WINNER_CFG,
        )
        assert allowed

    def test_blocks_early_trailing_loss(self):
        allowed, reason = entry_sell_allowed(
            position=_fresh_position(minutes_ago=8),
            strategy_params={"volatility_tier": "volatile"},
            sell_source="trailing_take_profit",
            action="SELL_PARTIAL_10",
            gain_pct=-2.0,
            ta_bearish=False,
            metrics_15m=_continuation_metrics(),
            cfg=WINNER_CFG,
        )
        assert not allowed
        assert "loss sell blocked" in reason

    def test_non_guarded_entry_passes_through(self):
        pos = {"entry_source": "auto", "first_buy_at": datetime.now().isoformat()}
        allowed, _ = entry_sell_allowed(
            position=pos,
            strategy_params={},
            sell_source="bb_upper",
            action="SELL_PARTIAL_30",
            gain_pct=1.0,
            ta_bearish=False,
            metrics_15m=_continuation_metrics(),
            cfg=WINNER_CFG,
        )
        assert allowed


class TestFilterSellCandidates:
    def test_filters_structure_candidate_keeps_stop(self):
        candidates = [
            ("SELL_PARTIAL_30", 3, "bb_upper"),
            ("SELL_STOP_FULL", 5, "x_stop_loss"),
        ]
        kept, blocked = filter_sell_candidates(
            candidates,
            position=_fresh_position(minutes_ago=2.5),
            strategy_params={"volatility_tier": "volatile"},
            gain_pct=0.5,
            ta_bearish=False,
            metrics_15m=_continuation_metrics(),
            cfg=WINNER_CFG,
        )
        assert len(kept) == 1
        assert kept[0][2] == "x_stop_loss"
        assert any("bb_upper" in b for b in blocked)

    def test_config_defaults_load_from_bot_config(self):
        cfg = entry_guard_config()
        assert cfg.get("vol_spike_mult") == 2.0
        assert "volatile" in (cfg.get("by_tier") or {})