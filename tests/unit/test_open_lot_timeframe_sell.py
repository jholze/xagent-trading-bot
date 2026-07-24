"""Sell sizing must follow the open lot timeframe (issue #117).

Volatile entries often live on 1h while analysis/watchlist still runs 4h.
Sells must not resolve amount=0 / no_amount against an empty 4h key.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.models import SignalAnalysis, TradeOrder, TradeResult
from services.signal_orchestrator import SignalOrchestrator
from strategies.positions import (
    find_open_position_for_symbol,
    get_position,
    parse_position_key,
)


def _analysis(**kwargs) -> SignalAnalysis:
    base = dict(
        action="SELL_FULL",
        symbol="BEAT/USDT",
        timeframe="4h",
        rsi=70.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["trailing_stop", "technical"],
        normalized_action="SELL_FULL",
        rationale="Trail->ATR stop",
        sell_source="trailing_stop",
        recommended=True,
    )
    base.update(kwargs)
    return SignalAnalysis(**base)


class TestParsePositionKey(unittest.TestCase):
    def test_parse_common_keys(self):
        self.assertEqual(parse_position_key("BEAT_USDT_1h"), ("BEAT/USDT", "1h"))
        self.assertEqual(parse_position_key("BTC_USDT_4h"), ("BTC/USDT", "4h"))


class TestFindOpenPositionForSymbol(unittest.TestCase):
    def test_prefers_preferred_tf_when_open(self):
        store = {
            "BEAT_USDT_1h": {
                "amount": Decimal("100"),
                "average_entry": 2.5,
                "sold_percent": 0.0,
            },
            "BEAT_USDT_4h": {
                "amount": Decimal("50"),
                "average_entry": 2.5,
                "sold_percent": 0.0,
            },
        }
        with patch("strategies.positions._active_store", return_value=store), \
             patch("strategies.positions._activate"), \
             patch("strategies.positions._positions_lock"):
            found = find_open_position_for_symbol("BEAT/USDT", preferred_timeframe="4h")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "4h")
        self.assertEqual(float(found[1]["amount"]), 50.0)

    def test_picks_largest_lot_without_preference(self):
        store = {
            "BEAT_USDT_1h": {
                "amount": Decimal("848"),
                "average_entry": 2.6266,
                "sold_percent": 0.0,
                "entry_at": "2026-07-16T16:58:23",
            },
            "BEAT_USDT_15m": {
                "amount": Decimal("10"),
                "average_entry": 2.6,
                "sold_percent": 0.0,
                "entry_at": "2026-07-20T00:00:00",
            },
        }
        with patch("strategies.positions._active_store", return_value=store), \
             patch("strategies.positions._activate"), \
             patch("strategies.positions._positions_lock"):
            found = find_open_position_for_symbol("BEAT/USDT")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "1h")
        self.assertAlmostEqual(float(found[1]["amount"]), 848.0)

    def test_none_when_no_open_lot(self):
        store = {
            "BEAT_USDT_1h": {
                "amount": Decimal("0"),
                "average_entry": 2.5,
                "sold_percent": 1.0,
            },
        }
        with patch("strategies.positions._active_store", return_value=store), \
             patch("strategies.positions._activate"), \
             patch("strategies.positions._positions_lock"):
            self.assertIsNone(find_open_position_for_symbol("BEAT/USDT"))


class TestOrchestratorSellUsesLotTimeframe(unittest.TestCase):
    def test_sell_full_on_4h_analysis_uses_1h_lot(self):
        trading = MagicMock()
        captured = {}

        def _exec(order, timeframe, **kwargs):
            captured["order"] = order
            captured["timeframe"] = timeframe
            return TradeResult(True, "SELL", order.symbol, amount=order.amount, price=order.price)

        trading.execute_order.side_effect = _exec
        trading.refresh = MagicMock()
        orch = SignalOrchestrator()
        orch.trading = trading

        lot = {
            "amount": Decimal("848.2448793116578"),
            "average_entry": 2.6266,
            "strategy_tier": "volatile",
            "sold_percent": 0.0,
        }

        with patch(
            "services.signal_orchestrator.find_open_position_for_symbol",
            return_value=("1h", lot),
        ), patch(
            "services.signal_orchestrator.resolve_coin_config",
            return_value={"strategy_params": {}},
        ), patch(
            "strategies.positions.sell_fraction_for_signal",
            return_value=1.0,
        ):
            result = orch.execute_if_needed(
                _analysis(timeframe="4h"),
                coin={"symbol": "BEAT/USDT", "timeframe": "4h"},
                current_price=3.2632,
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.executed)
        self.assertEqual(captured["timeframe"], "1h")
        self.assertAlmostEqual(float(captured["order"].amount), 848.2448793116578)
        self.assertEqual(captured["order"].signal, "SELL_FULL")
        self.assertEqual(captured["order"].exit_source, "trailing_stop")

    def test_skip_sell_when_no_open_lot(self):
        trading = MagicMock()
        orch = SignalOrchestrator()
        orch.trading = trading
        trading.refresh = MagicMock()

        with patch(
            "services.signal_orchestrator.find_open_position_for_symbol",
            return_value=None,
        ), patch(
            "services.signal_orchestrator.resolve_coin_config",
            return_value={"strategy_params": {}},
        ):
            result = orch.execute_if_needed(
                _analysis(),
                coin={"symbol": "BEAT/USDT", "timeframe": "4h"},
                current_price=3.0,
            )

        self.assertIsNone(result)
        trading.execute_order.assert_not_called()


class TestRiskFillFromOpenLot(unittest.TestCase):
    def test_no_amount_repaired_from_1h_lot(self):
        from risk.risk_manager import RiskManager

        cfg = MagicMock()
        cfg.raw = {}
        market = MagicMock()
        rm = RiskManager(cfg, market)

        order = TradeOrder(
            type="SELL",
            symbol="BEAT/USDT",
            price=3.26,
            amount=0,
            signal="SELL_FULL",
            source="auto",
        )
        lot = {
            "amount": Decimal("100"),
            "average_entry": 2.5,
            "sold_percent": 0.0,
        }

        with patch(
            "risk.risk_manager.find_open_position_for_symbol",
            return_value=("1h", lot),
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), \
             patch.object(rm, "_resolve_sell_order", side_effect=lambda o, *a, **k: o), \
             patch.object(rm, "_partial_sell_blocked", return_value=(False, "")), \
             patch.object(rm, "_effective_max_daily_sells", return_value=0), \
             patch.object(rm, "_daily_sells_count", return_value=0), \
             patch("risk.risk_manager.sell_fraction_for_signal", return_value=1.0):
            decision = rm.evaluate(order, "4h", source="auto")

        self.assertTrue(decision.approved)
        self.assertAlmostEqual(float(decision.order.amount), 100.0)


class TestResolveEffectiveTimeframeOpenLot(unittest.TestCase):
    def test_open_1h_lot_overrides_watchlist_4h(self):
        from strategies.registry import resolve_effective_timeframe

        coin = {"symbol": "BEAT/USDT", "timeframe": "4h", "source": "cmc_trending"}
        with patch(
            "strategies.registry.get_bot_config",
            return_value=MagicMock(
                volatile_altcoin_config={"enabled": True, "timeframe": "1h"}
            ),
        ), patch(
            "strategies.positions.find_open_position_for_symbol",
            return_value=("1h", {"amount": 10, "average_entry": 1.0}),
        ):
            self.assertEqual(resolve_effective_timeframe(coin), "1h")


if __name__ == "__main__":
    unittest.main()
