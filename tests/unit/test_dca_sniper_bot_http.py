"""Bot HTTP helpers for dca_sniper (token + execute pure)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestDcaSniperBotHttp(unittest.TestCase):
    def test_execute_bad_args(self):
        from services.dca_sniper.bot_http import execute_sniper_dca

        body, code = execute_sniper_dca({})
        self.assertEqual(code, 400)
        self.assertFalse(body.get("executed"))

    def test_fund_sell_blocks_recovery_hold(self):
        from services.dca_sniper.bot_http import execute_fund_sell

        with patch("services.dca_sniper.bot_http.get_position", create=True):
            # patch at strategies.positions used inside function
            with patch(
                "strategies.positions.get_position",
                return_value={
                    "amount": 10,
                    "average_entry": 1.0,
                    "recovery_hold": True,
                },
            ), patch("strategies.positions.is_open_position", return_value=True):
                body, code = execute_fund_sell(
                    {"symbol": "X/USDT", "timeframe": "1h", "price": 1.2}
                )
        self.assertEqual(code, 409)
        self.assertIn("recovery_hold", body.get("message") or "")

    def test_execute_sets_hold_on_success(self):
        from services.dca_sniper.bot_http import execute_sniper_dca

        pos = {"amount": 5, "average_entry": 1.0}
        mock_result = MagicMock()
        mock_result.executed = True
        mock_result.message = "filled"
        mock_result.price = 0.8

        mock_trading = MagicMock()
        mock_trading.execute_order.return_value = mock_result

        with patch(
            "services.trading_service.TradingService", return_value=mock_trading
        ), patch("strategies.positions.get_position", return_value=pos), patch(
            "strategies.positions.flush_positions"
        ), patch(
            "services.market_service.MarketService"
        ) as MS:
            MS.return_value.get_price.return_value = 0.8
            body, code = execute_sniper_dca(
                {
                    "symbol": "Y/USDT",
                    "timeframe": "1h",
                    "usdt": 500,
                    "price": 0.8,
                    "set_recovery_hold": True,
                    "heavy": True,
                    "score": 8,
                }
            )
        self.assertTrue(body.get("executed"))
        self.assertTrue(pos.get("recovery_hold"))
        self.assertTrue(pos.get("sniper_focus"))
        self.assertTrue(pos.get("dca_heavy_used"))


if __name__ == "__main__":
    unittest.main()
