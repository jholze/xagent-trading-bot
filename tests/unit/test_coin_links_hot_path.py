"""coin_links must not hit network or effective watchlist on list hot path."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications import coin_links


class TestCoinLinksHotPath(unittest.TestCase):
    def test_format_ticker_html_no_network_no_effective_wl(self):
        with patch.object(coin_links, "_resolve_slug_from_api") as api, patch.object(
            coin_links, "_watchlist_name_for_ticker"
        ) as wl, patch.object(coin_links, "_load_cache", return_value={}), patch.object(
            coin_links, "coin_links_enabled", return_value=True
        ):
            html = coin_links.format_ticker_html("BTC", symbol_suffix="", allow_network=False)
        api.assert_not_called()
        wl.assert_not_called()
        self.assertIn("BTC", html)
        self.assertIn("gate.io", html.lower())

    def test_resolve_slug_cache_only_by_default(self):
        with patch.object(coin_links, "_load_cache", return_value={"ETH": "ethereum"}), patch.object(
            coin_links, "_resolve_slug_from_api"
        ) as api:
            slug = coin_links.resolve_cmc_slug("ETH")
        self.assertEqual(slug, "ethereum")
        api.assert_not_called()

    def test_format_order_line_uses_no_network_ticker(self):
        from services.order_service import format_order_line

        order = {
            "status": "filled",
            "side": "buy",
            "symbol": "AAA/USDT",
            "display_seq": 1,
            "source": "grid",
            "request": {"usdt": 100},
            "execution": {"usdt": 100, "price": 1.0},
            "timestamps": {"filled": "2026-07-30T12:00:00"},
        }
        with patch(
            "notifications.coin_links.format_ticker_html",
            return_value="AAA",
        ) as ft:
            line = format_order_line(order)
        self.assertIn("#1", line)
        # must request allow_network=False
        kwargs = ft.call_args.kwargs if ft.call_args else {}
        self.assertFalse(kwargs.get("allow_network", True))


if __name__ == "__main__":
    unittest.main()
