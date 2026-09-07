import copy
from unittest.mock import MagicMock, patch

import pytest

from core.actions import BUY, BUY_STRONG, HOLD, SELL_PARTIAL_30
from decimal import Decimal
from core.config import BotConfig

from services.market_service import MarketService
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    clear_pending_for_tests,
    evaluate_entry_sensor_15m,
    passes_vol_spike_prefilter,
    set_pending_sensor_metrics,
)
from strategies import watch_15m_state


@pytest.fixture(autouse=True)
def reset_sensor_state(tmp_path, monkeypatch):
    from strategies.positions import clear_positions_memory
    from services.venue_quality import VenueQualityResult

    clear_pending_for_tests()
    clear_positions_memory()
    watch_15m_state.reset_cache_for_tests()
    path = str(tmp_path / "watch_15m_state.demo.json")
    monkeypatch.setattr(watch_15m_state, "_state_path", lambda: path)
    monkeypatch.setattr("strategies.decision_engine.get_bot_config", _active_sensor_config)
    monkeypatch.setattr(
        "data.cmc_market_cap.resolve_market_cap_usd",
        lambda symbol, coin=None: 10_000_000,
    )
    monkeypatch.setattr(
        "price_fetcher.is_gate_tradeable",
        lambda symbol, gate_price=None: True if gate_price is None else float(gate_price or 0) > 0,
    )
    # Synthetic symbols are not on Gate — venue must not kill classic sensor unit tests
    monkeypatch.setattr(
        "services.venue_quality.check_venue_for_buy",
        lambda *a, **k: VenueQualityResult(ok=True, reasons=["test_fixture"]),
    )
    yield
    clear_pending_for_tests()
    clear_positions_memory()
    watch_15m_state.reset_cache_for_tests()


