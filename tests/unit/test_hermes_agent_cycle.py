from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.models import SandboxMetrics
from hermes.agent import HermesAgent
from hermes.validation import WalkForwardResult
from intelligence.grok_json import GrokError


@pytest.fixture
def agent_with_mocks(monkeypatch, hermes_memory_tmp, sample_live_trade_history):
    from core.config import BotConfig
    from hermes.memory import store

    raw = BotConfig().raw
    raw["hermes"]["live_evidence"]["enabled"] = True
    raw["hermes"]["symbols_mode"] = "static"
    raw["hermes"]["symbols"] = ["ARIA/USDT"]
    cfg = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda *a, **k: cfg)
    monkeypatch.setattr("data_manager.get_config", lambda *a, **k: raw)
    monkeypatch.setattr("data_manager.reload_config", lambda *a, **k: raw)
    monkeypatch.setattr("core.config.reload_config", lambda *a, **k: raw)

    store.init_baseline_from_config(cfg, "ARIA/USDT", "4h")

    agent = HermesAgent(cfg)
    df = pd.DataFrame({"ts": list(range(100)), "close": [1.0] * 100})

    aggregate = SandboxMetrics(sharpe=0.0, trades=0, opportunity_score=0.0, trade_quality=0.0)
    wf = WalkForwardResult(
        symbol="ARIA/USDT",
        timeframe="4h",
        params={},
        fold_metrics=[],
        aggregate=aggregate,
        folds_total=4,
        folds_won=0,
    )

    monkeypatch.setattr(agent.backtester, "_fetch_ohlcv", lambda *a, **k: df)
    monkeypatch.setattr(
        "price_fetcher.get_gate_prices_batch",
        lambda symbols: {s: 1.0 for s in symbols},
    )
    monkeypatch.setattr("price_fetcher.get_ticker_price", lambda symbol, exchange=None: 1.0)
    monkeypatch.setattr(
        "services.market_service.MarketService._fetch_ohlcv",
        lambda *a, **k: df,
    )
    monkeypatch.setattr(
        "hermes.symbol_pool.load_effective_watchlist",
        lambda *a, **k: [{"symbol": "ARIA/USDT", "timeframe": "4h", "active": True}],
    )
    monkeypatch.setattr("hermes.agent.run_walk_forward", lambda *a, **k: wf)
    monkeypatch.setattr(
        "hermes.agent.compute_counterfactual_delta",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "hermes.agent.compute_live_metrics",
        lambda *a, **k: MagicMock(
            to_dict=lambda: {"live_sell_pnl": -5.0, "live_trades": 2},
            live_trades=2,
            live_sell_trades=1,
            live_sell_pnl=-5.0,
            lookback_days=7,
        ),
    )
    monkeypatch.setattr(
        agent.improver,
        "propose_experiment",
        lambda baseline: None,
    )
    def _no_grok(*_a, **_k):
        raise GrokError("blocked")

    monkeypatch.setattr("hermes.self_improver.ask_grok_json", _no_grok)
    return agent


def test_run_cycle_records_live_metrics(agent_with_mocks):
    with patch.object(agent_with_mocks.improver, "analyze_and_suggest", return_value="ok"):
        result = agent_with_mocks.run_cycle()
    assert result.symbol == "ARIA/USDT"
    from hermes.memory import store

    experiments = store.load_experiments()["experiments"]
    assert experiments
    last = experiments[-1]
    assert "live_metrics" in last
    assert last["live_metrics"]["live_sell_pnl"] == -5.0