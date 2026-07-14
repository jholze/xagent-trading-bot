"""Pure derive path: orders SOT + cache field merge + material cache-only lots."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.positions import derive_positions_from_orders_and_cache, is_open_position


def _open_keys(snapshot: dict) -> set[str]:
    return {k for k, v in snapshot.items() if is_open_position(v)}


class TestDerivePositions(unittest.TestCase):
    def test_material_cache_only_lot_injected(self):
        order_snap = {f"COIN{i}_USDT_4h": {"amount": 10.0, "peak_amount": 10.0} for i in range(25)}
        cache_doc = {
            "positions": {
                "BTC_USDT_4h": {"amount": 1.0, "peak_amount": 1.0, "recent_high": 99.0},
                "COIN0_USDT_4h": {"recent_high": 1.5},
            }
        }
        derived = derive_positions_from_orders_and_cache(order_snap, cache_doc)
        self.assertEqual(len(derived), 26)
        self.assertIn("BTC_USDT_4h", derived)
        self.assertEqual(derived["COIN0_USDT_4h"]["recent_high"], 1.5)
        self.assertEqual(len(_open_keys(derived)), 26)

    def test_empty_orders_uses_material_cache_lots(self):
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


if __name__ == "__main__":
    unittest.main()