DEFAULT_CFG = {
    "enabled": True,
    "mode": "active",
    "vol_spike_mult": 2.0,
    "block_buy_if_rsi_4h_above": 75,
    "fakeout_min_body_atr_ratio": 0.3,
    "cooldown_after_reject_hours": 2,
    "require_ema_breakout": False,
    # Classic lift tests: no MOMENTUM block unless product config is under test
    "hold_override_by_mode": {
        "GRID": "slice_only",
        "HYBRID": "allow_with_conditions",
        "MOMENTUM": "legacy",
        "DEFENSIVE": "off",
    },
    "ignore_aggression_boost": True,
    "max_usdt_absolute": 1000,
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


def _active_sensor_config(*_args, **_kwargs):
    """Match get_bot_config(tenant_id=...) signature used by execute/risk paths."""
    import data_manager

    raw = copy.deepcopy(data_manager.get_config())
    raw["entry_sensor_15m"] = {**DEFAULT_CFG, "mode": "active"}
    # Classic sensor lift tests: no regime mode forcing MOMENTUM hold_override=block
    raw["regime_detector"] = {**(raw.get("regime_detector") or {}), "enabled": False}
    raw["strategy_allocator"] = {**(raw.get("strategy_allocator") or {}), "enabled": False}
    raw["max_usdt_per_trade"] = 2500
    risk = dict(raw.get("risk") or {})
    risk["min_trade_usdt"] = 50
    risk["venue_quality"] = {**(risk.get("venue_quality") or {}), "enabled": False}
    raw["risk"] = risk
    return BotConfig(raw=raw)


def _shadow_sensor_config(*_args, **_kwargs):
    import data_manager

    raw = copy.deepcopy(data_manager.get_config())
    raw["entry_sensor_15m"] = {**DEFAULT_CFG, "mode": "shadow"}
    raw["regime_detector"] = {**(raw.get("regime_detector") or {}), "enabled": False}
    raw["strategy_allocator"] = {**(raw.get("strategy_allocator") or {}), "enabled": False}
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

    def test_blocked_when_not_on_gate(self):
        metrics = {"volume_spike_ratio": 3.0, "body_atr_ratio": 0.5}
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=metrics,
            cfg={**DEFAULT_CFG, "gate_only": True},
            rsi_4h=50,
            gate_tradeable=False,
        )
        assert not r.triggered
        assert "Gate.io" in r.rationale

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

    def test_market_cap_min_blocks_micro_cap(self):
        cfg = {**DEFAULT_CFG, "market_cap_min_usd": 5_000_000}
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=SPIKE_METRICS,
            cfg=cfg,
            rsi_4h=40,
            market_cap_usd=500_000,
        )
        assert not r.triggered
        assert "min" in r.rationale.lower()

    def test_market_cap_min_allows_large_cap(self):
        cfg = {**DEFAULT_CFG, "market_cap_min_usd": 5_000_000}
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=SPIKE_METRICS,
            cfg=cfg,
            rsi_4h=40,
            market_cap_usd=8_000_000_000,
        )
        assert r.triggered

    def test_vol_prefilter_matches_spike_gate(self):
        assert passes_vol_spike_prefilter(SPIKE_METRICS, DEFAULT_CFG)
        assert not passes_vol_spike_prefilter({"volume_spike_ratio": 1.0, "body_atr_ratio": 0.5}, DEFAULT_CFG)


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

    def test_seed_from_watchlist_skips_non_gate_coins(self, monkeypatch):
        watch_15m_state.reset_cache_for_tests()
        cfg = {
            **DEFAULT_CFG,
            "setup_modes": ["watchlist"],
            "max_watched_coins": 10,
            "gate_only": True,
        }
        wl = [
            {"symbol": "GATE/USDT", "active": True, "timeframe": "4h"},
            {"symbol": "NOGATE/USDT", "active": True, "timeframe": "4h"},
        ]
        monkeypatch.setattr("data_manager.load_effective_watchlist", lambda: wl)
        monkeypatch.setattr("strategies.positions.list_active_positions", lambda: [])
        with patch(
            "intelligence.strategy_backtest.classify_coin",
            lambda sym, _: "mid_cap",
        ), patch(
            "price_fetcher.get_gate_prices_batch",
            lambda symbols: {"GATE/USDT": 1.0, "NOGATE/USDT": 0.0},
        ):
            added = watch_15m_state.seed_from_watchlist(cfg)
        assert added == 1
        assert watch_15m_state.is_watched("GATE/USDT")
        assert not watch_15m_state.is_watched("NOGATE/USDT")

    def test_seed_from_watchlist_skips_held_and_large_cap(self, monkeypatch):
        watch_15m_state.reset_cache_for_tests()
        cfg = {
            **DEFAULT_CFG,
            "setup_modes": ["watchlist"],
            "max_watched_coins": 10,
        }
        wl = [
            {"symbol": "BEAT/USDT", "active": True, "timeframe": "4h"},
            {"symbol": "BTC/USDT", "active": True, "timeframe": "4h"},
            {"symbol": "H/USDT", "active": True, "timeframe": "4h"},
        ]
        monkeypatch.setattr(
            "data_manager.load_effective_watchlist",
            lambda: wl,
        )
        monkeypatch.setattr(
            "strategies.positions.list_active_positions",
            lambda: [{"symbol": "BEAT/USDT"}],
        )
        with patch(
            "intelligence.strategy_backtest.classify_coin",
            lambda sym, _: "large_cap" if sym == "BTC/USDT" else "mid_cap",
        ), patch(
            "price_fetcher.get_gate_prices_batch",
            lambda symbols: {sym: 1.0 for sym in symbols},
        ):
            added = watch_15m_state.seed_from_watchlist(cfg)
        assert added == 1
        assert not watch_15m_state.is_watched("BEAT/USDT")
        assert not watch_15m_state.is_watched("BTC/USDT")
        assert watch_15m_state.is_watched("H/USDT")


