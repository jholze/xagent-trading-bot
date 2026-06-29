import json
import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.actions import BUY, BUY_STRONG, HOLD
from core.models import MarketContext, SignalAnalysis
from services.market_service import MarketService
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    clear_pending_for_tests,
    evaluate_entry_sensor_15m,
    set_pending_sensor_result,
)
from strategies import watch_15m_state


@pytest.fixture(autouse=True)
def reset_sensor_state(tmp_path, monkeypatch):
    clear_pending_for_tests()
    watch_15m_state.reset_cache_for_tests()
    path = str(tmp_path / "watch_15m_state.demo.json")
    monkeypatch.setattr(watch_15m_state, "_state_path", lambda: path)
    yield
    clear_pending_for_tests()
    watch_15m_state.reset_cache_for_tests()


DEFAULT_CFG = {
    "enabled": True,
    "mode": "active",
    "vol_spike_mult": 2.0,
    "block_buy_if_rsi_4h_above": 75,
    "fakeout_min_body_atr_ratio": 0.3,
    "cooldown_after_reject_hours": 2,
    "require_ema_breakout": False,
}


class TestEvaluateEntrySensor15m:
    def test_no_trigger_without_watch(self):
        metrics = {"volume_spike_ratio": 3.0, "body_atr_ratio": 0.5}
        r = evaluate_entry_sensor_15m(watched=False, metrics=metrics, cfg=DEFAULT_CFG, rsi_4h=50)
        assert not r.triggered

    def test_trigger_on_volume_spike(self):
        metrics = {
            "volume_spike_ratio": 2.5,
            "body_atr_ratio": 0.5,
            "price_momentum": True,
        }
        r = evaluate_entry_sensor_15m(watched=True, metrics=metrics, cfg=DEFAULT_CFG, rsi_4h=50)
        assert r.triggered
        assert r.action == BUY
        assert "vol spike" in r.rationale

    def test_buy_strong_when_tech_already_buy(self):
        metrics = {"volume_spike_ratio": 2.2, "body_atr_ratio": 0.4}
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=metrics,
            cfg=DEFAULT_CFG,
            rsi_4h=50,
            tech_already_buy=True,
        )
        assert r.triggered
        assert r.action == BUY_STRONG

    def test_blocked_by_rsi_cap(self):
        metrics = {"volume_spike_ratio": 3.0, "body_atr_ratio": 0.5}
        r = evaluate_entry_sensor_15m(watched=True, metrics=metrics, cfg=DEFAULT_CFG, rsi_4h=80)
        assert not r.triggered

    def test_never_returns_sell(self):
        metrics = {"volume_spike_ratio": 5.0, "body_atr_ratio": 1.0}
        r = evaluate_entry_sensor_15m(watched=True, metrics=metrics, cfg=DEFAULT_CFG, rsi_4h=40)
        assert r.action in (BUY, BUY_STRONG, HOLD)

    def test_shadow_mode_flag(self):
        cfg = {**DEFAULT_CFG, "mode": "shadow"}
        metrics = {"volume_spike_ratio": 2.5, "body_atr_ratio": 0.5}
        r = evaluate_entry_sensor_15m(watched=True, metrics=metrics, cfg=cfg, rsi_4h=50)
        assert r.triggered
        assert r.shadow_only


class TestWatch15mState:
    def test_set_and_is_watched(self):
        watch_15m_state.set_watch("VELVET/USDT", "4h", reason="setup_zone", ttl_hours=2)
        assert watch_15m_state.is_watched("VELVET/USDT")
        assert watch_15m_state.get_watch_entry("VELVET/USDT")["timeframe"] == "4h"

    def test_clear_watch(self):
        watch_15m_state.set_watch("RAVE/USDT", "4h")
        watch_15m_state.clear_watch("RAVE/USDT")
        assert not watch_15m_state.is_watched("RAVE/USDT")

    def test_persist_roundtrip(self, tmp_path, monkeypatch):
        path = str(tmp_path / "watch.demo.json")
        monkeypatch.setattr(watch_15m_state, "_state_path", lambda: path)
        watch_15m_state.reset_cache_for_tests()
        watch_15m_state.set_watch("XPL/USDT", "1h")
        watch_15m_state.reset_cache_for_tests()
        assert watch_15m_state.is_watched("XPL/USDT")


