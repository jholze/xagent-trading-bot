"""Dry-run sim cash and portfolio value invariants across multi-step trade flows."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import (
    LIVE_TRADE_HISTORY_FILE,
    compute_sim_cash_from_trades,
    compute_sim_realized_pnl,
    load_live_trade_history,
    record_live_trade,
    save_live_trade_history,
)
from execution.gate_adapter import GateExecutionAdapter
from notifications.telegram_commands.position_display import format_portfolio_summary
from risk.risk_manager import RiskManager
from services.gate_balance import fetch_portfolio_equity, fetch_usdt_balance
from strategies.positions import get_position, list_active_positions, load_positions


def _position_market_value(active: list, prices: dict) -> float:
    total = 0.0
    for p in active:
        sym = p["symbol"] if "/" in p["symbol"] else f"{p['symbol']}/USDT"
        amount = float(p.get("amount", 0) or 0)
        price = float(prices.get(sym, 0) or 0)
        if price > 0 and amount > 0:
            total += amount * price
    return total


def _unrealized_pnl(active: list, prices: dict) -> float:
    total = 0.0
    for p in active:
        sym = p["symbol"] if "/" in p["symbol"] else f"{p['symbol']}/USDT"
        amount = float(p.get("amount", 0) or 0)
        entry = float(p.get("average_entry", p.get("entry_price", 0)) or 0)
        price = float(prices.get(sym, 0) or 0)
        if price > 0 and entry > 0 and amount > 0:
            total += (price - entry) * amount
    return total


class DryRunPortfolioHarness:
    """Isolated live dry-run ledger in a temp directory."""

    def __init__(self, initial: float = 5000.0):
        self.tmp = tempfile.TemporaryDirectory()
        self.initial = initial
        self.live_hist_path = os.path.join(self.tmp.name, LIVE_TRADE_HISTORY_FILE)
        self.positions_path = os.path.join(self.tmp.name, "positions.live.json")
        self.raw = {
            "trading_mode": "live",
            "live_confirmed": True,
            "initial_capital_usdt": initial,
            "max_usdt_per_trade": 500,
            "max_open_positions": 10,
            "max_position_percent": 50,
            "max_daily_trades": 50,
            "slippage_percent": 0,
            "live": {
                "dry_run": True,
                "dry_run_enhanced": True,
                "simulated_balance_usdt": initial,
            },
        }
        self._patches = [
            patch.dict(os.environ, {"DEMO_MODE": "0"}),
            patch("data_manager.get_data_file", side_effect=self._data_file),
            patch("data_manager.get_config", return_value=self.raw),
            patch("data_manager.is_dry_run_enhanced", return_value=True),
            patch("data_manager.is_demo_mode", return_value=False),
            patch("data_manager.resolve_ledger_scope", return_value="live"),
            patch("data_manager.POSITIONS_SCOPE_FILES", {
                "demo": os.path.join(self.tmp.name, "positions.demo.json"),
                "paper": os.path.join(self.tmp.name, "positions.paper.json"),
                "live": self.positions_path,
            }),
            patch("strategies.positions.resolve_ledger_scope", return_value="live"),
            patch("strategies.positions.resolve_positions_file", return_value=self.positions_path),
        ]
        for p in self._patches:
            p.start()
        save_live_trade_history({"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0})
        with open(self.positions_path, "w", encoding="utf-8") as f:
            json.dump({"positions": {}, "ledger_scope": "live"}, f)
        load_positions(scope="live")

    def _data_file(self, name: str) -> str:
        if name == LIVE_TRADE_HISTORY_FILE:
            return self.live_hist_path
        return os.path.join(self.tmp.name, name)

    def config(self) -> BotConfig:
        cfg = BotConfig()
        cfg._raw = dict(self.raw)
        return cfg

    def adapter(self) -> GateExecutionAdapter:
        return GateExecutionAdapter(self.config())

    def execute_buy(self, symbol: str, usdt: float, price: float, source: str = "manual") -> None:
        order = TradeOrder("BUY", symbol, price, 0, usdt_amount=usdt, source=source)
        result = self.adapter().execute(order, "4h")
        self.assert_executed(result)

    def execute_sell(self, symbol: str, amount: float, price: float, signal: str = "SELL", source: str = "manual") -> None:
        order = TradeOrder("SELL", symbol, price, amount, signal=signal, source=source)
        result = self.adapter().execute(order, "4h")
        self.assert_executed(result)

    @staticmethod
    def assert_executed(result) -> None:
        if not result.executed:
            raise AssertionError(result.message or "trade not executed")

    def history(self) -> dict:
        return load_live_trade_history()

    def assert_portfolio_invariant(self, prices: dict, tol: float = 0.05) -> None:
        history = self.history()
        cash = float(history["virtual_balance"])
        active = list_active_positions()
        market_value = _position_market_value(active, prices)
        unreal = _unrealized_pnl(active, prices)
        realized = float(history.get("realized_pnl", history.get("total_pnl", 0)))
        total = cash + market_value

        self._assert_close(total, self.initial + realized + unreal, tol, "equity identity")
        self._assert_close(
            compute_sim_cash_from_trades(history.get("trades", []), self.initial),
            cash,
            0.01,
            "cash replay",
        )
        self._assert_close(
            fetch_usdt_balance(self.config()),
            cash,
            0.01,
            "fetch_usdt_balance",
        )
        self._assert_close(
            fetch_portfolio_equity(self.config(), reference_prices=prices),
            total,
            tol,
            "fetch_portfolio_equity",
        )

    @staticmethod
    def _assert_close(actual, expected, tol, label) -> None:
        if abs(actual - expected) > tol:
            raise AssertionError(f"{label}: expected {expected:.4f}, got {actual:.4f}")

    def cleanup(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()


class TestDryRunPortfolioMath(unittest.TestCase):
    def test_compute_sim_cash_replays_user_trade_sequence(self):
        trades = [
            {"type": "SELL", "usdt_received": 27.947503484111127},
            {"type": "SELL", "usdt_received": 49.98945919999999},
            {"type": "BUY", "usdt_amount": 10},
            {"type": "BUY", "usdt_amount": 10},
            {"type": "BUY", "usdt_amount": 10},
            {"type": "BUY", "usdt_amount": 11.82},
            {"type": "BUY", "usdt_amount": 500},
            {"type": "BUY", "usdt_amount": 500},
            {"type": "SELL", "usdt_received": 166.07047279214987},
            {"type": "BUY", "usdt_amount": 250},
        ]
        cash = compute_sim_cash_from_trades(trades, 5000)
        self.assertAlmostEqual(cash, 3952.18742847626, places=2)

    def test_stored_drift_is_corrected_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, LIVE_TRADE_HISTORY_FILE)
            cfg = {
                "trading_mode": "live",
                "live": {"dry_run": True, "dry_run_enhanced": True, "simulated_balance_usdt": 5000},
            }
            payload = {
                "trades": [{"type": "BUY", "usdt_amount": 100}],
                "virtual_balance": 9999.0,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            with patch("data_manager.get_data_file", return_value=path), \
                 patch("data_manager.get_config", return_value=cfg), \
                 patch("data_manager.is_dry_run_enhanced", return_value=True), \
                 patch("data_manager._reconcile_live_trade_sources", return_value=(payload, False)):
                history = load_live_trade_history()
            self.assertAlmostEqual(history["virtual_balance"], 4900.0)


class TestDryRunPortfolioFlows(unittest.TestCase):
    def setUp(self):
        self.harness = DryRunPortfolioHarness(initial=5000.0)

    def tearDown(self):
        self.harness.cleanup()

    def test_single_buy_reduces_sim_cash(self):
        self.harness.execute_buy("CAT/USDT", 500, 1.514e-06)
        history = self.harness.history()
        self.assertAlmostEqual(history["virtual_balance"], 4500.0)
        self.harness.assert_portfolio_invariant({"CAT/USDT": 1.514e-06})

    def test_buy_sell_round_trip_updates_cash_and_realized_pnl(self):
        self.harness.execute_buy("ARIA/USDT", 200, 0.04)
        pos = get_position("ARIA/USDT", "4h")
        amount = float(pos["amount"])
        self.harness.execute_sell("ARIA/USDT", amount, 0.05, signal="SELL_STOP_FULL", source="auto")

        history = self.harness.history()
        expected_profit = amount * (0.05 - 0.04)
        self.assertAlmostEqual(history["virtual_balance"], 5000.0 + expected_profit, places=2)
        self.assertAlmostEqual(history["realized_pnl"], expected_profit, places=2)
        self.harness.assert_portfolio_invariant({"ARIA/USDT": 0.05})

    def test_multi_coin_partial_sell_sequence(self):
        self.harness.execute_buy("CAT/USDT", 500, 1.514e-06)
        self.harness.execute_buy("ARIA/USDT", 500, 0.03363)
        self.harness.execute_buy("ZBT/USDT", 250, 0.12078)

        aria_pos = get_position("ARIA/USDT", "4h")
        sell_amount = float(aria_pos["amount"]) * 0.3
        self.harness.execute_sell("ARIA/USDT", sell_amount, 0.0378, signal="SELL_PARTIAL_30", source="auto")

        prices = {"CAT/USDT": 1.514e-06, "ARIA/USDT": 0.0378, "ZBT/USDT": 0.12078}
        history = self.harness.history()
        self.assertAlmostEqual(
            history["virtual_balance"],
            compute_sim_cash_from_trades(history["trades"], 5000.0),
            places=2,
        )
        self.harness.assert_portfolio_invariant(prices)

    def test_manual_and_auto_trades_share_same_cash_ledger(self):
        self.harness.execute_buy("HIGH/USDT", 11.82, 0.0578, source="auto")
        self.harness.execute_buy("CAT/USDT", 500, 1.514e-06, source="manual")
        self.harness.execute_buy("ZBT/USDT", 250, 0.12078, source="manual")

        history = self.harness.history()
        self.assertAlmostEqual(history["virtual_balance"], 5000 - 11.82 - 500 - 250)
        prices = {"HIGH/USDT": 0.0578, "CAT/USDT": 1.514e-06, "ZBT/USDT": 0.12078}
        self.harness.assert_portfolio_invariant(prices)

    def test_portfolio_summary_matches_cash_plus_unrealized(self):
        self.harness.execute_buy("CAT/USDT", 500, 1.514e-06)
        self.harness.execute_buy("ZBT/USDT", 250, 0.12078)
        prices = {"CAT/USDT": 1.6e-06, "ZBT/USDT": 0.125}

        history = self.harness.history()
        active = list_active_positions()
        unreal = _unrealized_pnl(active, prices)
        cash = float(history["virtual_balance"])
        msg = format_portfolio_summary(
            history, unreal, len(active), mode_label="live [DRY RUN]",
            cash_balance=cash, cash_label="Cash (Sim)",
        )
        self.assertIn("Cash (Sim)", msg)
        self.assertIn(f"${cash:,.2f}", msg)
        market = _position_market_value(active, prices)
        realized = float(history.get("realized_pnl", 0))
        self.assertAlmostEqual(cash + market, self.harness.initial + realized + unreal, places=1)

    def test_risk_manager_equity_matches_sim_portfolio(self):
        self.harness.execute_buy("CAT/USDT", 500, 1.514e-06)
        self.harness.execute_buy("ZBT/USDT", 250, 0.12078)
        prices = {"CAT/USDT": 1.514e-06, "ZBT/USDT": 0.12078}

        rm = RiskManager(self.harness.config())
        equity = rm._portfolio_equity(reference_price=0)
        cash = fetch_usdt_balance(self.harness.config())
        market = _position_market_value(list_active_positions(), prices)
        self.assertAlmostEqual(equity, cash + market, places=2)
        self.assertAlmostEqual(equity, self.harness.initial, places=2)

    def test_rebuy_after_partial_sell_keeps_invariant(self):
        self.harness.execute_buy("SOL/USDT", 300, 100.0)
        pos = get_position("SOL/USDT", "4h")
        half = float(pos["amount"]) / 2
        self.harness.execute_sell("SOL/USDT", half, 110.0, signal="SELL_50", source="auto")
        self.harness.execute_buy("SOL/USDT", 150, 105.0, source="manual")

        prices = {"SOL/USDT": 108.0}
        history = self.harness.history()
        self.assertAlmostEqual(
            compute_sim_realized_pnl(history["trades"]),
            float(history["realized_pnl"]),
            places=2,
        )
        self.harness.assert_portfolio_invariant(prices)


if __name__ == "__main__":
    unittest.main()