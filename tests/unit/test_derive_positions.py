"""Pure derive path: orders SOT + cache field merge + material cache-only lots."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.positions import (
    derive_positions_from_orders_and_cache,
    is_open_position,
    prune_orphan_position_cache,
)


def _open_keys(snapshot: dict) -> set[str]:
    return {k for k, v in snapshot.items() if is_open_position(v)}


class TestDerivePositions(unittest.TestCase):
    def test_material_cache_only_lot_injected_when_not_order_ledger(self):
        order_snap = {f"COIN{i}_USDT_4h": {"amount": 10.0, "peak_amount": 10.0} for i in range(25)}
        cache_doc = {
            "positions": {
                "BTC_USDT_4h": {"amount": 1.0, "peak_amount": 1.0, "recent_high": 99.0},
                "COIN0_USDT_4h": {"recent_high": 1.5},
            }
        }
        with patch("core.simulated_trading.uses_order_ledger_cash", return_value=False), patch(
            "data_manager.get_config", return_value={}
        ):
            derived = derive_positions_from_orders_and_cache(order_snap, cache_doc)
        self.assertEqual(len(derived), 26)
        self.assertIn("BTC_USDT_4h", derived)
        self.assertEqual(derived["COIN0_USDT_4h"]["recent_high"], 1.5)
        self.assertEqual(len(_open_keys(derived)), 26)

    def test_cache_only_lot_skipped_when_order_ledger_cash(self):
        order_snap = {"COIN0_USDT_4h": {"amount": 10.0, "peak_amount": 10.0}}
        cache_doc = {
            "positions": {
                "BTC_USDT_4h": {"amount": 1.0, "peak_amount": 1.0, "average_entry": 1.0},
                "COIN0_USDT_4h": {"recent_high": 1.5},
            }
        }
        with patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), patch(
            "data_manager.get_config", return_value={"live": {"dry_run": True}}
        ):
            derived = derive_positions_from_orders_and_cache(order_snap, cache_doc)
        self.assertEqual(set(derived.keys()), {"COIN0_USDT_4h"})
        self.assertEqual(derived["COIN0_USDT_4h"]["recent_high"], 1.5)

    def test_empty_orders_uses_material_cache_lots_legacy_only(self):
        with patch("core.simulated_trading.uses_order_ledger_cash", return_value=False), patch(
            "data_manager.get_config", return_value={}
        ):
            derived = derive_positions_from_orders_and_cache(
                {},
                {"positions": {"BTC_USDT_4h": {"amount": 5.0, "average_entry": 100.0}}},
            )
        self.assertEqual(set(derived.keys()), {"BTC_USDT_4h"})
        self.assertEqual(derived["BTC_USDT_4h"]["amount"], 5.0)

    def test_dust_cache_lot_not_injected(self):
        derived = derive_positions_from_orders_and_cache(
            {},
            {"positions": {"BTC_USDT_4h": {"amount": 1e-15, "average_entry": 100.0}}},
        )
        self.assertEqual(derived, {})

    def test_prune_orphan_position_cache_order_ledger(self):
        order_snap = {"ADA_USDT_1h": {"amount": 10.0, "peak_amount": 10.0}}
        cache_doc = {
            "positions": {
                "ADA_USDT_1h": {"recent_high": 1.1},
                "SPCX_USDT_1h": {"amount": 26.0, "peak_amount": 26.0},
            }
        }
        with patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), patch(
            "data_manager.get_config", return_value={"live": {"dry_run": True}}
        ):
            pruned, orphans = prune_orphan_position_cache(order_snap, cache_doc)
        self.assertEqual(orphans, ["SPCX_USDT_1h"])
        self.assertNotIn("SPCX_USDT_1h", pruned["positions"])
        self.assertIn("ADA_USDT_1h", pruned["positions"])


if __name__ == "__main__":
    unittest.main()