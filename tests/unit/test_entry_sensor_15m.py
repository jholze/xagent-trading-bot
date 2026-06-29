import copy
from unittest.mock import MagicMock, patch

import pytest

from core.actions import BUY, BUY_STRONG, HOLD
from core.config import BotConfig

from services.market_service import MarketService
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    clear_pending_for_tests,
    evaluate_entry_sensor_15m,
    set_pending_sensor_metrics,
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

HOLD_INDICATORS = {
    "rsi": 42.0,
    "lower_bb": 0.95,
    "middle_bb": 1.0,
    "upper_bb": 1.05,
    "vol_multiplier": 1.0,
    "atr": 0.03,
    "atr_pct": 3.0,
}

SPIKE_METRICS = {
    "volume_spike_ratio": 2.8,
    "body_atr_ratio": 0.55,
    "price_momentum": True,
}

VOLATILE_COIN = {
    "symbol": "XENTRY15/USDT",
    "timeframe": "4h",
    "active": True,
    "strategy_params": {
        "rsi_buy_low": 25,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.5,
        "stop_loss_pct": 50,
    },
}


def _active_sensor_config():
    import data_manager

    raw = copy.deepcopy(data_manager.get_config())
    raw["entry_sensor_15m"] = {**DEFAULT_CFG, "mode": "active"}
    return BotConfig(raw=raw)


def _shadow_sensor_config():
    import data_manager

    raw = copy.deepcopy(data_manager.get_config())
    raw["entry_sensor_15m"] = {**DEFAULT_CFG, "mode": "shadow"}
    return BotConfig(raw=raw)


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


class TestDecisionEngineSensorIntegration:
    def test_evaluate_lifts_hold_to_buy_with_pending_metrics(self):
        from strategies.decision_engine import DecisionEngine

        set_pending_sensor_metrics(VOLATILE_COIN["symbol"], SPIKE_METRICS)
        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=HOLD_INDICATORS["rsi"],
        )

        engine = DecisionEngine()
        with patch.object(engine.market, "fetch_indicators", return_value=HOLD_INDICATORS), patch.object(
            engine.market, "fetch_15m_sensor_metrics", return_value=None
        ), patch.object(engine.market, "fetch_funding_rate", return_value=None):
            analysis = engine.evaluate(VOLATILE_COIN, 1.0)

        assert analysis.action == BUY
        assert analysis.timeframe == "4h"
        assert ENTRY_SENSOR_SOURCE in (analysis.sources or [])
        assert "vol spike" in (analysis.rationale or "")

    def test_pending_metrics_revalidated_with_fresh_rsi(self):
        from strategies.decision_engine import DecisionEngine

        set_pending_sensor_metrics(VOLATILE_COIN["symbol"], SPIKE_METRICS)
        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=42.0,
        )

        engine = DecisionEngine()
        hot_rsi = {**HOLD_INDICATORS, "rsi": 80.0}
        with patch.object(engine.market, "fetch_indicators", return_value=hot_rsi), patch.object(
            engine.market, "fetch_15m_sensor_metrics", return_value=None
        ):
            analysis = engine.evaluate(VOLATILE_COIN, 1.0)

        assert analysis.action == HOLD
        assert ENTRY_SENSOR_SOURCE not in (analysis.sources or [])

    def test_evaluate_shadow_annotates_without_buy(self, monkeypatch):
        from strategies.decision_engine import DecisionEngine

        set_pending_sensor_metrics(
            VOLATILE_COIN["symbol"],
            {"volume_spike_ratio": 3.0, "body_atr_ratio": 0.6},
        )
        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=HOLD_INDICATORS["rsi"],
        )

        monkeypatch.setattr("strategies.decision_engine.get_bot_config", _shadow_sensor_config)
        engine = DecisionEngine()
        with patch.object(engine.market, "fetch_indicators", return_value=HOLD_INDICATORS), patch.object(
            engine.market, "fetch_15m_sensor_metrics", return_value=None
        ), patch.object(engine.market, "fetch_funding_rate", return_value=None):
            analysis = engine.evaluate(VOLATILE_COIN, 1.0)

        assert analysis.action == HOLD
        assert "entry_sensor_shadow" in (analysis.sources or [])

    def test_evaluate_shadow_keeps_existing_buy(self, monkeypatch):
        from strategies.decision_engine import DecisionEngine

        set_pending_sensor_metrics(
            VOLATILE_COIN["symbol"],
            {"volume_spike_ratio": 3.0, "body_atr_ratio": 0.6},
        )
        indicators = {**HOLD_INDICATORS, "rsi": 35.0, "lower_bb": 1.02, "vol_multiplier": 2.0}
        watch_15m_state.set_watch(VOLATILE_COIN["symbol"], VOLATILE_COIN["timeframe"], rsi_4h=35.0)
        monkeypatch.setattr("strategies.decision_engine.get_bot_config", _shadow_sensor_config)
        engine = DecisionEngine()
        with patch.object(engine.market, "fetch_indicators", return_value=indicators), patch.object(
            engine.market, "fetch_15m_sensor_metrics", return_value=None
        ):
            analysis = engine.evaluate(VOLATILE_COIN, 1.0)

        assert analysis.action == BUY
        assert "entry_sensor_shadow" in (analysis.sources or [])


