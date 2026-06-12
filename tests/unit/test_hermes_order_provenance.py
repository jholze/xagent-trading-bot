from unittest.mock import MagicMock, patch

from core.models import SignalAnalysis, TradeResult
from services.signal_orchestrator import SignalOrchestrator


def test_hermes_source_when_strategy_has_experiment_id():
    orch = SignalOrchestrator()
    coin = {"symbol": "ARIA/USDT", "timeframe": "4h"}
    analysis = SignalAnalysis(
        action="BUY",
        symbol="ARIA/USDT",
        timeframe="4h",
        rsi=40.0,
        lower_bb=1.0,
        vol_multiplier=1.5,
        ampel_emoji="🟢",
        ampel_text="Bullish",
        sources=["technical"],
    )

    with patch("services.signal_orchestrator.resolve_coin_config") as mock_resolve:
        mock_resolve.return_value = {
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "strategy_params": {
                "hermes_experiment_id": "exp_abc123",
                "hermes_updated_at": "2026-06-12T00:00:00",
            },
        }
        with patch.object(orch.trading, "execute_order", return_value=TradeResult(False, "BUY", "ARIA/USDT")) as mock_exec:
            orch.execute_if_needed(analysis, coin, 1.5)
            mock_exec.assert_called_once()
            kwargs = mock_exec.call_args.kwargs
            assert kwargs["source"] == "hermes"
            assert kwargs["request_extra"]["hermes_experiment_id"] == "exp_abc123"