class TestDecisionEngineSensorIntegration:
    def test_evaluate_lifts_hold_to_buy_with_pending_metrics(self):
        from strategies.decision_engine import DecisionEngine
        from tests.unit.test_market_service_15m import _sample_15m_df

        set_pending_sensor_metrics(VOLATILE_COIN["symbol"], SPIKE_METRICS)
        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=HOLD_INDICATORS["rsi"],
        )

        engine = DecisionEngine()
        with patch.object(engine.market, "fetch_indicators", return_value=HOLD_INDICATORS), patch.object(
            engine.market, "fetch_ohlcv", return_value=_sample_15m_df(30)
        ), patch.object(
            engine.market,
            "fetch_ohlcv_and_indicators",
            return_value=(_sample_15m_df(30), HOLD_INDICATORS),
        ), patch.object(
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
            engine.market,
            "fetch_ohlcv_and_indicators",
            return_value=(None, hot_rsi),
        ), patch.object(
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
            engine.market,
            "fetch_ohlcv_and_indicators",
            return_value=(None, HOLD_INDICATORS),
        ), patch.object(
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
            engine.market,
            "fetch_ohlcv_and_indicators",
            return_value=(None, indicators),
        ), patch.object(
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
        # Align risk sizing with sensor-entry-guard caps (demo cash can shrink size)
        cfg = _active_sensor_config()
        cfg.raw.setdefault("risk", {})["min_trade_usdt"] = 1
        cfg.raw["max_usdt_per_trade"] = 2500
        orch.trading.config = cfg
        orch.trading.risk.config = cfg
        orch.decision_engine.config = cfg
        # execute_order calls refresh() which reloads disk config — freeze test cfg
        with patch.object(orch.trading, "refresh", return_value=orch.trading), patch.object(
            orch.decision_engine.market, "fetch_indicators", return_value=HOLD_INDICATORS
        ), patch.object(
            orch.decision_engine.market,
            "fetch_ohlcv_and_indicators",
            return_value=(None, HOLD_INDICATORS),
        ), patch.object(
            orch.decision_engine.market, "fetch_15m_sensor_metrics", return_value=None
        ), patch.object(orch.decision_engine.market, "fetch_funding_rate", return_value=None), patch.object(
            orch.trading.risk.market, "fetch_indicators", return_value=HOLD_INDICATORS
        ), patch.object(orch.trading.risk.market, "fetch_funding_rate", return_value=None), patch(
            "notifications.telegram_commands.position_display.send_positions_snapshot"
        ), patch("risk.risk_manager.is_demo_mode", return_value=False), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"active": False, "block_buys": False, "apply_size_mult": False},
        ), patch(
            "services.venue_quality.check_venue_for_buy",
            return_value=__import__(
                "services.venue_quality", fromlist=["VenueQualityResult"]
            ).VenueQualityResult(ok=True, reasons=[]),
        ), patch.object(
            orch.trading.risk, "_spendable_usdt", return_value=50_000.0
        ), patch.object(
            orch.trading.risk, "_portfolio_equity", return_value=100_000.0
        ), patch.object(
            orch.trading.risk, "_available_usdt", return_value=50_000.0
        ):
            result = orch.process_coin(VOLATILE_COIN, 1.0, quiet=True)

        assert result["action"] == BUY
        assert result["executed"] is True
        assert ENTRY_SENSOR_SOURCE in result["sources"]
        assert VOLATILE_COIN["timeframe"] == "4h"


