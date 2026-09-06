"""Slice 1 of #308: fold geometry, inconclusive verdict, health, observe, min_trades_per_fold."""

from __future__ import annotations

import copy
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from core.models import SandboxMetrics
from hermes.backtester import BACKTESTER_MIN_BARS, BacktestResult
from hermes.goals import GoalEngine, Verdict
from hermes.health import (
    check_fold_geometry,
    format_status_lines,
    reset_hermes_health_for_tests,
    update_inconclusive_health,
)
from hermes.memory import store
from hermes.validation import (
    WalkForwardResult,
    expected_fold_bars,
    inspect_fold_geometry,
    min_fold_days_for_timeframe,
    run_walk_forward,
    timeframe_bars_per_day,
)
from intelligence.grok_json import GrokError


@pytest.fixture(autouse=True)
def _reset_hermes_health():
    reset_hermes_health_for_tests()
    yield
    reset_hermes_health_for_tests()


def _synthetic_ohlcv(days: int = 21) -> pd.DataFrame:
    bars_per_day = 6
    n = days * bars_per_day
    start_ms = 1_700_000_000_000
    step_ms = 4 * 3600 * 1000
    ts = [start_ms + i * step_ms for i in range(n)]
    close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.5, n))
    return pd.DataFrame(
        {
            "ts": ts,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.random.default_rng(2).uniform(1000, 5000, n),
        }
    )


def _zero_wf(**kwargs) -> WalkForwardResult:
    folds = kwargs.pop("folds_total", 4)
    metrics = [
        {"fold_id": i, "sharpe": 0.0, "max_drawdown_pct": 0, "trades": 0} for i in range(folds)
    ]
    return WalkForwardResult(
        symbol="T",
        timeframe="4h",
        params={},
        fold_metrics=metrics,
        aggregate=SandboxMetrics(trades=0, sharpe=0.0),
        folds_total=folds,
        folds_won=0,
        **kwargs,
    )


class _ScriptedBacktester:
    def __init__(self, trades, sharpes):
        self._trades = list(trades)
        self._sharpes = list(sharpes)
        self.i = 0

    def run(self, symbol, timeframe, params, days=None, ohlcv_df=None):
        trades = self._trades[self.i]
        sharpe = self._sharpes[self.i]
        self.i += 1
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            params=params,
            metrics=SandboxMetrics(
                trades=trades,
                sharpe=sharpe,
                win_rate=55,
                max_drawdown_pct=5,
            ),
            bars_tested=len(ohlcv_df) if ohlcv_df is not None else 0,
        )


def test_backtester_min_bars_constant_and_4h_geometry():
    assert BACKTESTER_MIN_BARS == 30
    assert timeframe_bars_per_day("4h") == 6
    assert expected_fold_bars(3, "4h") == 18
    assert expected_fold_bars(5, "4h") == 30
    assert min_fold_days_for_timeframe("4h") == 5


def test_config_defaults_meet_backtester_min_bars():
    from core.config import BotConfig

    hermes = BotConfig().raw.get("hermes") or {}
    geo = inspect_fold_geometry(hermes)
    assert geo.ok, geo.message
    assert geo.fold_days >= 5
    assert geo.min_bars_per_fold >= BACKTESTER_MIN_BARS
    assert expected_fold_bars(geo.fold_days, "4h") >= BACKTESTER_MIN_BARS


def test_inspect_fold_geometry_rejects_old_3day_4h_window():
    geo = inspect_fold_geometry(
        {
            "timeframes": ["4h"],
            "validation": {"fold_days": 3, "min_bars_per_fold": 12},
        }
    )
    assert geo.ok is False
    assert "min_bars_per_fold=12" in geo.message
    assert "BACKTESTER_MIN_BARS=30" in geo.message
    assert "18 bars" in geo.message
    assert "fold_days >= 5" in geo.message


