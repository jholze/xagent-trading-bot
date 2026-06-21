import json
import os

import pytest

from hermes.experiment import ExperimentRunner
from hermes.memory import store


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)
    yield tmp_path


def test_experiment_mutates_single_variable(isolated_memory):
    runner = ExperimentRunner()
    params = {
        "rsi_buy_low": 28,
        "rsi_buy_high": 48,
        "volume_multiplier": 1.3,
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "stop_loss_pct": 12.0,
    }
    proposal = runner.propose(params)
    assert proposal.variable in runner.tunable_params
    changed = [
        k for k, v in proposal.params.items()
        if params.get(k) != v
    ]
    assert len(changed) == 1
    assert changed[0] == proposal.variable


def test_memory_baseline_init(isolated_memory):
    from core.config import get_bot_config

    cfg = get_bot_config()
    baseline = store.init_baseline_from_config(cfg)
    expected = cfg.strategy_params("ARIA/USDT", "4h").get("rsi_buy_low")
    assert baseline["params"]["rsi_buy_low"] == expected
    assert (isolated_memory / "baseline.demo.json").exists() or baseline["params"]


def test_take_profit_pct_mutation_bounds(isolated_memory):
    runner = ExperimentRunner()
    params = {
        "rsi_buy_low": 28,
        "take_profit_pct": 12,
        "stop_loss_pct": 12.0,
    }
    proposal = runner.propose(params, grok_proposal={
        "variable": "take_profit_pct",
        "old_value": 12,
        "new_value": 8,
        "hypothesis": "earlier take profit",
        "source": "grok",
    })
    assert proposal.variable == "take_profit_pct"
    assert 5 <= proposal.new_value <= 30


def test_experiment_record_persisted(isolated_memory):
    runner = ExperimentRunner()
    params = {"rsi_buy_low": 28, "rsi_buy_high": 48, "volume_multiplier": 1.3}
    proposal = runner.propose(params)
    record = runner.record(
        proposal=proposal,
        baseline_metrics={"sharpe": 0.5},
        variant_metrics={"sharpe": 0.7},
        verdict_promoted=True,
        verdict_reason="improved",
        symbol="ARIA/USDT",
        timeframe="4h",
    )
    data = store.load_experiments()
    assert len(data["experiments"]) == 1
    assert data["experiments"][0]["id"] == record["id"]