class TestActiveOrchestratorPath:
    def test_process_coin_executes_via_real_risk_manager(self):
        from services.signal_orchestrator import SignalOrchestrator

        set_pending_sensor_metrics(VOLATILE_COIN["symbol"], SPIKE_METRICS)
        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=HOLD_INDICATORS["rsi"],
        )

        orch = SignalOrchestrator()
        risk_outcomes = []
        real_risk_eval = orch.trading.risk.evaluate

        def _capture_risk(*args, **kwargs):
            decision = real_risk_eval(*args, **kwargs)
            risk_outcomes.append(decision)
            return decision

        with patch.object(orch.decision_engine.market, "fetch_indicators", return_value=HOLD_INDICATORS), patch.object(
            orch.decision_engine.market, "fetch_15m_sensor_metrics", return_value=None
        ), patch.object(orch.decision_engine.market, "fetch_funding_rate", return_value=None), patch.object(
            orch.trading.risk.market, "fetch_indicators", return_value=HOLD_INDICATORS
        ), patch.object(orch.trading.risk.market, "fetch_funding_rate", return_value=None), patch.object(
            orch.trading.risk, "evaluate", side_effect=_capture_risk
        ), patch("notifications.telegram_commands.position_display.send_positions_snapshot"):
            result = orch.process_coin(VOLATILE_COIN, 1.0, quiet=True)

        assert risk_outcomes
        assert risk_outcomes[-1].approved is True
        assert result["action"] == BUY
        assert result["executed"] is True
        assert ENTRY_SENSOR_SOURCE in result["sources"]
        assert VOLATILE_COIN["timeframe"] == "4h"


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

    def test_poll_once_uses_single_ohlcv_and_rate_limit(self, monkeypatch):
        from services import entry_sensor_loop
        from tests.unit.test_market_service_15m import _sample_15m_df

        entry_sensor_loop.reset_poll_state_for_tests()
        fetch_calls = []

        class FakeMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                fetch_calls.append((symbol, timeframe, limit))
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return MarketService.compute_15m_sensor_metrics(df, **kwargs)

        orch = MagicMock()
        orch.market = FakeMarket()
        watch_15m_state.set_watch(
            "XENTRY15/USDT",
            "4h",
            rsi_4h=45,
            tech_buy=False,
        )
        monkeypatch.setattr(entry_sensor_loop, "get_prices_batch", lambda symbols: {"XENTRY15/USDT": 1.0})
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol: {"symbol": symbol, "timeframe": "4h", "active": True},
        )
        monkeypatch.setattr(
            "core.config.get_bot_config",
            lambda: BotConfig(
                raw={
                    "entry_sensor_15m": {
                        **DEFAULT_CFG,
                        "mode": "shadow",
                        "poll_interval_sec": 20,
                        "min_poll_gap_sec_per_coin": 20,
                    }
                }
            ),
        )

        entry_sensor_loop._poll_once(orch)
        entry_sensor_loop._poll_once(orch)

        assert len(fetch_calls) == 1
        assert fetch_calls[0][1] == "15m"

    def test_poll_once_active_hands_metrics_to_process_coin(self, monkeypatch):
        from services import entry_sensor_loop
        from tests.unit.test_market_service_15m import _sample_15m_df

        entry_sensor_loop.reset_poll_state_for_tests()
        metrics_calls = []
        process_calls = []

        class FakeMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return SPIKE_METRICS

        orch = MagicMock()
        orch.market = FakeMarket()
        orch.process_coin = lambda coin, price, **kw: process_calls.append((coin["symbol"], price)) or {
            "action": "BUY",
            "executed": True,
        }

        watch_15m_state.set_watch("XENTRY15/USDT", "4h", rsi_4h=45, tech_buy=False)
        monkeypatch.setattr(entry_sensor_loop, "get_prices_batch", lambda symbols: {"XENTRY15/USDT": 1.0})
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol: {"symbol": symbol, "timeframe": "4h", "active": True},
        )
        monkeypatch.setattr(
            "core.config.get_bot_config",
            lambda: BotConfig(
                raw={
                    "entry_sensor_15m": {
                        **DEFAULT_CFG,
                        "mode": "active",
                        "fakeout_min_body_atr_ratio": 0.01,
                        "poll_interval_sec": 20,
                        "min_poll_gap_sec_per_coin": 20,
                    }
                }
            ),
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "set_pending_sensor_metrics",
            lambda sym, m: metrics_calls.append(sym),
        )

        entry_sensor_loop._poll_once(orch)

        assert metrics_calls == ["XENTRY15/USDT"]
        assert process_calls and process_calls[0][0] == "XENTRY15/USDT"