class TestEntrySensorLoop:
    def _reset_loop_state(self):
        from services import entry_sensor_loop
        from strategies.positions import clear_positions_memory

        entry_sensor_loop.reset_poll_state_for_tests()
        clear_positions_memory()

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

        self._reset_loop_state()
        fetch_calls = []

        class FakeMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                fetch_calls.append((symbol, timeframe, limit))
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return MarketService.compute_15m_sensor_metrics(df, **kwargs)

            def fetch_indicators(self, symbol, timeframe, price):
                return HOLD_INDICATORS

        orch = MagicMock()
        orch.market = FakeMarket()
        watch_15m_state.set_watch(
            "XENTRY15/USDT",
            "4h",
            rsi_4h=45,
            tech_buy=False,
        )
        monkeypatch.setattr(entry_sensor_loop, "get_gate_prices_batch", lambda symbols: {"XENTRY15/USDT": 1.0})
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol, entry=None: {"symbol": symbol, "timeframe": "4h", "active": True},
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

    def test_poll_once_active_drives_real_risk_path(self, monkeypatch):
        from services import entry_sensor_loop
        from services.signal_orchestrator import SignalOrchestrator
        from tests.unit.test_market_service_15m import _sample_15m_df

        self._reset_loop_state()
        monkeypatch.setattr("strategies.decision_engine.get_bot_config", _active_sensor_config)
        monkeypatch.setattr("core.config.get_bot_config", _active_sensor_config)

        class LoopMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return SPIKE_METRICS

            def fetch_indicators(self, symbol, timeframe, price):
                return HOLD_INDICATORS

            def fetch_ohlcv_and_indicators(self, symbol, timeframe, price, limit=100):
                return None, HOLD_INDICATORS

            def fetch_funding_rate(self, symbol):
                return None

            def fetch_15m_sensor_metrics(self, symbol, cfg):
                return None

        orch = SignalOrchestrator()
        orch.market = LoopMarket()
        orch.decision_engine.market = LoopMarket()

        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=42.0,
            tech_buy=False,
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "get_gate_prices_batch",
            lambda symbols: {VOLATILE_COIN["symbol"]: 1.0},
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol, entry=None: dict(VOLATILE_COIN),
        )

        cfg = _active_sensor_config()
        cfg.raw.setdefault("risk", {})["min_trade_usdt"] = 1
        cfg.raw["max_usdt_per_trade"] = 2500
        orch.trading.config = cfg
        orch.trading.risk.config = cfg
        orch.decision_engine.config = cfg

        with patch.object(orch.trading, "refresh", return_value=orch.trading), patch.object(
            orch.trading.risk.market, "fetch_indicators", return_value=HOLD_INDICATORS
        ), patch.object(
            orch.trading.risk.market, "fetch_funding_rate", return_value=None
        ), patch("bus.eval_queue.eval_queue_enabled", return_value=False), patch(
            "notifications.telegram_commands.position_display.send_positions_snapshot"
        ), patch("risk.risk_manager.is_demo_mode", return_value=False), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={
                "active": False,
                "block_buys": False,
                "apply_size_mult": False,
                "apply_sensor_policy": False,
                "sensor_policy": "active",
            },
        ), patch(
            "services.venue_quality.check_venue_for_buy",
            return_value=__import__(
                "services.venue_quality", fromlist=["VenueQualityResult"]
            ).VenueQualityResult(ok=True, reasons=[]),
        ), patch.object(
            orch.trading.risk, "_spendable_usdt", return_value=50_000.0
        ), patch.object(
            orch.trading.risk, "_portfolio_equity", return_value=100_000.0
        ), patch.object(
            orch.trading.risk, "_available_usdt", return_value=50_000.0
        ):
            entry_sensor_loop._poll_once(orch)

        assert not watch_15m_state.is_watched(VOLATILE_COIN["symbol"])

    def test_poll_once_ignores_stale_watch_rsi_when_live_rsi_hot(self, monkeypatch):
        from services import entry_sensor_loop
        from services.signal_orchestrator import SignalOrchestrator
        from tests.unit.test_market_service_15m import _sample_15m_df

        self._reset_loop_state()
        monkeypatch.setattr("strategies.decision_engine.get_bot_config", _active_sensor_config)
        monkeypatch.setattr("core.config.get_bot_config", _active_sensor_config)

        hot_rsi = {**HOLD_INDICATORS, "rsi": 80.0}

        class LoopMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return SPIKE_METRICS

            def fetch_indicators(self, symbol, timeframe, price):
                return hot_rsi

            def fetch_funding_rate(self, symbol):
                return None

            def fetch_15m_sensor_metrics(self, symbol, cfg):
                return None

        orch = SignalOrchestrator()
        orch.market = LoopMarket()
        orch.decision_engine.market = LoopMarket()

        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=42.0,
            tech_buy=False,
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "get_gate_prices_batch",
            lambda symbols: {VOLATILE_COIN["symbol"]: 1.0},
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol, entry=None: dict(VOLATILE_COIN),
        )

        risk_outcomes = []
        real_risk_eval = orch.trading.risk.evaluate

        def _capture_risk(*args, **kwargs):
            decision = real_risk_eval(*args, **kwargs)
            risk_outcomes.append(decision)
            return decision

        with patch.object(orch.trading.risk, "evaluate", side_effect=_capture_risk), patch(
            "notifications.telegram_commands.position_display.send_positions_snapshot"
        ):
            entry_sensor_loop._poll_once(orch)

        assert not risk_outcomes

    def test_poll_once_skips_symbol_with_open_position(self, monkeypatch):
        from services import entry_sensor_loop
        from tests.unit.test_market_service_15m import _sample_15m_df

        self._reset_loop_state()
        fetch_calls = []

        class FakeMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                fetch_calls.append(symbol)
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return SPIKE_METRICS

        orch = MagicMock()
        orch.market = FakeMarket()
        watch_15m_state.set_watch(VOLATILE_COIN["symbol"], VOLATILE_COIN["timeframe"])
        monkeypatch.setattr(
            entry_sensor_loop,
            "get_gate_prices_batch",
            lambda symbols: {VOLATILE_COIN["symbol"]: 1.0},
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol, entry=None: dict(VOLATILE_COIN),
        )
        monkeypatch.setattr(
            "core.config.get_bot_config",
            lambda: BotConfig(raw={"entry_sensor_15m": {**DEFAULT_CFG, "mode": "active"}}),
        )
        from strategies.positions import init_position, positions, get_key

        init_position(VOLATILE_COIN["symbol"], VOLATILE_COIN["timeframe"])
        key = get_key(VOLATILE_COIN["symbol"], VOLATILE_COIN["timeframe"])
        positions[key]["amount"] = Decimal("100")
        positions[key]["average_entry"] = 1.0
        positions[key]["peak_amount"] = 100.0

        entry_sensor_loop._poll_once(orch)

        assert not fetch_calls
        assert not watch_15m_state.is_watched(VOLATILE_COIN["symbol"])
        orch.process_entry_sensor.assert_not_called()


