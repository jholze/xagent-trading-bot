"""#11: entry-sensor / DCA must not KeyError on position keys."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.positions import (
    clear_positions_memory,
    get_key,
    get_position,
    mark_profit_max_lifetime_done,
    mark_trailing_take_profit_step,
    positions,
    update_position,
)


class TestGetPositionNeverKeyError(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def test_get_position_missing_key_returns_empty(self):
        pos = get_position("O/USDT", "1h")
        self.assertIsInstance(pos, dict)
        self.assertEqual(float(pos.get("amount") or 0), 0.0)
        self.assertIn(get_key("O/USDT", "1h"), positions)

    def test_get_position_idempotent_for_doge(self):
        a = get_position("DOGE/USDT", "4h")
        b = get_position("DOGE/USDT", "4h")
        self.assertIs(a, b)
        self.assertEqual(get_key("DOGE/USDT", "4h"), "DOGE_USDT_4h")

    def test_concurrent_get_position_no_keyerror(self):
        errors: list[BaseException] = []

        def worker(i: int):
            try:
                for _ in range(50):
                    get_position(f"C{i}/USDT", "1h")
                    get_position("PLUME/USDT", "1h")
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"concurrent KeyErrors: {errors}")

    def test_mark_trail_tp_without_prior_init(self):
        # previously used bare positions[key] and KeyError'd for non-default tenant / missing key
        mark_trailing_take_profit_step("NEW/USDT", "4h", 1.25)
        pos = get_position("NEW/USDT", "4h")
        self.assertAlmostEqual(float(pos.get("recent_high") or 0), 1.25)

    def test_mark_profit_max_without_prior_init(self):
        mark_profit_max_lifetime_done("NEW2/USDT", "4h")
        self.assertTrue(get_position("NEW2/USDT", "4h").get("profit_max_lifetime_done"))

    def test_update_position_buy_after_empty(self):
        with patch("strategies.positions.save_positions_document", return_value=True):
            update_position("FRESH/USDT", "4h", "BUY", 2.0, amount_traded=5)
        pos = get_position("FRESH/USDT", "4h")
        self.assertEqual(pos["amount"], Decimal("5"))


class TestDcaTargetsSkipBadCoin(unittest.TestCase):
    def test_collect_dca_targets_swallows_per_coin_errors(self):
        from strategies.dca_portfolio import collect_dca_targets

        clear_positions_memory()
        coins = [
            {"symbol": "GOOD/USDT", "timeframe": "4h", "active": True},
            {"symbol": "BAD/USDT", "timeframe": "4h", "active": True},
        ]
        prices = {"GOOD/USDT": 1.0, "BAD/USDT": 1.0}

        def boom_get(symbol, tf):
            if symbol == "BAD/USDT":
                raise KeyError(get_key(symbol, tf))
            return get_position(symbol, tf)

        with patch("strategies.dca_portfolio.get_position", side_effect=boom_get), patch(
            "strategies.dca_portfolio.resolve_coin_config",
            side_effect=lambda c: {
                "symbol": c["symbol"],
                "timeframe": c.get("timeframe", "4h"),
                "strategy_params": {"dca": {"portfolio": {"enabled": True, "min_dca_score": 0}}},
            },
        ), patch(
            "strategies.dca_portfolio.evaluate_dca_addon", return_value=None
        ), patch(
            "strategies.dca_portfolio.resolve_strategy_params",
            return_value={"dca": {"portfolio": {"enabled": True, "min_dca_score": 0}}},
        ):
            # must not raise KeyError for BAD
            out = collect_dca_targets(coins, prices, config_raw={})
        self.assertIsInstance(out, list)


if __name__ == "__main__":
    unittest.main()