class TestDecisionEngineSensorMerge:
    def test_hold_lifted_to_buy_via_pending_sensor(self, monkeypatch):
        from strategies.decision_engine import DecisionEngine

        clear_pending_for_tests()
        set_pending_sensor_result(
            "VELVET/USDT",
            evaluate_entry_sensor_15m(
                watched=True,
                metrics={"volume_spike_ratio": 2.8, "body_atr_ratio": 0.5},
                cfg={**DEFAULT_CFG, "mode": "active"},
                rsi_4h=45,
            ),
        )

        engine = DecisionEngine(market_service=MagicMock())
        engine._entry_sensor_cfg = lambda: {**DEFAULT_CFG, "mode": "active"}

        market = MarketContext(
            symbol="VELVET/USDT",
            timeframe="4h",
            current_price=1.0,
            rsi=45,
            has_position=False,
            open_positions=0,
            strategy_params={"strategy_profile": "hermes_baseline+volatile", "rsi_buy_low": 25, "rsi_buy_high": 55},
        )
        technical = SignalAnalysis(
            action="HOLD",
            symbol="VELVET/USDT",
            timeframe="4h",
            rsi=45,
            lower_bb=0.9,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="HOLD",
            sources=["technical"],
        )

        norm, sources, conf, rationale, shadow = engine._apply_entry_sensor_buy(
            HOLD, ["technical"], 50.0, "VELVET/USDT", market, technical
        )
        assert norm == BUY
        assert ENTRY_SENSOR_SOURCE in sources
        assert shadow == ""
        assert "vol spike" in rationale

    def test_sensor_shadow_does_not_downgrade_existing_buy(self):
        from strategies.decision_engine import DecisionEngine

        engine = DecisionEngine(market_service=MagicMock())
        market = MarketContext(
            symbol="VELVET/USDT",
            timeframe="4h",
            current_price=1.0,
            rsi=45,
            has_position=False,
            open_positions=0,
            strategy_params={"strategy_profile": "hermes_baseline+volatile"},
        )
        technical = SignalAnalysis(
            action="HOLD",
            symbol="VELVET/USDT",
            timeframe="4h",
            rsi=45,
            lower_bb=0.9,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="HOLD",
        )
        set_pending_sensor_result(
            "VELVET/USDT",
            evaluate_entry_sensor_15m(
                watched=True,
                metrics={"volume_spike_ratio": 3.0, "body_atr_ratio": 0.6},
                cfg={**DEFAULT_CFG, "mode": "shadow"},
                rsi_4h=45,
            ),
        )
        norm, sources, _, _, shadow = engine._apply_entry_sensor_buy(
            BUY, ["lc"], 60.0, "VELVET/USDT", market, technical
        )
        assert norm == BUY
        assert shadow == BUY
        assert "entry_sensor_shadow" in sources

    def test_sensor_shadow_does_not_buy(self):
        from strategies.decision_engine import DecisionEngine

        engine = DecisionEngine(market_service=MagicMock())
        cfg = {**DEFAULT_CFG, "mode": "shadow"}
        market = MarketContext(
            symbol="VELVET/USDT",
            timeframe="4h",
            current_price=1.0,
            rsi=45,
            has_position=False,
            open_positions=0,
            strategy_params={},
        )
        technical = SignalAnalysis(
            action="HOLD",
            symbol="VELVET/USDT",
            timeframe="4h",
            rsi=45,
            lower_bb=0.9,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="HOLD",
        )
        set_pending_sensor_result(
            "VELVET/USDT",
            evaluate_entry_sensor_15m(
                watched=True,
                metrics={"volume_spike_ratio": 3.0, "body_atr_ratio": 0.6},
                cfg=cfg,
                rsi_4h=45,
            ),
        )
        norm, sources, _, _, shadow = engine._apply_entry_sensor_buy(
            HOLD, [], 40.0, "VELVET/USDT", market, technical
        )
        assert norm == HOLD
        assert shadow == BUY
        assert "entry_sensor_shadow" in sources


class TestEntrySensorLoop:
    def test_loop_module_import(self, monkeypatch):
        from services import entry_sensor_loop
        from services.entry_sensor_loop import start_entry_sensor_loop

        monkeypatch.setattr(entry_sensor_loop, "_loop_thread", None)
        orch = MagicMock()
        orch.market = MarketService()
        with patch.object(watch_15m_state, "list_watched", return_value=[]):
            with patch.object(entry_sensor_loop._stop_event, "wait"):
                thread = start_entry_sensor_loop(orch)
        assert thread is not None
        assert thread.name == "entry-sensor-15m"
        entry_sensor_loop.stop_entry_sensor_loop()