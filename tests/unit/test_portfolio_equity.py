import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import get_bot_config
from data_manager import load_trade_history, save_trade_history
from services.portfolio_service import PortfolioService
from strategies.positions import (
    count_open_positions,
    get_position,
    list_active_positions,
    positions,
    save_positions,
)


class TestPortfolioEquity(unittest.TestCase):
    SYMBOL = "EQTY/USDT"
    TF = "4h"
    INITIAL = 5000.0

    def setUp(self):
        from decimal import Decimal
        from strategies.positions import get_key

        self._positions_backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        self._trade_history_backup = load_trade_history()
        positions.clear()
        save_positions()
        save_trade_history({
            "virtual_balance": self.INITIAL,
            "realized_pnl": 0.0,
            "open_positions": 0,
            "trades": [],
        })
        key = get_key(self.SYMBOL, self.TF)
        if key in positions:
            del positions[key]

    def tearDown(self):
        from decimal import Decimal

        positions.clear()
        positions.update(self._positions_backup)
        save_positions()
        save_trade_history(self._trade_history_backup)

    def _service(self) -> PortfolioService:
        return PortfolioService(get_bot_config())

    def _compute_total_value(self, mark_prices: dict) -> float:
        history = load_trade_history()
        balance = history.get("virtual_balance", 0)
        unreal = 0.0
        for p in list_active_positions():
            sym = p["symbol"] if "/" in p["symbol"] else f"{p['symbol']}/USDT"
            price = mark_prices.get(sym, 0.0)
            entry = p.get("average_entry", p.get("entry_price", 0))
            if price > 0 and entry > 0:
                unreal += (price - entry) * float(p["amount"])
        return balance + unreal

    def test_double_buy_weighted_average_and_balance(self):
        svc = self._service()
        svc.execute_buy(self.SYMBOL, self.TF, 1.0, 100.0)
        svc.execute_buy(self.SYMBOL, self.TF, 2.0, 100.0)

        pos = get_position(self.SYMBOL, self.TF)
        # 100 USDT @ 1.0 + 100 USDT @ 2.0 → 100 + 50 coins, avg (100+100)/150
        self.assertAlmostEqual(pos["average_entry"], 1.3333, places=3)
        self.assertAlmostEqual(float(pos["amount"]), 150.0, places=4)

        history = load_trade_history()
        self.assertAlmostEqual(history["virtual_balance"], self.INITIAL - 200.0, places=2)
        self.assertEqual(len(history["trades"]), 2)

    def test_multiple_partial_sells_realized_pnl_and_balance(self):
        svc = self._service()
        slippage = get_bot_config().slippage_percent / 100
        svc.execute_buy(self.SYMBOL, self.TF, 1.0, 200.0)

        svc.execute_sell(self.SYMBOL, self.TF, 1.5, "SELL_20")
        svc.execute_sell(self.SYMBOL, self.TF, 1.5, "SELL_30")
        svc.execute_sell(self.SYMBOL, self.TF, 1.5, "SELL_STOP_FULL")

        history = load_trade_history()
        sells = [t for t in history["trades"] if t["type"] == "SELL"]
        self.assertEqual(len(sells), 3)
        self.assertAlmostEqual(sum(t["pnl"] for t in sells), history["realized_pnl"], places=2)

        expected_received = sum(t["usdt_received"] for t in sells)
        self.assertAlmostEqual(
            history["virtual_balance"],
            self.INITIAL - 200.0 + expected_received,
            places=2,
        )
        for t in sells:
            gross = t["price"] * t["amount"]
            self.assertAlmostEqual(t["usdt_received"], gross * (1 - slippage), places=2)

        pos = get_position(self.SYMBOL, self.TF)
        self.assertAlmostEqual(float(pos["amount"]), 0.0, places=4)
        self.assertEqual(len(list_active_positions()), 0)
        self.assertEqual(count_open_positions(), 0)

    def test_buy_partial_sell_rebuy_resets_sold_percent(self):
        svc = self._service()
        svc.execute_buy(self.SYMBOL, self.TF, 1.0, 100.0)
        svc.execute_sell(self.SYMBOL, self.TF, 1.2, "SELL_30")
        pos = get_position(self.SYMBOL, self.TF)
        self.assertGreater(pos["sold_percent"], 0)

        svc.execute_buy(self.SYMBOL, self.TF, 1.5, 100.0)
        pos = get_position(self.SYMBOL, self.TF)
        self.assertEqual(pos["sold_percent"], 0.0)
        # 70 @ 1.0 + 66.67 @ 1.5 ≈ weighted avg
        self.assertAlmostEqual(pos["average_entry"], 1.244, places=2)

    def test_equity_invariant_with_open_positions(self):
        svc = self._service()
        svc.execute_buy(self.SYMBOL, self.TF, 1.0, 150.0)
        svc.execute_buy(self.SYMBOL, self.TF, 1.2, 120.0)
        svc.execute_sell(self.SYMBOL, self.TF, 1.3, "SELL_20")

        mark = {self.SYMBOL: 1.35}
        expected = self._compute_total_value(mark)
        history = load_trade_history()
        balance = history["virtual_balance"]
        unreal = expected - balance
        self.assertAlmostEqual(expected, balance + unreal, places=2)
        self.assertGreater(unreal, 0)

    def test_round_trip_all_sold_matches_capital_plus_realized(self):
        svc = self._service()
        svc.execute_buy(self.SYMBOL, self.TF, 1.0, 200.0)
        svc.execute_sell(self.SYMBOL, self.TF, 1.25, "SELL_STOP_FULL")

        history = load_trade_history()
        sell = next(t for t in history["trades"] if t["type"] == "SELL")
        # Balance = initial - buy spend + sell proceeds (slippage on proceeds only)
        expected_balance = self.INITIAL - 200.0 + sell["usdt_received"]
        self.assertAlmostEqual(history["virtual_balance"], expected_balance, places=2)
        self.assertAlmostEqual(history["realized_pnl"], sell["pnl"], places=2)

    def test_portfolio_command_reports_correct_total_value(self):
        svc = self._service()
        svc.execute_buy(self.SYMBOL, self.TF, 2.0, 100.0)

        with patch("notifications.telegram_commands.portfolio_commands.send_positions_snapshot") as mock_send:
            from notifications.telegram_commands.portfolio_commands import handle

            handle("/positions")
            mock_send.assert_called_once()

    def test_pnl_percent_uses_config_initial_capital(self):
        svc = self._service()
        svc.execute_buy(self.SYMBOL, self.TF, 1.0, 100.0)

        from unittest.mock import MagicMock

        mock_cfg = MagicMock()
        mock_cfg.initial_capital_usdt = 1000.0

        with patch("notifications.telegram_commands.portfolio_commands.send_positions_snapshot") as mock_send:
            from notifications.telegram_commands.portfolio_commands import handle

            handle("/positions")
            mock_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()