class TestProcessEntrySensorPath:
    def test_entry_sensor_never_executes_sell(self, monkeypatch):
        from services.signal_orchestrator import SignalOrchestrator
        from strategies.decision_engine import DecisionEngine
        from core.models import SignalAnalysis

        orch = SignalOrchestrator()
        sell_analysis = SignalAnalysis(
            symbol=VOLATILE_COIN["symbol"],
            timeframe=VOLATILE_COIN["timeframe"],
            action=SELL_PARTIAL_30,
            normalized_action=SELL_PARTIAL_30,
            confidence=80,
            rsi=70,
            lower_bb=0.9,
            upper_bb=1.1,
            vol_multiplier=2.0,
            ampel_emoji="🔴",
            ampel_text="sell",
            rationale="bb upper",
            sources=["bb_upper"],
        )

        with patch.object(orch, "analyze", return_value=sell_analysis), patch.object(
            orch, "execute_if_needed"
        ) as mock_exec:
            result = orch.process_entry_sensor(VOLATILE_COIN, 1.0, sensor_metrics=SPIKE_METRICS)

        mock_exec.assert_not_called()
        assert result["action"] == "HOLD"
        assert result["executed"] is False

    def test_active_loop_clears_watch_after_buy(self, monkeypatch):
        from services import entry_sensor_loop
        from services.signal_orchestrator import SignalOrchestrator
        from tests.unit.test_market_service_15m import _sample_15m_df
        from strategies.positions import clear_positions_memory

        entry_sensor_loop.reset_poll_state_for_tests()
        clear_positions_memory()
        monkeypatch.setattr("strategies.decision_engine.get_bot_config", _active_sensor_config)
        monkeypatch.setattr("core.config.get_bot_config", _active_sensor_config)

        class LoopMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                return _sample_15m_df(30, spike_last=True)

            def compute_15m_sensor_metrics(self, df, **kwargs):
                return SPIKE_METRICS

            def fetch_indicators(self, symbol, timeframe, price):
                return HOLD_INDICATORS

            def fetch_ohlcv_and_indicators(self, symbol, timeframe, price, limit=100):
                return None, HOLD_INDICATORS

            def fetch_funding_rate(self, symbol):
                return None

            def fetch_15m_sensor_metrics(self, symbol, cfg):
                return None

        orch = SignalOrchestrator()
        orch.market = LoopMarket()
        orch.decision_engine.market = LoopMarket()

        watch_15m_state.set_watch(
            VOLATILE_COIN["symbol"],
            VOLATILE_COIN["timeframe"],
            rsi_4h=42.0,
            tech_buy=False,
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "get_gate_prices_batch",
            lambda symbols: {VOLATILE_COIN["symbol"]: 1.0},
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol, entry=None: dict(VOLATILE_COIN),
        )

        cfg = _active_sensor_config()
        cfg.raw.setdefault("risk", {})["min_trade_usdt"] = 1
        cfg.raw["max_usdt_per_trade"] = 2500
        orch.trading.config = cfg
        orch.trading.risk.config = cfg
        orch.decision_engine.config = cfg

        with patch.object(orch.trading, "refresh", return_value=orch.trading), patch.object(
            orch.trading.risk.market, "fetch_indicators", return_value=HOLD_INDICATORS
        ), patch.object(
            orch.trading.risk.market, "fetch_funding_rate", return_value=None
        ), patch("bus.eval_queue.eval_queue_enabled", return_value=False), patch(
            "notifications.telegram_commands.position_display.send_positions_snapshot"
        ), patch("risk.risk_manager.is_demo_mode", return_value=False), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={
                "active": False,
                "block_buys": False,
                "apply_size_mult": False,
                "apply_sensor_policy": False,
                "sensor_policy": "active",
            },
        ), patch(
            "services.venue_quality.check_venue_for_buy",
            return_value=__import__(
                "services.venue_quality", fromlist=["VenueQualityResult"]
            ).VenueQualityResult(ok=True, reasons=[]),
        ), patch.object(
            orch.trading.risk, "_spendable_usdt", return_value=50_000.0
        ), patch.object(
            orch.trading.risk, "_portfolio_equity", return_value=100_000.0
        ), patch.object(
            orch.trading.risk, "_available_usdt", return_value=50_000.0
        ):
            entry_sensor_loop._poll_once(orch)

        assert not watch_15m_state.is_watched(VOLATILE_COIN["symbol"])