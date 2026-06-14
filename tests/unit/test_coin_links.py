import json
import unittest
from unittest.mock import patch

from notifications.coin_links import (
    cmc_coin_url,
    format_links_line,
    format_ticker_html,
    gate_trade_url,
    inline_link_buttons,
    normalize_ticker,
    tradingview_chart_url,
)


class TestCoinLinks(unittest.TestCase):
    def test_normalize_ticker(self):
        self.assertEqual(normalize_ticker("H/USDT"), "H")
        self.assertEqual(normalize_ticker("btc"), "BTC")

    def test_gate_and_tradingview_urls(self):
        self.assertEqual(gate_trade_url("H"), "https://www.gate.io/trade/H_USDT")
        self.assertIn("GATEIO%3AHUSDT", tradingview_chart_url("H"))

    @patch("notifications.coin_links._load_cache")
    def test_cmc_url_with_cached_slug(self, mock_cache):
        mock_cache.return_value = {"BTC": "bitcoin"}
        self.assertEqual(cmc_coin_url("BTC"), "https://coinmarketcap.com/currencies/bitcoin/")

    @patch("notifications.coin_links._load_cache")
    def test_cmc_url_search_fallback(self, mock_cache):
        mock_cache.return_value = {}
        with patch("notifications.coin_links._resolve_slug_from_api", return_value=""):
            url = cmc_coin_url("OBSCURE")
        self.assertIn("search/?q=OBSCURE", url)

    @patch("notifications.coin_links.coin_links_config")
    def test_format_ticker_html_contains_link(self, mock_cfg):
        mock_cfg.return_value = {"enabled": True}
        with patch("notifications.coin_links.cmc_coin_url", return_value="https://coinmarketcap.com/currencies/bitcoin/"):
            html = format_ticker_html("BTC", symbol_suffix="/USDT")
        self.assertIn('<a href="https://coinmarketcap.com/currencies/bitcoin/">BTC</a>/USDT', html)

    @patch("notifications.coin_links.coin_links_config")
    def test_format_links_line(self, mock_cfg):
        mock_cfg.return_value = {
            "enabled": True,
            "show_cmc": True,
            "show_gate": True,
            "show_tradingview": True,
        }
        line = format_links_line("SOL")
        self.assertIn("CMC", line)
        self.assertIn("Gate", line)
        self.assertIn("Chart", line)

    @patch("notifications.coin_links.coin_links_config")
    def test_inline_buttons(self, mock_cfg):
        mock_cfg.return_value = {
            "enabled": True,
            "inline_buttons_on_signals": True,
            "show_cmc": True,
            "show_gate": True,
            "show_tradingview": True,
        }
        rows = inline_link_buttons("ETH")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 3)
        self.assertIn("url", rows[0][0])


if __name__ == "__main__":
    unittest.main()