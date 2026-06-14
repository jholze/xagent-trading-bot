from unittest.mock import patch

import pytest

from hermes.agent import HermesAgent
from hermes.live_evidence import LiveMetrics


@pytest.fixture
def hybrid_agent(monkeypatch, hermes_memory_tmp):
    from core.config import BotConfig

    raw = BotConfig().raw
    raw["hermes"]["symbols_mode"] = "hybrid"
    raw["hermes"]["live_evidence"]["enabled"] = True
    cfg = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda: cfg)
    return HermesAgent(cfg)


def test_status_includes_pool_and_live_lines(hybrid_agent):
    with patch("hermes.agent.resolve_active_symbols", return_value=["ARIA/USDT", "STG/USDT"]), \
         patch(
             "hermes.agent.compute_live_metrics",
             return_value=LiveMetrics(symbol="STG/USDT", live_trades=3, live_sell_trades=2, live_sell_pnl=4.34),
         ), \
         patch("hermes.agent.format_active_pool_line", return_value="Pool (hybrid): ARIA/USDT, STG/USDT"):
        text = hybrid_agent.status()
    assert "Pool (hybrid)" in text
    assert "Live evidence: enabled" in text
    assert "STG/USDT" in text
    assert "+4.34" in text