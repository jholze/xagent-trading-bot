"""Orchestrator stamps exit_source on sell TradeOrders from SignalAnalysis."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.models import SignalAnalysis, TradeResult
from services.signal_orchestrator import SignalOrchestrator


def _analysis(**kwargs) -> SignalAnalysis:
    base = dict(
        action="SELL_30",
        symbol="LAB/USDT",
        timeframe="4h",
        rsi=70.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["time_profit_exit", "technical"],
        normalized_action="SELL_PARTIAL_50",
        rationale="Time->profit exit (50h held, gain=3.0%, sell 50%)",
        sell_source="time_profit_exit",
        recommended=True,
    )
    base.update(kwargs)
    return SignalAnalysis(**base)


class TestOrchestratorExitSource(unittest.TestCase):
    def test_sell_order_gets_exit_source(self):
        trading = MagicMock()
        captured = {}

        def _exec(order, *a, **k):
            captured["order"] = order
            return TradeResult(True, "SELL", order.symbol, amount=1, price=1.0)

        trading.execute_order.side_effect = _exec
        trading.refresh = MagicMock()
        orch = SignalOrchestrator()
        orch.trading = trading

        with patch(
            "services.signal_orchestrator.find_open_position_for_symbol",
            return_value=("4h", {"amount": 100.0}),
        ), patch(
            "services.signal_orchestrator.resolve_coin_config",
            return_value={"strategy_params": {}},
        ), patch(
            "strategies.positions.sell_fraction_for_signal",
            return_value=0.5,
        ):
            orch.execute_if_needed(
                _analysis(),
                coin={"symbol": "LAB/USDT", "timeframe": "4h"},
                current_price=0.15,
            )

        order = captured.get("order")
        self.assertIsNotNone(order)
        self.assertEqual(order.source, "auto")
        self.assertEqual(order.exit_source, "time_profit_exit")
        self.assertIn("Time->profit", order.exit_rationale)
        self.assertEqual(order.signal, "SELL_PARTIAL_50")


if __name__ == "__main__":
    unittest.main()
