"""Slice 2 of #308: hold-out, significance, veto window, snapshot/rollback, post-apply."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from core.models import SandboxMetrics
from hermes.backtester import BacktestResult
from hermes.goals import GoalEngine, Verdict
from hermes.significance import (
    block_bootstrap_win_probability,
    format_win_probability,
    tightened_threshold,
)
from hermes.validation import WalkForwardResult, run_walk_forward


T0 = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _folds(n, *, sharpe, dd=5.0, trades=4, start_id=0):
    return [
        {
            "fold_id": start_id + i,
            "sharpe": sharpe,
            "max_drawdown_pct": dd,
            "trades": trades,
            "win_rate": 55,
            "excluded": False,
        }
        for i in range(n)
    ]


def _wf(
    *,
    sharpe,
    trades,
    folds_won,
    folds_total,
    fold_metrics=None,
    holdout_metrics=None,
    folds_holdout=0,
    dd=5.0,
    win_rate=55.0,
    opportunity_score=0.5,
):
    metrics = fold_metrics or _folds(folds_total, sharpe=sharpe, dd=dd, trades=max(1, trades // max(folds_total, 1)))
    return WalkForwardResult(
        symbol="ARIA/USDT",
        timeframe="4h",
        params={"rsi_buy_low": 28},
        fold_metrics=metrics,
        holdout_metrics=list(holdout_metrics or []),
        aggregate=SandboxMetrics(
            sharpe=sharpe,
            max_drawdown_pct=dd,
            win_rate=win_rate,
            trades=trades,
            opportunity_score=opportunity_score,
            trade_quality=0.8,
        ),
        folds_total=folds_total,
        folds_won=folds_won,
        folds_holdout=folds_holdout,
    )


def _goals_with(raw_patch: dict | None = None) -> GoalEngine:
    from core.config import BotConfig

    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["live_evidence"]["enabled"] = False
    raw["hermes"].setdefault("validation", {})
    raw["hermes"]["validation"]["holdout_folds"] = 2
    raw["hermes"]["validation"]["holdout_dd_tolerance_pct"] = 2.0
    raw["hermes"]["validation"]["min_win_probability"] = 0.95
    raw["hermes"]["validation"]["min_total_trades"] = 30
    raw["hermes"].setdefault("promotion", {})
    raw["hermes"]["promotion"]["max_promotions_per_day"] = 1
    raw["hermes"]["promotion"]["veto_window_min"] = 10
    if raw_patch:
        for k, v in raw_patch.items():
            raw["hermes"][k] = v
    return GoalEngine(BotConfig(raw))


class _ScriptedBacktester:
    def __init__(self, trades, sharpes, dds=None):
        self._trades = list(trades)
        self._sharpes = list(sharpes)
        self._dds = list(dds) if dds is not None else [5.0] * len(trades)
        self.i = 0

    def run(self, symbol, timeframe, params, days=None, ohlcv_df=None):
        trades = self._trades[self.i]
        sharpe = self._sharpes[self.i]
        dd = self._dds[self.i]
        self.i += 1
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            params=params,
            metrics=SandboxMetrics(
                trades=trades,
                sharpe=sharpe,
                win_rate=55,
                max_drawdown_pct=dd,
            ),
            bars_tested=len(ohlcv_df) if ohlcv_df is not None else 0,
        )


def _ohlcv_days(days: int) -> pd.DataFrame:
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


# ---------------------------------------------------------------------------
# Significance (pure)
# ---------------------------------------------------------------------------


def test_block_bootstrap_all_positive_deltas_is_certain():
    p = block_bootstrap_win_probability([0.2, 0.1, 0.15, 0.05, 0.3, 0.12], seed=308)
    assert p == 1.0


def test_block_bootstrap_all_negative_deltas_is_zero():
    p = block_bootstrap_win_probability([-0.2, -0.1, -0.15, -0.05, -0.3, -0.12], seed=308)
    assert p == 0.0


def test_block_bootstrap_is_deterministic_with_seed():
    deltas = [0.05, -0.04, 0.03, -0.05, 0.02, -0.03, 0.01, -0.02]
    a = block_bootstrap_win_probability(deltas, seed=308)
    b = block_bootstrap_win_probability(deltas, seed=308)
    c = block_bootstrap_win_probability(deltas, seed=1)
    assert a == b
    assert 0.0 < a < 1.0
    assert isinstance(c, float)


def test_tightened_threshold_bonferroni_style():
    assert tightened_threshold(0.95, 1) == pytest.approx(0.95)
    assert tightened_threshold(0.95, 2) == pytest.approx(0.975)
    assert tightened_threshold(0.95, 5) == pytest.approx(0.99)


def test_format_win_probability_is_operator_facing_german():
    text = format_win_probability(0.97, 41)
    assert text == "Gewinnwahrscheinlichkeit 0,97 bei 41 Trades"
    assert "p-value" not in text.lower()
    assert "p=" not in text.lower()


# ---------------------------------------------------------------------------
# Hold-out
# ---------------------------------------------------------------------------


def test_run_walk_forward_splits_last_k_folds_as_holdout():
    df = _ohlcv_days(22)
    hermes = {
        "validation": {
            "fold_days": 7,
            "step_days": 7,
            "min_bars_per_fold": 30,
            "holdout_folds": 2,
        }
    }
    # 3 non-overlapping 7d folds; last 2 are hold-out, first is in-sample.
    bt = _ScriptedBacktester(
        trades=[3, 4, 5],
        sharpes=[0.4, 0.5, 0.6],
    )
    result = run_walk_forward(bt, "TEST/USDT", "4h", {"rsi_buy_low": 28}, df, hermes)
    assert result.folds_total == 1
    assert result.folds_holdout == 2
    assert len(result.fold_metrics) == 1
    assert len(result.holdout_metrics) == 2
    assert result.fold_metrics[0]["fold_id"] == 0
    assert [f["fold_id"] for f in result.holdout_metrics] == [1, 2]
    assert result.aggregate.sharpe == pytest.approx(0.4)


def test_holdout_pass_allows_promotion_when_significance_clears():
    goals = _goals_with()
    in_sample = _folds(8, sharpe=1.1, trades=5)
    holdout_var = _folds(2, sharpe=0.9, dd=6.0, trades=5, start_id=8)
    holdout_base = _folds(2, sharpe=0.7, dd=6.0, trades=5, start_id=8)
    base = _wf(
        sharpe=0.7,
        trades=40,
        folds_won=0,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=0.7, trades=5),
        holdout_metrics=holdout_base,
        folds_holdout=2,
    )
    var = _wf(
        sharpe=1.1,
        trades=40,
        folds_won=8,
        folds_total=8,
        fold_metrics=in_sample,
        holdout_metrics=holdout_var,
        folds_holdout=2,
        win_rate=60,
    )
    verdict = goals.evaluate_walk_forward(base, var, n_variables_today=1)
    assert verdict.promoted is True
    assert verdict.win_probability is not None
    assert verdict.win_probability >= 0.95
    assert verdict.total_trades >= 30
    assert verdict.threshold_used == pytest.approx(0.95)


def test_holdout_sharpe_regression_rejects_with_holdout_reason():
    goals = _goals_with()
    base = _wf(
        sharpe=0.7,
        trades=40,
        folds_won=0,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=0.7, trades=5),
        holdout_metrics=_folds(2, sharpe=0.9, trades=5, start_id=8),
        folds_holdout=2,
    )
    var = _wf(
        sharpe=1.1,
        trades=40,
        folds_won=8,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=1.1, trades=5),
        holdout_metrics=_folds(2, sharpe=0.5, trades=5, start_id=8),
        folds_holdout=2,
        win_rate=60,
    )
    verdict = goals.evaluate_walk_forward(base, var)
    assert verdict.promoted is False
    assert verdict.reason.startswith("holdout")


def test_holdout_drawdown_beyond_tolerance_rejects():
    goals = _goals_with()
    base = _wf(
        sharpe=0.7,
        trades=40,
        folds_won=0,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=0.7, dd=5.0, trades=5),
        holdout_metrics=_folds(2, sharpe=0.7, dd=5.0, trades=5, start_id=8),
        folds_holdout=2,
    )
    var = _wf(
        sharpe=1.1,
        trades=40,
        folds_won=8,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=1.1, dd=5.0, trades=5),
        holdout_metrics=_folds(2, sharpe=0.9, dd=8.0, trades=5, start_id=8),
        folds_holdout=2,
        win_rate=60,
        dd=5.0,
    )
    verdict = goals.evaluate_walk_forward(base, var)
    assert verdict.promoted is False
    assert verdict.reason.startswith("holdout")


def test_significance_rejects_low_win_probability():
    goals = _goals_with()
    # In-sample deltas straddle zero → bootstrap P(mean>0) well below 0.95.
    base_folds = _folds(8, sharpe=1.0, trades=5)
    var_folds = []
    signs = [0.05, -0.04, 0.03, -0.05, 0.02, -0.03, 0.01, -0.02]
    for i, d in enumerate(signs):
        var_folds.append({**base_folds[i], "sharpe": 1.0 + d})
    base = _wf(
        sharpe=1.0,
        trades=40,
        folds_won=0,
        folds_total=8,
        fold_metrics=base_folds,
        holdout_metrics=_folds(2, sharpe=0.8, trades=5, start_id=8),
        folds_holdout=2,
    )
    var = _wf(
        sharpe=1.02,
        trades=40,
        folds_won=5,
        folds_total=8,
        fold_metrics=var_folds,
        holdout_metrics=_folds(2, sharpe=0.85, trades=5, start_id=8),
        folds_holdout=2,
        win_rate=60,
    )
    verdict = goals.evaluate_walk_forward(base, var)
    assert verdict.promoted is False
    assert verdict.win_probability is not None
    assert verdict.win_probability < 0.95
    assert "win_probability" in verdict.reason or "Gewinnwahrscheinlichkeit" in verdict.reason


def test_significance_rejects_too_few_trades():
    goals = _goals_with()
    base = _wf(
        sharpe=0.7,
        trades=16,
        folds_won=0,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=0.7, trades=2),
        holdout_metrics=_folds(2, sharpe=0.7, trades=2, start_id=8),
        folds_holdout=2,
    )
    var = _wf(
        sharpe=1.1,
        trades=16,
        folds_won=8,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=1.1, trades=2),
        holdout_metrics=_folds(2, sharpe=0.9, trades=2, start_id=8),
        folds_holdout=2,
        win_rate=60,
    )
    verdict = goals.evaluate_walk_forward(base, var)
    assert verdict.promoted is False
    assert verdict.total_trades == 16
    assert "min_total_trades" in verdict.reason or "30" in verdict.reason


def test_threshold_tightens_with_n_variables_today():
    goals = _goals_with()
    base = _wf(
        sharpe=0.7,
        trades=40,
        folds_won=0,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=0.7, trades=5),
        holdout_metrics=_folds(2, sharpe=0.7, trades=5, start_id=8),
        folds_holdout=2,
    )
    var = _wf(
        sharpe=1.1,
        trades=40,
        folds_won=8,
        folds_total=8,
        fold_metrics=_folds(8, sharpe=1.1, trades=5),
        holdout_metrics=_folds(2, sharpe=0.9, trades=5, start_id=8),
        folds_holdout=2,
        win_rate=60,
    )
    v1 = goals.evaluate_walk_forward(base, var, n_variables_today=1)
    v5 = goals.evaluate_walk_forward(base, var, n_variables_today=5)
    assert v1.threshold_used == pytest.approx(0.95)
    assert v5.threshold_used == pytest.approx(0.99)
    assert v1.promoted is True


def test_config_slice2_defaults_and_hermes_disabled():
    from core.config import BotConfig

    hermes = BotConfig().raw.get("hermes") or {}
    assert hermes.get("enabled") is False
    v = hermes.get("validation") or {}
    assert int(v.get("backtest_days")) == 45
    assert int(v.get("holdout_folds", 2)) == 2
    assert float(v.get("min_win_probability", 0.95)) == 0.95
    assert int(v.get("min_total_trades", 30)) == 30
    promo = hermes.get("promotion") or {}
    assert int(promo.get("veto_window_min", 10)) == 10
    assert int(promo.get("max_promotions_per_day", 1)) == 1
    assert int(promo.get("post_apply_validation_hours", 24)) == 24


# ---------------------------------------------------------------------------
# Pending / veto / snapshot / rollback / post-apply
# ---------------------------------------------------------------------------


@pytest.fixture
def promo_env(hermes_memory_tmp, monkeypatch):
    from core.config import BotConfig
    from hermes.memory import store

    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["live_evidence"]["enabled"] = False
    raw["hermes"]["sync_to_config"] = True
    raw["hermes"].setdefault("promotion", {})
    raw["hermes"]["promotion"]["veto_window_min"] = 10
    raw["hermes"]["promotion"]["max_promotions_per_day"] = 1
    raw["hermes"]["promotion"]["post_apply_validation_hours"] = 24
    raw["hermes"]["promotion"]["post_apply_min_trades"] = 5
    cfg = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda *a, **k: cfg)
    monkeypatch.setattr("data_manager.get_config", lambda *a, **k: raw)
    monkeypatch.setattr("data_manager.reload_config", lambda *a, **k: raw)
    store.init_baseline_from_config(cfg, "ARIA/USDT", "4h")
    return cfg, raw, hermes_memory_tmp


def _pending_record(**kwargs):
    rec = {
        "id": kwargs.get("id", "exp_slice2"),
        "variable": "rsi_buy_low",
        "old_value": 30,
        "new_value": 28,
        "symbol": "ARIA/USDT",
        "timeframe": "4h",
        "params": {"rsi_buy_low": 28, "rsi_sell_30": 70},
        "baseline_params": {"rsi_buy_low": 30, "rsi_sell_30": 70},
        "variant_metrics": {
            "sharpe": 1.1,
            "win_rate": 60,
            "trades": 40,
            "trade_quality": 0.8,
        },
        "win_probability": 0.97,
        "total_trades": 41,
        "threshold_used": 0.95,
        "verdict": "pending",
    }
    rec.update(kwargs)
    return rec


def test_max_promotions_per_day_blocks_second_queue(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    rec1 = store.append_experiment(_pending_record(id="exp_a", variable="rsi_buy_low"))
    rec2 = store.append_experiment(_pending_record(id="exp_b", variable="rsi_sell_30"))
    r1 = promotion.queue_or_suppress(agent, rec1, observe=False, now=T0)
    r2 = promotion.queue_or_suppress(agent, rec2, observe=False, now=T0)
    assert r1["status"] == "pending"
    assert r2["status"] == "rejected"
    assert "max_promotions_per_day" in (r2.get("reason") or "")
    pending = promotion.load_pending()
    assert [p["experiment_id"] for p in pending] == ["exp_a"]


def test_pending_not_applied_before_veto_window(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    rec = store.append_experiment(_pending_record())
    promotion.queue_or_suppress(agent, rec, observe=False, now=T0)
    out = promotion.tick(agent, now=T0 + timedelta(minutes=9))
    assert out["applied"] == []
    assert store.load_baseline()["params"].get("rsi_buy_low") != 28
    agent._sync_to_config.assert_not_called()


def test_pending_applied_after_veto_window(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    rec = store.append_experiment(_pending_record())
    promotion.queue_or_suppress(agent, rec, observe=False, now=T0)
    out = promotion.tick(agent, now=T0 + timedelta(minutes=10, seconds=1))
    assert len(out["applied"]) == 1
    assert out["applied"][0]["experiment_id"] == "exp_slice2"
    assert store.load_baseline()["params"]["rsi_buy_low"] == 28
    agent._sync_to_config.assert_called()
    agent._notify_promotion.assert_called()
    exp = next(e for e in store.load_experiments()["experiments"] if e["id"] == "exp_slice2")
    assert exp["verdict"] == "promoted"
    assert exp.get("snapshot_path")
    snap = tmp / "snapshots" / "exp_slice2.json"
    assert snap.exists()


def test_veto_cancels_pending_promotion(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    rec = store.append_experiment(_pending_record())
    promotion.queue_or_suppress(agent, rec, observe=False, now=T0)
    vetoed = promotion.veto("exp_slice2", now=T0 + timedelta(minutes=1))
    assert vetoed["status"] == "vetoed"
    out = promotion.tick(agent, now=T0 + timedelta(minutes=30))
    assert out["applied"] == []
    exp = next(e for e in store.load_experiments()["experiments"] if e["id"] == "exp_slice2")
    assert exp["verdict"] == "vetoed"
    agent._sync_to_config.assert_not_called()


def test_snapshot_rollback_roundtrip_restores_baseline(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, raw, tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    before = store.load_baseline()
    old_rsi = before["params"]["rsi_buy_low"]
    rec = store.append_experiment(_pending_record())
    promotion.queue_or_suppress(agent, rec, observe=False, now=T0)
    promotion.tick(agent, now=T0 + timedelta(minutes=11))
    assert store.load_baseline()["params"]["rsi_buy_low"] == 28
    rolled = promotion.rollback("exp_slice2", agent=agent, now=T0 + timedelta(hours=1))
    assert rolled["verdict"] == "rolled_back"
    assert store.load_baseline()["params"]["rsi_buy_low"] == old_rsi
    exp = next(e for e in store.load_experiments()["experiments"] if e["id"] == "exp_slice2")
    assert exp["verdict"] == "rolled_back"
    agent._sync_to_config.assert_called()


def test_post_apply_reverts_on_synthetic_bad_orders(promo_env):
    from hermes import post_apply, promotion
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    rec = store.append_experiment(_pending_record())
    promotion.queue_or_suppress(agent, rec, observe=False, now=T0)
    promotion.tick(agent, now=T0 + timedelta(minutes=11))
    applied_at = T0 + timedelta(minutes=11)
    trades = []
    for i in range(6):
        trades.append(
            {
                "symbol": "ARIA/USDT",
                "type": "SELL",
                "pnl": -1.5,
                "timestamp": (applied_at + timedelta(hours=i + 1)).isoformat(),
                "hermes_experiment_id": "exp_slice2",
                "request": {"hermes_experiment_id": "exp_slice2"},
            }
        )
    decision = post_apply.evaluate(
        experiment_id="exp_slice2",
        symbol="ARIA/USDT",
        applied_at=applied_at,
        variant_metrics={"win_rate": 60, "trade_quality": 0.8},
        trades=trades,
        min_trades=5,
        win_rate_gap_pp=20,
    )
    assert decision.action == "revert"
    assert decision.realized_pnl < 0
    out = promotion.tick(
        agent,
        now=applied_at + timedelta(hours=24, minutes=1),
        trades=trades,
    )
    assert out["reverted"]
    exp = next(e for e in store.load_experiments()["experiments"] if e["id"] == "exp_slice2")
    assert exp["verdict"] == "rolled_back"


def test_post_apply_quiet_with_too_few_trades(promo_env):
    from hermes import post_apply

    applied_at = T0
    trades = [
        {
            "symbol": "ARIA/USDT",
            "type": "SELL",
            "pnl": -2.0,
            "timestamp": (applied_at + timedelta(hours=1)).isoformat(),
            "hermes_experiment_id": "exp_slice2",
            "request": {"hermes_experiment_id": "exp_slice2"},
        }
        for _ in range(4)
    ]
    decision = post_apply.evaluate(
        experiment_id="exp_slice2",
        symbol="ARIA/USDT",
        applied_at=applied_at,
        variant_metrics={"win_rate": 60, "trade_quality": 0.8},
        trades=trades,
        min_trades=5,
        win_rate_gap_pp=20,
    )
    assert decision.action == "no_verdict_yet"


def test_observe_mode_records_pending_but_never_applies(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = True
    rec = store.append_experiment({**_pending_record(), "verdict": "suppressed"})
    queued = promotion.queue_or_suppress(agent, rec, observe=True, now=T0)
    assert queued["status"] == "suppressed"
    out = promotion.tick(agent, now=T0 + timedelta(hours=2))
    assert out["applied"] == []
    agent._sync_to_config.assert_not_called()
    assert store.load_baseline()["params"].get("rsi_buy_low") != 28


def test_agent_status_shows_last_win_probability(promo_env, monkeypatch):
    from hermes.agent import HermesAgent
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    store.append_experiment(
        _pending_record(verdict="pending", win_probability=0.97, total_trades=41, threshold_used=0.95)
    )
    agent = HermesAgent(cfg)
    with patch("hermes.agent.format_active_pool_line", return_value="Pool (static): ARIA/USDT"):
        text = agent.status()
    assert "Gewinnwahrscheinlichkeit 0,97 bei 41 Trades" in text


def test_veto_message_uses_operator_facing_number():
    from hermes.promotion import format_veto_message

    rec = _pending_record()
    msg = format_veto_message(rec, window_min=10)
    assert "Gewinnwahrscheinlichkeit 0,97 bei 41 Trades" in msg
    assert "/hermes_veto" in msg
    assert "exp_slice2" in msg
    assert "p-value" not in msg.lower()


def test_post_apply_ledger_reads_active_scope_not_hardcoded_live(monkeypatch):
    """Review #308: the unit suite runs in the demo scope; post-apply must follow it."""
    from data_manager import resolve_ledger_scope
    from hermes import post_apply

    seen: dict = {}

    def fake_load_orders(scope="live"):
        seen["scope"] = scope
        return [
            {
                "id": "o1",
                "side": "sell",
                "symbol": "ARIA/USDT",
                "pnl": -1.0,
                "timestamps": {"created": "2026-09-06T10:00:00"},
                "request": {"hermes_experiment_id": "exp-scope"},
            }
        ]

    monkeypatch.setattr("hermes.live_evidence._load_orders", fake_load_orders)
    monkeypatch.setattr("data_manager.load_trade_history_document", lambda scope, *a, **k: {"trades": []})
    rows = post_apply.load_ledger_trades("ARIA/USDT")
    assert seen["scope"] == resolve_ledger_scope()
    assert seen["scope"] == "demo"
    assert any(r.get("hermes_experiment_id") == "exp-scope" for r in rows)

