import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from price_fetcher import (
    _last_good_cache,
    _price_cache,
    format_token_amount,
    format_usdt_price,
    get_prices_batch,
)


class TestPriceFetcher(unittest.TestCase):
    def setUp(self):
        _price_cache.clear()
        _last_good_cache.clear()

    def test_format_usdt_price_micro_cap(self):
        self.assertEqual(format_usdt_price(1.514e-06), "$0.000001514")
        self.assertEqual(format_usdt_price(1.516e-06), "$0.000001516")
        self.assertNotEqual(format_usdt_price(1.514e-06), "$0.00000151")

    def test_format_usdt_price_does_not_rstrip_significant_zeros(self):
        self.assertEqual(format_usdt_price(1.51e-06), "$0.000001510")

    def test_format_usdt_price_regular(self):
        self.assertEqual(format_usdt_price(0.0363), "$0.0363")
        self.assertEqual(format_usdt_price(12.5), "$12.50")

    def test_get_prices_batch_uses_entry_fallback_when_live_missing(self):
        with patch("price_fetcher._fetch_gate_bulk", return_value={}), \
             patch("price_fetcher._fetch_coingecko_bulk", return_value={}), \
             patch("price_fetcher._fetch_single_symbol", return_value=("CAT/USDT", 0.0)):
            prices, sources = get_prices_batch(
                ["CAT/USDT"],
                fallbacks={"CAT/USDT": 1.514e-06},
                return_sources=True,
            )
        self.assertAlmostEqual(prices["CAT/USDT"], 1.514e-06)
        self.assertEqual(sources["CAT/USDT"], "entry")

    def test_get_prices_batch_uses_stale_cache_before_entry(self):
        _last_good_cache["CAT/USDT"] = 1.6e-06
        with patch("price_fetcher._fetch_gate_bulk", return_value={}), \
             patch("price_fetcher._fetch_coingecko_bulk", return_value={}), \
             patch("price_fetcher._fetch_single_symbol", return_value=("CAT/USDT", 0.0)):
            prices, sources = get_prices_batch(
                ["CAT/USDT"],
                fallbacks={"CAT/USDT": 1.514e-06},
                return_sources=True,
            )
        self.assertAlmostEqual(prices["CAT/USDT"], 1.6e-06)
        self.assertEqual(sources["CAT/USDT"], "stale")

    def test_get_prices_batch_fetches_cat_from_gate(self):
        with patch("price_fetcher._fetch_gate_bulk", return_value={"CAT/USDT": 1.514e-06}):
            prices, sources = get_prices_batch(["CAT/USDT"], return_sources=True)
        self.assertAlmostEqual(prices["CAT/USDT"], 1.514e-06)
        self.assertEqual(sources["CAT/USDT"], "live")

    def test_get_prices_batch_cached_returns_sources_tuple(self):
        _price_cache["SOL/USDT"] = (42.0, 9999999999.0)
        prices, sources = get_prices_batch(["SOL/USDT"], return_sources=True)
        self.assertAlmostEqual(prices["SOL/USDT"], 42.0)
        self.assertEqual(sources["SOL/USDT"], "live")

    def test_format_token_amount_micro_cap(self):
        self.assertEqual(format_token_amount(32597.3574), "32,597.3574")
        self.assertIn("330,250,990", format_token_amount(330250990.75))


if __name__ == "__main__":
    unittest.main()