def _pin_config(monkeypatch, raw):
    cfg_holder = {}
    from core.config import BotConfig

    cfg_holder["cfg"] = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda *a, **k: cfg_holder["cfg"])
    monkeypatch.setattr("data_manager.get_config", lambda *a, **k: raw)
    monkeypatch.setattr("data_manager.reload_config", lambda *a, **k: raw)
    monkeypatch.setattr("core.config.reload_config", lambda *a, **k: raw)
    return cfg_holder["cfg"]


def test_bad_geometry_skips_cycle_and_notifies_once(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig
    from hermes.agent import HermesAgent

    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["validation"]["fold_days"] = 3
    raw["hermes"]["validation"]["min_bars_per_fold"] = 12
    raw["hermes"]["live_evidence"]["enabled"] = False
    cfg = _pin_config(monkeypatch, raw)

    with patch("core.operator_notify.notify_operator", return_value=True) as notify:
        agent = HermesAgent(cfg)
        r1 = agent.run_cycle()
        r2 = agent.run_cycle()

    assert r1.verdict == "invalid_geometry"
    assert r2.verdict == "invalid_geometry"
    assert r1.promoted is False
    assert notify.call_count == 1
    text = notify.call_args[0][0]
    assert "12" in text
    assert "30" in text
    assert "18" in text
    assert store.load_experiments().get("experiments") == []


def test_zero_trade_walk_forward_is_inconclusive():
    goals = GoalEngine()
    verdict = goals.evaluate_walk_forward(_zero_wf(), _zero_wf())
    assert verdict.inconclusive is True
    assert verdict.promoted is False
    assert verdict.label == "inconclusive"
    assert "0 trades" in verdict.reason
    assert verdict.label != "rejected"


def test_zero_trade_full_evaluate_is_inconclusive():
    goals = GoalEngine()
    verdict = goals.evaluate(
        {"sharpe": 0, "trades": 0, "max_drawdown_pct": 0, "win_rate": 0},
        {"sharpe": 0, "trades": 0, "max_drawdown_pct": 0, "win_rate": 0},
    )
    assert verdict.label == "inconclusive"
    assert verdict.promoted is False


def test_zero_trade_cycle_records_inconclusive(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig
    from hermes.agent import HermesAgent

    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["live_evidence"]["enabled"] = False
    raw["hermes"]["symbols_mode"] = "static"
    raw["hermes"]["symbols"] = ["ARIA/USDT"]
    cfg = _pin_config(monkeypatch, raw)
    store.init_baseline_from_config(cfg, "ARIA/USDT", "4h")

    agent = HermesAgent(cfg)
    wf = WalkForwardResult(
        symbol="ARIA/USDT",
        timeframe="4h",
        params={},
        fold_metrics=[{"fold_id": i, "sharpe": 0, "max_drawdown_pct": 0, "trades": 0} for i in range(4)],
        aggregate=SandboxMetrics(sharpe=0.0, trades=0),
        folds_total=4,
        folds_won=0,
    )
    df = pd.DataFrame({"ts": list(range(100)), "close": [1.0] * 100})
    monkeypatch.setattr(agent.backtester, "_fetch_ohlcv", lambda *a, **k: df)
    monkeypatch.setattr("hermes.agent.run_walk_forward", lambda *a, **k: wf)
    monkeypatch.setattr(agent.improver, "propose_experiment", lambda baseline: None)

    def _no_grok(*_a, **_k):
        raise GrokError("blocked")

    monkeypatch.setattr("hermes.self_improver.ask_grok_json", _no_grok)

    with patch("core.operator_notify.notify_operator", return_value=True):
        with patch.object(agent.improver, "analyze_and_suggest", return_value="ok"):
            result = agent.run_cycle()

    assert result.verdict == "inconclusive"
    last = store.load_experiments()["experiments"][-1]
    assert last["verdict"] == "inconclusive"
    counts = store.verdict_counts()
    assert counts.get("inconclusive", 0) >= 1
    # #308: 0-trade experiments must not refute the variable
    assert last["variable"] not in store.refuted_variables(last["symbol"], last["timeframe"])


def test_inconclusive_not_counted_as_refuted(hermes_memory_tmp):
    store.append_experiment(
        {
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "variable": "rsi_buy_low",
            "verdict": "inconclusive",
        }
    )
    store.append_experiment(
        {
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "variable": "rsi_sell_30",
            "verdict": "rejected",
        }
    )
    refuted = store.refuted_variables("ARIA/USDT", "4h")
    assert "rsi_buy_low" not in refuted
    assert "rsi_sell_30" in refuted
    counts = store.verdict_counts()
    assert counts["inconclusive"] == 1
    assert counts["rejected"] == 1


def test_inconclusive_health_share_and_alert_once_per_episode(hermes_memory_tmp):
    hermes_cfg = {"health": {"inconclusive_window": 20, "inconclusive_alert_pct": 50}}
    for i in range(11):
        store.append_experiment({"verdict": "inconclusive", "id": f"inc_{i}"})
    for i in range(9):
        store.append_experiment({"verdict": "rejected", "id": f"rej_{i}"})

    with patch("core.operator_notify.notify_operator", return_value=True) as notify:
        stats = update_inconclusive_health(hermes_cfg)
        assert stats["window_inconclusive"] == 11
        assert stats["window_n"] == 20
        assert stats["window_pct"] == 55
        assert stats["alert"] is True
        assert notify.call_count == 1
        update_inconclusive_health(hermes_cfg)
        assert notify.call_count == 1

        for i in range(20):
            store.append_experiment({"verdict": "promoted", "id": f"ok_{i}"})
        recovered = update_inconclusive_health(hermes_cfg)
        assert recovered["alert"] is False
        assert notify.call_count == 2


def test_status_shows_inconclusive_share(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig
    from hermes.agent import HermesAgent

    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["live_evidence"]["enabled"] = False
    raw["hermes"]["symbols_mode"] = "static"
    cfg = _pin_config(monkeypatch, raw)
    store.append_experiment({"verdict": "inconclusive", "variable": "x", "symbol": "ARIA/USDT"})
    store.append_experiment({"verdict": "rejected", "variable": "y", "symbol": "ARIA/USDT"})
    agent = HermesAgent(cfg)
    with patch("hermes.agent.format_active_pool_line", return_value="Pool (static): ARIA/USDT"):
        text = agent.status()
    assert "Geometry: OK" in text
    assert "Inconclusive:" in text
    assert "last cycle" in text.lower() or "Inconclusive: last cycle" in text


def test_observe_mode_skips_config_writer(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig
    from hermes.agent import HermesAgent

    monkeypatch.setenv("HERMES_LIVE_EVIDENCE_MODE", "observe")
    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["live_evidence"]["enabled"] = False
    raw["hermes"]["sync_to_config"] = True
    raw["hermes"]["symbols_mode"] = "static"
    raw["hermes"]["symbols"] = ["ARIA/USDT"]
    cfg = _pin_config(monkeypatch, raw)
    store.init_baseline_from_config(cfg, "ARIA/USDT", "4h")

    agent = HermesAgent(cfg)
    wf = WalkForwardResult(
        symbol="ARIA/USDT",
        timeframe="4h",
        params={},
        fold_metrics=[{"fold_id": i, "sharpe": 1.2, "max_drawdown_pct": 5, "trades": 4} for i in range(10)],
        aggregate=SandboxMetrics(sharpe=1.2, trades=40, win_rate=60, max_drawdown_pct=5, opportunity_score=0.5),
        folds_total=10,
        folds_won=8,
    )
    df = pd.DataFrame({"ts": list(range(100)), "close": [1.0] * 100})
    monkeypatch.setattr(agent.backtester, "_fetch_ohlcv", lambda *a, **k: df)
    monkeypatch.setattr("hermes.agent.run_walk_forward", lambda *a, **k: wf)
    monkeypatch.setattr(agent.improver, "propose_experiment", lambda baseline: None)
    monkeypatch.setattr(
        agent.goals,
        "evaluate_walk_forward",
        lambda *a, **k: Verdict(
            promoted=True,
            reason="test win",
            baseline_better=False,
            meets_success_criteria=True,
        ),
    )
    monkeypatch.setattr(agent.goals, "evaluate_with_live_and_counterfactual", lambda v, *a, **k: v)

    def _no_grok(*_a, **_k):
        raise GrokError("blocked")

    monkeypatch.setattr("hermes.self_improver.ask_grok_json", _no_grok)

    with patch("hermes.agent.log") as log_fn, patch(
        "strategies.registry.sync_hermes_baseline_to_config"
    ) as sync, patch("data_manager.save_config") as save:
        with patch.object(agent.improver, "analyze_and_suggest", return_value="ok"):
            result = agent.run_cycle()

    sync.assert_not_called()
    save.assert_not_called()
    assert result.promoted is True
    log_fn.assert_any_call("hermes observe: promotion suppressed", "INFO")
    last = store.load_experiments()["experiments"][-1]
    assert last["verdict"] == "promoted"


def test_min_trades_per_fold_excludes_thin_folds():
    # 3 non-overlapping 7d folds on 21d of 4h bars.
    df = _synthetic_ohlcv(22)
    hermes = {
        "validation": {
            "fold_days": 7,
            "step_days": 7,
            "min_bars_per_fold": 30,
            "min_trades_per_fold": 2,
        }
    }
    # baseline 3 folds then variant 3 folds
    bt = _ScriptedBacktester(
        trades=[0, 3, 3, 0, 4, 5],
        sharpes=[0.0, 0.5, 0.5, 0.0, 0.9, 0.9],
    )
    params = {"rsi_buy_low": 28}
    base = run_walk_forward(bt, "TEST/USDT", "4h", params, df, hermes)
    var = run_walk_forward(
        bt, "TEST/USDT", "4h", {"rsi_buy_low": 26}, df, hermes, baseline_folds=base.fold_metrics
    )
    assert base.folds_total == 3
    assert var.folds_total == 3
    assert var.folds_excluded == 1
    assert var.fold_metrics[0]["excluded"] is True
    assert var.folds_won == 2
    scored = var.folds_total - var.folds_excluded
    assert scored == 2
    assert var.folds_won / scored == 1.0
    verdict = GoalEngine().evaluate_walk_forward(base, var)
    assert verdict.inconclusive is False


def test_min_trades_per_fold_defaults_to_one_when_key_missing():
    df = _synthetic_ohlcv(22)
    hermes = {
        "validation": {
            "fold_days": 7,
            "step_days": 7,
            "min_bars_per_fold": 30,
        }
    }
    bt = _ScriptedBacktester(
        trades=[0, 1, 1, 0, 1, 2],
        sharpes=[0.0, 0.4, 0.4, 0.0, 0.8, 0.8],
    )
    base = run_walk_forward(bt, "TEST/USDT", "4h", {"rsi_buy_low": 28}, df, hermes)
    var = run_walk_forward(
        bt, "TEST/USDT", "4h", {"rsi_buy_low": 26}, df, hermes, baseline_folds=base.fold_metrics
    )
    assert var.folds_excluded == 1
    assert var.fold_metrics[0].get("exclude_reason", "").startswith("trades")


def test_explain_hermes_inconclusive():
    from notifications.user_explain import explain_hermes_cycle

    msg = explain_hermes_cycle(
        {
            "verdict": "inconclusive",
            "variable": "rsi_sell_30",
            "old_value": 70,
            "new_value": 68,
            "symbol": "H/USDT",
            "verdict_reason": "Inconclusive: baseline and variant both have 0 trades",
        }
    )
    assert "unentschieden" in msg.lower() or "0 Trades" in msg


def test_check_fold_geometry_sets_health_flag():
    ok = check_fold_geometry(
        {"timeframes": ["4h"], "validation": {"fold_days": 3, "min_bars_per_fold": 12}}
    )
    assert ok is False
    lines = format_status_lines(
        {"timeframes": ["4h"], "validation": {"fold_days": 3, "min_bars_per_fold": 12}}
    )
    assert any("INVALID" in line for line in lines)
